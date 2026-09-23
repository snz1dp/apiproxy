"""节点独立API密钥：运行时选择、过滤、自动禁用与内存同步的单元测试"""

import hashlib
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import orjson

from openaiproxy.services.database.models.node.model import QuotaResetCycle
from openaiproxy.services.nodeproxy.schemas import NodeApiKeyEntry, Status
from openaiproxy.services.nodeproxy.service import (
    NodeProxyService,
    _RequestContext,
    INVALID_API_KEY_FAILURE_THRESHOLD,
)


def _build_service() -> NodeProxyService:
    """构造仅含内存状态的最小 NodeProxyService 实例（绕过完整初始化）"""
    service = object.__new__(NodeProxyService)
    service._lock = threading.RLock()
    service.nodes = {}
    service.snode = {}
    service._offline_nodes = {}
    service._node_metadata = {}
    return service


def _entry(priority: int = 1, max_tokens=None, tokens_used: int = 0) -> NodeApiKeyEntry:
    """构造运行时密钥条目"""
    return NodeApiKeyEntry(
        api_key_id=uuid4(),
        api_key=f"sk-test-{uuid4().hex[:8]}",
        priority=priority,
        max_tokens=max_tokens,
        tokens_used=tokens_used,
    )


def _record(**overrides) -> SimpleNamespace:
    """构造模拟 NodeApiKey ORM 记录，字段与真实模型 schema 对齐。

    默认值为一条健康、未冻结、无周期的记录；测试通过 overrides 覆盖关注字段。
    """
    fields = {
        "id": uuid4(),
        "enabled": True,
        "expires_at": None,
        "max_tokens": None,
        "tokens_used": 0,
        "api_key": "enc",
        "priority": 1,
        "frozen_until": None,
        "quota_reset_cycle": QuotaResetCycle.none,
        "quota_next_reset_at": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# ── 加权选择 ─────────────────────────────────────────────


def test_select_api_key_returns_none_without_keys():
    """无独立密钥时返回 None（调用方回退节点主密钥）"""
    service = _build_service()
    status = Status(api_keys=[])
    assert service._select_api_key(status) is None


def test_select_api_key_skips_zero_priority_and_exhausted():
    """priority=0 或已达限额的密钥不参与选择"""
    service = _build_service()
    skipped_priority = _entry(priority=0)
    skipped_exhausted = _entry(priority=5, max_tokens=100, tokens_used=100)
    available = _entry(priority=3)
    status = Status(api_keys=[skipped_priority, skipped_exhausted, available])

    for _ in range(20):
        assert service._select_api_key(status) is available


def test_select_api_key_weight_distribution():
    """高权重密钥被选中的频率显著更高（100:1 权重）"""
    service = _build_service()
    high = _entry(priority=100)
    low = _entry(priority=1)
    status = Status(api_keys=[high, low])

    picks = [service._select_api_key(status) for _ in range(200)]
    high_count = sum(1 for pick in picks if pick is high)
    # 期望约 99% 命中高权重；宽松下界防止偶发抖动导致脆断
    assert high_count >= 150


# ── 运行时条目构建（过滤与解密容错） ─────────────────────


def test_build_entries_filters_disabled_expired_overquota():
    """禁用、过期、超额的记录不进入运行时条目"""
    now = datetime.now().astimezone()
    disabled = _record(enabled=False, api_key="enc-1")
    expired = _record(expires_at=now - timedelta(hours=1), api_key="enc-2")
    over_quota = _record(max_tokens=10, tokens_used=10, api_key="enc-3")
    frozen = _record(frozen_until=now + timedelta(hours=1), api_key="enc-5")
    healthy = _record(
        expires_at=now + timedelta(days=1),
        max_tokens=1000, tokens_used=10, api_key="enc-4", priority=3,
    )
    records = [disabled, expired, over_quota, frozen, healthy]

    with patch(
        "openaiproxy.services.nodeproxy.service.decrypt_api_key",
        side_effect=lambda token: f"plain-{token}",
    ):
        entries = NodeProxyService._build_node_api_key_entries(
            node_url="http://node.example.com",
            api_key_records=records,
            evaluation_now=now,
        )

    assert len(entries) == 1
    assert entries[0].api_key_id == healthy.id
    assert entries[0].api_key == "plain-enc-4"
    assert entries[0].priority == 3


def test_build_entries_skips_decrypt_failure_without_breaking_others():
    """单条解密失败仅跳过该条，不影响其他密钥"""
    now = datetime.now().astimezone()
    from openaiproxy.utils.apikey import ApiKeyEncryptionError

    bad = _record(api_key="bad", priority=1)
    good = _record(api_key="good", priority=2)

    def _decrypt(token: str) -> str:
        if token == "bad":
            raise ApiKeyEncryptionError("decrypt failed")
        return f"plain-{token}"

    with patch(
        "openaiproxy.services.nodeproxy.service.decrypt_api_key", side_effect=_decrypt
    ):
        entries = NodeProxyService._build_node_api_key_entries(
            node_url="http://node.example.com",
            api_key_records=[bad, good],
            evaluation_now=now,
        )

    assert [entry.api_key_id for entry in entries] == [good.id]
    assert entries[0].api_key == "plain-good"
    assert entries[0].priority == 2


# ── 后处理：累计、限额错误自动禁用、超额自动禁用 ─────────


def test_post_process_zero_tokens_no_side_effect():
    """total_tokens=0 且无错误时：不累计、不禁用"""
    service = _build_service()
    entry = _entry()
    context = _RequestContext(start_time=0.0, total_tokens=0, node_api_key_entry=entry)

    with patch.object(service, "_disable_node_api_key") as disable_mock, \
            patch("openaiproxy.services.nodeproxy.service.run_until_complete") as run_mock:
        service._post_process_api_key_usage(context)

    disable_mock.assert_not_called()
    run_mock.assert_not_called()


def test_post_process_capacity_error_disables_key():
    """下游限额错误触发自动禁用且不再做超额检查"""
    service = _build_service()
    entry = _entry(max_tokens=100000, tokens_used=10)
    context = _RequestContext(
        start_time=0.0,
        total_tokens=5,
        node_api_key_entry=entry,
        backend_capacity_exhausted=True,
        error_message="insufficient_quota",
    )

    with patch.object(service, "_disable_node_api_key") as disable_mock, \
            patch("openaiproxy.services.nodeproxy.service.run_until_complete"):
        service._post_process_api_key_usage(context)

    disable_mock.assert_called_once()
    kwargs = disable_mock.call_args.kwargs
    assert kwargs["api_key_id"] == entry.api_key_id
    assert "下游限额错误" in kwargs["reason"]


def test_post_process_over_quota_disables_key():
    """累计用量达到 max_tokens 时自动禁用"""
    service = _build_service()
    entry = _entry(max_tokens=100, tokens_used=95)
    context = _RequestContext(start_time=0.0, total_tokens=10, node_api_key_entry=entry)

    with patch.object(service, "_disable_node_api_key") as disable_mock, \
            patch("openaiproxy.services.nodeproxy.service.run_until_complete"):
        service._post_process_api_key_usage(context)

    disable_mock.assert_called_once()
    assert "达到上限" in disable_mock.call_args.kwargs["reason"]


def test_post_process_under_quota_no_disable():
    """累计用量未达上限时不禁用"""
    service = _build_service()
    entry = _entry(max_tokens=100, tokens_used=10)
    context = _RequestContext(start_time=0.0, total_tokens=5, node_api_key_entry=entry)

    with patch.object(service, "_disable_node_api_key") as disable_mock, \
            patch("openaiproxy.services.nodeproxy.service.run_until_complete"):
        service._post_process_api_key_usage(context)

    disable_mock.assert_not_called()


# ── 内存同步：移除与恢复 ─────────────────────────────────


def test_forget_api_key_removes_from_all_memory_maps():
    """删除密钥后从 snode/nodes/_offline_nodes 全部内存状态中移除条目"""
    service = _build_service()
    doomed = _entry()
    keeper = _entry()
    node_url = "http://node.example.com"
    for store in (service.snode, service.nodes, service._offline_nodes):
        store[node_url] = Status(api_keys=[doomed, keeper])

    service.forget_node_api_key(doomed.api_key_id)

    for store in (service.snode, service.nodes, service._offline_nodes):
        remaining = store[node_url].api_keys
        assert [entry.api_key_id for entry in remaining] == [keeper.api_key_id]


def test_restore_api_key_invalidates_config_version():
    """手动重新启用密钥时，使所属节点配置指纹失效以触发下轮刷新"""
    from openaiproxy.services.nodeproxy.service import _NodeMetadata

    service = _build_service()
    entry_id = uuid4()
    other_entry_id = uuid4()
    node_url = "http://node.example.com"
    service._node_metadata[node_url] = _NodeMetadata(
        node_id=uuid4(),
        config_version="v1:1:1:gpt-4o",
        api_key_ids=[entry_id],
    )
    service._node_metadata["http://other.example.com"] = _NodeMetadata(
        node_id=uuid4(),
        config_version="v2:1:1:gpt-4o",
        api_key_ids=[other_entry_id],
    )

    with patch("openaiproxy.services.nodeproxy.service.run_until_complete"):
        ok = service.restore_node_api_key_availability(entry_id)

    assert ok is True
    assert service._node_metadata[node_url].config_version == ""
    # 无关节点保持原配置指纹
    assert service._node_metadata["http://other.example.com"].config_version == "v2:1:1:gpt-4o"


# ── 限额错误识别 ─────────────────────────────────────────

def test_is_rate_limit_error_message_matches_quota_keywords():
    """错误信息包含限额/配额关键词时识别为限额错误（大小写不敏感）"""
    assert NodeProxyService._is_rate_limit_error_message("Rate Limit Exceeded") is True
    assert NodeProxyService._is_rate_limit_error_message("insufficient_quota") is True
    assert NodeProxyService._is_rate_limit_error_message("You exceeded your current quota") is True
    assert NodeProxyService._is_rate_limit_error_message("余额不足，请充值") is True


def test_is_rate_limit_error_message_ignores_unrelated_and_empty():
    """无关错误、空值不识别为限额错误"""
    assert NodeProxyService._is_rate_limit_error_message("The model does not exist") is False
    assert NodeProxyService._is_rate_limit_error_message("") is False
    assert NodeProxyService._is_rate_limit_error_message(None) is False


# ── 管理接口哈希一致性 ───────────────────────────────────


def test_hash_node_api_key_is_sha256_hex():
    """管理接口的密钥哈希为明文 SHA256 十六进制（64位），与模型约束一致"""
    from openaiproxy.api.node_apikey import _hash_node_api_key

    plaintext = "sk-abc123"
    digest = _hash_node_api_key(plaintext)
    assert digest == hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    assert len(digest) == 64


# ── D1：限额时节点连坐保护 ───────────────────────────────

def test_node_has_healthy_api_keys_true_when_other_keys_available():
    """节点存在其他健康密钥时返回 True（D1：不应踢节点）"""
    service = _build_service()
    healthy = _entry(priority=3)
    failed = _entry(priority=5)
    status = Status(api_keys=[healthy, failed])
    service.snode["http://node.example.com"] = status

    assert service._node_has_healthy_api_keys(
        "http://node.example.com", exclude_entry=failed) is True


def test_node_has_healthy_api_keys_false_when_only_failed_key():
    """节点只有失败密钥自身时返回 False（应踢节点）"""
    service = _build_service()
    failed = _entry(priority=5)
    status = Status(api_keys=[failed])
    service.snode["http://node.example.com"] = status

    assert service._node_has_healthy_api_keys(
        "http://node.example.com", exclude_entry=failed) is False


def test_node_has_healthy_api_keys_false_without_keys():
    """节点无独立密钥时返回 False（回退节点主密钥语义，允许踢节点）"""
    service = _build_service()
    service.snode["http://node.example.com"] = Status(api_keys=[])

    assert service._node_has_healthy_api_keys("http://node.example.com") is False


def test_cleanup_keeps_node_with_healthy_keys():
    """限额清理时节点仍有健康密钥则不踢节点（D1 核心场景）"""
    service = _build_service()
    node_url = "http://node.example.com"
    healthy = _entry(priority=3)
    failed = _entry(priority=5)
    service.snode[node_url] = Status(api_keys=[healthy, failed])
    service.snode["http://other.example.com"] = Status(api_keys=[])

    context = _RequestContext(
        request_id=uuid4(),
        start_time=time.time(),
        model_name="gpt-4o-mini",
        model_type="chat",
        request_action=None,
        stream=False,
        request_data=None,
        client_ip=None,
        api_key_id=None,
    )
    context.node_api_key_entry = failed
    context.node_api_key_id = failed.api_key_id
    context.error = True
    context.error_message = "You exceeded your current quota"
    context.backend_capacity_exhausted = True

    unavailable_calls = []
    service.mark_backend_node_unavailable = (
        lambda node_url, reason=None: unavailable_calls.append((node_url, reason)))
    service._active_request_leases = {}

    service.cleanup_backend_capacity_exhausted_attempt(node_url, context, {})

    # 节点未被踢出
    assert not unavailable_calls


# ── D2：同节点换密钥重试 ─────────────────────────────────

def test_select_node_api_key_excludes_attempted_ids():
    """select_node_api_key 排除本请求已失败的密钥ID（D2）"""
    service = _build_service()
    failed = _entry(priority=100)
    healthy = _entry(priority=1)
    service.snode["http://node.example.com"] = Status(
        api_keys=[failed, healthy])

    for _ in range(20):
        selected = service.select_node_api_key(
            "http://node.example.com",
            exclude_api_key_ids={failed.api_key_id},
        )
        assert selected is healthy


# ── D3：无效密钥熔断 ─────────────────────────────────────

def test_is_invalid_api_key_error_message_matches_auth_keywords():
    """错误信息包含鉴权失败关键词时识别为无效密钥错误"""
    assert NodeProxyService._is_invalid_api_key_error_message(
        "Invalid API key provided") is True
    assert NodeProxyService._is_invalid_api_key_error_message(
        "Incorrect API key provided") is True
    assert NodeProxyService._is_invalid_api_key_error_message(
        "401 Unauthorized") is True
    assert NodeProxyService._is_invalid_api_key_error_message(
        "403 Forbidden: access denied") is True


def test_is_invalid_api_key_error_message_ignores_unrelated():
    """无关错误、空值不识别为无效密钥错误"""
    assert NodeProxyService._is_invalid_api_key_error_message(
        "The model does not exist") is False
    assert NodeProxyService._is_invalid_api_key_error_message("") is False
    assert NodeProxyService._is_invalid_api_key_error_message(None) is False


def test_record_api_key_auth_failure_counts_and_resets():
    """鉴权失败计数累加；成功一次清零（D3 计数器行为）"""
    service = _build_service()
    service._api_key_auth_failures = {}
    entry = _entry(priority=1)

    assert service._record_api_key_auth_failure(entry) == 1
    assert service._record_api_key_auth_failure(entry) == 2
    service._reset_api_key_auth_failures(entry.api_key_id)
    assert service._record_api_key_auth_failure(entry) == 1


def test_post_process_invalid_api_key_disables_after_threshold():
    """连续鉴权失败达到阈值后自动禁用密钥（D3 熔断）"""
    service = _build_service()
    service._api_key_auth_failures = {}
    node_url = "http://node.example.com"
    entry = _entry(priority=1)
    service.snode[node_url] = Status(api_keys=[entry])

    disable_calls = []
    service._disable_node_api_key = (
        lambda api_key_id, reason: disable_calls.append((api_key_id, reason)))

    invalid_error_payload = {
        "error": {
            "message": "Invalid API key provided",
            "type": "invalid_request_error",
            "code": "invalid_api_key",
        }
    }

    def _build_context() -> _RequestContext:
        context = _RequestContext(
            request_id=uuid4(),
            start_time=datetime.now(),
            model_name="gpt-4o-mini",
            model_type="chat",
            request_action=None,
            stream=False,
            request_data=None,
            client_ip=None,
            api_key_id=None,
        )
        context.node_api_key_entry = entry
        context.node_api_key_id = entry.api_key_id
        context.error = True
        context.error_message = "Invalid API key provided"
        context.response_data = orjson.dumps(invalid_error_payload).decode("utf-8")
        return context

    # 前 N-1 次失败只计数不熔断
    for _ in range(INVALID_API_KEY_FAILURE_THRESHOLD - 1):
        service._post_process_api_key_usage(_build_context())
    assert not disable_calls

    # 第 N 次失败触发熔断禁用
    service._post_process_api_key_usage(_build_context())
    assert len(disable_calls) == 1
    assert disable_calls[0][0] == entry.api_key_id
