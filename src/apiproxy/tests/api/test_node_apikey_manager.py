"""节点独立API密钥管理接口集成测试（创建/upsert→查询→更新→重新启用清空→删除）"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlmodel import delete

from openaiproxy.api.utils import check_api_key
from openaiproxy.services.database.models import (
    Node as OpenAINode,
    NodeModel as OpenAINodeModel,
)
from openaiproxy.services.database.models.node.model import NodeApiKey, ProtocolType
from openaiproxy.services.database.models.node.crud import (
    disable_node_api_key,
    increment_node_api_key_tokens_used,
    select_node_api_key_by_id,
)
from openaiproxy.services.deps import get_async_session, get_node_proxy_service
from openaiproxy.utils.apikey import decrypt_api_key
from openaiproxy.utils.timezone import current_time_in_timezone


class DummyNodeProxyService:
    """记录跨实例同步调用的轻量替身（node_apikey 直接调用 get_node_proxy_service）"""

    def __init__(self) -> None:
        self.restore_calls: list[UUID] = []
        self.forget_calls: list[UUID] = []

    def restore_node_api_key_availability(self, api_key_id: UUID) -> bool:
        self.restore_calls.append(api_key_id)
        return True

    def forget_node_api_key(self, api_key_id: UUID) -> None:
        self.forget_calls.append(api_key_id)


@pytest.fixture
async def clean_session(session):
    await session.exec(delete(NodeApiKey))
    await session.exec(delete(OpenAINodeModel))
    await session.exec(delete(OpenAINode))
    await session.commit()
    try:
        yield session
    finally:
        await session.rollback()
        await session.exec(delete(NodeApiKey))
        await session.exec(delete(OpenAINodeModel))
        await session.exec(delete(OpenAINode))
        await session.commit()


@pytest.fixture
async def api_client(clean_session):
    from openaiproxy.main import setup_app

    app = setup_app(backend_only=True)
    dummy_service = DummyNodeProxyService()

    async def override_session():
        yield clean_session

    async def override_api_key():
        return None

    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[check_api_key] = override_api_key

    transport = ASGITransport(app=app)

    # node_apikey 路由直接调用 get_node_proxy_service()，需 patch 模块命名空间
    import openaiproxy.api.node_apikey as node_apikey_module

    original_getter = node_apikey_module.get_node_proxy_service
    node_apikey_module.get_node_proxy_service = lambda: dummy_service
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, dummy_service, clean_session
    finally:
        node_apikey_module.get_node_proxy_service = original_getter
        app.dependency_overrides.clear()


async def _create_node(session) -> OpenAINode:
    """直接落库一个测试节点，返回节点记录"""
    node = OpenAINode(
        url=f"http://apikey-mgr-{uuid4().hex[:8]}.example.com",
        name="apikey-mgr-node",
    )
    session.add(node)
    await session.commit()
    await session.refresh(node)
    return node

async def _create_api_key_record(session, node: OpenAINode, **overrides):
    """直接落库一条节点API密钥记录，返回记录（绕过验证逻辑）"""
    plaintext = overrides.pop("api_key", f"sk-test-{uuid4().hex[:16]}")
    payload = {
        "node_id": node.id,
        "name": "global-list-key",
        "api_key": plaintext,
        "api_key_hash": hashlib.sha256(plaintext.encode("utf-8")).hexdigest(),
        "priority": 1,
        "enabled": True,
    }
    payload.update(overrides)
    record = NodeApiKey.model_validate(payload)
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record

@pytest.mark.asyncio
async def test_list_all_node_apikeys_global_filters(api_client):
    """全局列表接口：不限定节点，支持密钥ID/节点ID/enabled/frozen过滤与分页"""
    client, _, session = api_client
    node_a = await _create_node(session)
    node_b = await _create_node(session)

    key_a = await _create_api_key_record(session, node_a)
    key_b = await _create_api_key_record(session, node_b, enabled=False)

    # 无过滤：返回全部，且带节点名称
    all_resp = await client.get("/node-apikeys")
    assert all_resp.status_code == 200
    all_payload = all_resp.json()
    assert all_payload["total"] == 2
    assert all_payload["offset"] == 0
    returned_ids = {item["id"] for item in all_payload["data"]}
    assert returned_ids == {str(key_a.id), str(key_b.id)}
    node_names = {item["id"]: item["node_name"] for item in all_payload["data"]}
    assert node_names[str(key_a.id)] == "apikey-mgr-node"
    assert node_names[str(key_b.id)] == "apikey-mgr-node"

    # 按密钥ID过滤
    by_key_resp = await client.get(
        "/node-apikeys", params={"node_api_key_id": str(key_a.id)}
    )
    assert by_key_resp.status_code == 200
    by_key_payload = by_key_resp.json()
    assert by_key_payload["total"] == 1
    assert by_key_payload["data"][0]["id"] == str(key_a.id)

    # 按节点ID过滤
    by_node_resp = await client.get(
        "/node-apikeys", params={"node_id": str(node_b.id)}
    )
    assert by_node_resp.status_code == 200
    by_node_payload = by_node_resp.json()
    assert by_node_payload["total"] == 1
    assert by_node_payload["data"][0]["id"] == str(key_b.id)

    # 按启用状态过滤
    enabled_resp = await client.get("/node-apikeys", params={"enabled": "true"})
    assert enabled_resp.status_code == 200
    enabled_payload = enabled_resp.json()
    assert enabled_payload["total"] == 1
    assert enabled_payload["data"][0]["id"] == str(key_a.id)

    # 分页
    page_resp = await client.get(
        "/node-apikeys", params={"offset": 1, "limit": 1}
    )
    assert page_resp.status_code == 200
    page_payload = page_resp.json()
    assert page_payload["total"] == 2
    assert page_payload["offset"] == 1
    assert len(page_payload["data"]) == 1

    # 响应体不泄露密钥与哈希
    for item in all_payload["data"]:
        assert "api_key" not in item
        assert "api_key_hash" not in item

@pytest.mark.asyncio
async def test_list_all_node_apikeys_empty_result(api_client):
    """全局列表接口：不存在的密钥ID返回空结果"""
    client, _, session = api_client
    await _create_node(session)

    resp = await client.get(
        "/node-apikeys", params={"node_api_key_id": str(uuid4())}
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 0
    assert payload["data"] == []


@pytest.mark.asyncio
async def test_node_apikey_crud_flow(api_client):
    """管理接口全流程：创建→列表→详情→更新→删除"""
    client, dummy_service, session = api_client
    node = await _create_node(session)

    # 创建
    create_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={"api_key": "sk-primary", "name": "主密钥", "priority": 3, "max_tokens": 1000, "verify": False},
    )
    assert create_resp.status_code == 200
    created = create_resp.json()
    key_id = UUID(created["id"])
    assert created["node_id"] == str(node.id)
    assert created["name"] == "主密钥"
    assert created["priority"] == 3
    assert created["max_tokens"] == 1000
    assert created["tokens_used"] == 0
    assert created["enabled"] is True
    # 管理接口返回解密后明文
    assert created["disabled_at"] is None
    assert created["disable_reason"] is None
    # 启用密钥触发跨实例恢复同步
    assert dummy_service.restore_calls == [key_id]

    # 数据库密文与明文一致
    stored = await select_node_api_key_by_id(key_id, session=session)
    assert stored is not None
    assert stored.api_key != "sk-primary"
    assert decrypt_api_key(stored.api_key) == "sk-primary"

    # 列表
    list_resp = await client.get(f"/nodes/{node.id}/apikeys")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    # 详情
    detail_resp = await client.get(f"/nodes/{node.id}/apikeys/{key_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["id"] == str(key_id)

    # 更新优先级与限额
    update_resp = await client.put(
        f"/nodes/{node.id}/apikeys/{key_id}",
        json={"priority": 5, "max_tokens": 2000, "name": "改名"},
    )
    assert update_resp.status_code == 200
    updated = update_resp.json()
    assert updated["priority"] == 5
    assert updated["max_tokens"] == 2000
    assert updated["name"] == "改名"

    # 删除
    delete_resp = await client.delete(f"/nodes/{node.id}/apikeys/{key_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {"message": "删除成功"}
    # 删除触发本实例内存移除
    assert dummy_service.forget_calls == [key_id]

    gone_resp = await client.get(f"/nodes/{node.id}/apikeys/{key_id}")
    assert gone_resp.status_code == 404


@pytest.mark.asyncio
async def test_node_apikey_upsert_is_idempotent(api_client):
    """同节点重复提交相同密钥视为更新（幂等），不产生重复记录"""
    client, _, session = api_client
    node = await _create_node(session)

    first = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={"api_key": "sk-dup", "priority": 1, "verify": False},
    )
    assert first.status_code == 200
    first_id = UUID(first.json()["id"])

    second = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={"api_key": "sk-dup", "priority": 7, "max_tokens": 500, "verify": False},
    )
    assert second.status_code == 200
    second_payload = second.json()
    # 命中同一条记录，字段被更新
    assert UUID(second_payload["id"]) == first_id
    assert second_payload["priority"] == 7
    assert second_payload["max_tokens"] == 500

    list_resp = await client.get(f"/nodes/{node.id}/apikeys")
    assert len(list_resp.json()) == 1


@pytest.mark.asyncio
async def test_node_apikey_reenable_clears_disable_traces(api_client):
    """自动禁用后通过管理接口重新启用：清空禁用时间/原因并重置已用Tokens"""
    client, dummy_service, session = api_client
    node = await _create_node(session)

    create_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={"api_key": "sk-recover", "priority": 1, "max_tokens": 100, "verify": False},
    )
    key_id = UUID(create_resp.json()["id"])

    # 模拟运行期累计用量 + 自动禁用（直接走 CRUD，等价于 _disable_node_api_key 落盘）
    await increment_node_api_key_tokens_used(session=session, api_key_id=key_id, delta=100)
    await disable_node_api_key(
        session=session,
        api_key_id=key_id,
        reason="已用Tokens(100)达到上限(100)",
        disabled_at=current_time_in_timezone(),
    )
    disabled_record = await select_node_api_key_by_id(key_id, session=session)
    assert disabled_record.enabled is False
    assert disabled_record.tokens_used == 100
    assert disabled_record.disable_reason is not None
    assert disabled_record.disabled_at is not None

    dummy_service.restore_calls.clear()

    # 通过 upsert 重新提交相同密钥 → 重新启用并清空痕迹
    reenable_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={"api_key": "sk-recover", "priority": 2, "verify": False},
    )
    assert reenable_resp.status_code == 200
    reenabled = reenable_resp.json()
    assert reenabled["enabled"] is True
    assert reenabled["disabled_at"] is None
    assert reenabled["disable_reason"] is None
    assert reenabled["tokens_used"] == 0
    assert reenabled["priority"] == 2
    assert dummy_service.restore_calls == [key_id]


@pytest.mark.asyncio
async def test_node_apikey_update_reenable_clears_traces(api_client):
    """更新接口设 enabled=true 同样清空自动禁用痕迹并重置用量"""
    client, _, session = api_client
    node = await _create_node(session)

    create_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={"api_key": "sk-update-recover", "priority": 1, "max_tokens": 50, "verify": False},
    )
    key_id = UUID(create_resp.json()["id"])

    await increment_node_api_key_tokens_used(session=session, api_key_id=key_id, delta=50)
    await disable_node_api_key(
        session=session,
        api_key_id=key_id,
        reason="下游限额错误: rate limit exceeded",
        disabled_at=current_time_in_timezone(),
    )

    update_resp = await client.put(
        f"/nodes/{node.id}/apikeys/{key_id}",
        json={"enabled": True},
    )
    assert update_resp.status_code == 200
    updated = update_resp.json()
    assert updated["enabled"] is True
    assert updated["disabled_at"] is None
    assert updated["disable_reason"] is None
    assert updated["tokens_used"] == 0


@pytest.mark.asyncio
async def test_node_apikey_update_enabled_key_keeps_frozen_state(api_client):
    """启用+冻结中的密钥仅更新 priority：不得重置冻结状态与已用Tokens

    回归场景：PUT 接口此前只要 record.enabled 就调用 restore，导致
    冻结中的密钥被意外解冻、tokens_used 被清零。
    """
    client, dummy_service, session = api_client
    node = await _create_node(session)

    create_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={
            "api_key": "sk-frozen-keep",
            "quota_reset_cycle": "daily",
            "quota_next_reset_at": "2099-01-02T00:00:00Z",
            "verify": False,
        },
    )
    assert create_resp.status_code == 200
    key_id = UUID(create_resp.json()["id"])

    # 模拟限额触发冻结 + 已用量
    record = await select_node_api_key_by_id(key_id, session=session)
    record.frozen_until = record.quota_next_reset_at
    record.frozen_at = record.quota_next_reset_at
    record.freeze_reason = "测试冻结"
    record.tokens_used = 100
    session.add(record)
    await session.commit()

    dummy_service.restore_calls.clear()

    # 仅更新 priority，不传 enabled
    update_resp = await client.put(
        f"/nodes/{node.id}/apikeys/{key_id}",
        json={"priority": 9},
    )
    assert update_resp.status_code == 200
    updated = update_resp.json()
    assert updated["priority"] == 9
    # 冻结状态与用量保持不变
    assert updated["frozen_until"] is not None
    assert updated["frozen_at"] is not None
    assert updated["freeze_reason"] == "测试冻结"
    assert updated["tokens_used"] == 100
    # 不触发运行时恢复（否则会误清冻结状态）
    assert dummy_service.restore_calls == []


@pytest.mark.asyncio
async def test_node_apikey_update_enabled_true_noop_keeps_frozen_state(api_client):
    """启用+冻结中的密钥显式传 enabled=true（无状态切换）：同样不得重置"""
    client, dummy_service, session = api_client
    node = await _create_node(session)

    create_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={
            "api_key": "sk-frozen-noop",
            "quota_reset_cycle": "daily",
            "quota_next_reset_at": "2099-01-02T00:00:00Z",
            "verify": False,
        },
    )
    assert create_resp.status_code == 200
    key_id = UUID(create_resp.json()["id"])

    record = await select_node_api_key_by_id(key_id, session=session)
    record.frozen_until = record.quota_next_reset_at
    record.frozen_at = record.quota_next_reset_at
    record.freeze_reason = "测试冻结"
    record.tokens_used = 100
    session.add(record)
    await session.commit()

    dummy_service.restore_calls.clear()

    update_resp = await client.put(
        f"/nodes/{node.id}/apikeys/{key_id}",
        json={"enabled": True, "priority": 2},
    )
    assert update_resp.status_code == 200
    updated = update_resp.json()
    assert updated["enabled"] is True
    assert updated["priority"] == 2
    assert updated["frozen_until"] is not None
    assert updated["freeze_reason"] == "测试冻结"
    assert updated["tokens_used"] == 100
    assert dummy_service.restore_calls == []


@pytest.mark.asyncio
async def test_node_apikey_update_disabled_key_reenable_still_resets(api_client):
    """禁用→启用的显式切换仍重置痕迹（保持原有语义不受本次修复影响）"""
    client, dummy_service, session = api_client
    node = await _create_node(session)

    create_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={"api_key": "sk-disabled-reset", "priority": 1, "verify": False},
    )
    key_id = UUID(create_resp.json()["id"])

    await increment_node_api_key_tokens_used(session=session, api_key_id=key_id, delta=30)
    await disable_node_api_key(
        session=session,
        api_key_id=key_id,
        reason="下游限额错误: rate limit exceeded",
        disabled_at=current_time_in_timezone(),
    )

    dummy_service.restore_calls.clear()

    update_resp = await client.put(
        f"/nodes/{node.id}/apikeys/{key_id}",
        json={"enabled": True},
    )
    assert update_resp.status_code == 200
    updated = update_resp.json()
    assert updated["enabled"] is True
    assert updated["disabled_at"] is None
    assert updated["disable_reason"] is None
    assert updated["tokens_used"] == 0
    # 状态切换触发运行时恢复
    assert dummy_service.restore_calls == [key_id]


@pytest.mark.asyncio
async def test_node_apikey_switching_to_no_reset_cycle_clears_quota_state(api_client):
    """切换为无周期时清理冻结和重置时间，避免残留状态继续阻塞密钥。"""
    client, _, session = api_client
    node = await _create_node(session)

    create_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={
            "api_key": "sk-cycle-switch",
            "quota_reset_cycle": "daily",
            "quota_next_reset_at": "2099-01-02T00:00:00Z",
            "verify": False,
        },
    )
    assert create_resp.status_code == 200
    key_id = UUID(create_resp.json()["id"])

    record = await select_node_api_key_by_id(key_id, session=session)
    record.frozen_until = record.quota_next_reset_at
    record.frozen_at = record.quota_next_reset_at
    record.freeze_reason = "测试冻结"
    record.tokens_used = 100
    session.add(record)
    await session.commit()

    update_resp = await client.put(
        f"/nodes/{node.id}/apikeys/{key_id}",
        json={"quota_reset_cycle": "none"},
    )

    assert update_resp.status_code == 200
    updated = update_resp.json()
    assert updated["quota_reset_cycle"] == "none"
    assert updated["quota_next_reset_at"] is None
    assert updated["frozen_until"] is None
    assert updated["frozen_at"] is None
    assert updated["freeze_reason"] is None
    assert updated["tokens_used"] == 0


@pytest.mark.asyncio
async def test_node_apikey_manual_disable_keeps_no_reason(api_client):
    """手动禁用（enabled=false）不记录 disabled_at/disable_reason，区别于自动禁用"""
    client, _, session = api_client
    node = await _create_node(session)

    create_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={"api_key": "sk-manual", "priority": 1, "verify": False},
    )
    key_id = UUID(create_resp.json()["id"])

    disable_resp = await client.put(
        f"/nodes/{node.id}/apikeys/{key_id}",
        json={"enabled": False},
    )
    assert disable_resp.status_code == 200
    disabled = disable_resp.json()
    assert disabled["enabled"] is False
    assert disabled["disabled_at"] is None
    assert disabled["disable_reason"] is None


@pytest.mark.asyncio
async def test_node_apikey_endpoints_404_for_missing_node_or_key(api_client):
    """节点不存在或密钥不属于该节点时返回 404"""
    client, _, session = api_client
    node = await _create_node(session)
    missing_node_id = uuid4()

    create_missing_node = await client.post(
        f"/nodes/{missing_node_id}/apikeys",
        json={"api_key": "sk-x", "verify": False},
    )
    assert create_missing_node.status_code == 404

    create_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={"api_key": "sk-owned", "verify": False},
    )
    key_id = UUID(create_resp.json()["id"])

    # 密钥存在但挂在其他节点路径下 → 404
    cross_node_detail = await client.get(f"/nodes/{missing_node_id}/apikeys/{key_id}")
    assert cross_node_detail.status_code == 404


@pytest.mark.asyncio
async def test_create_node_apikey_verify_uses_node_runtime_config(api_client, monkeypatch):
    """创建密钥时 verify=True（默认）会按节点运行时配置调用验证逻辑"""
    client, _, session = api_client
    node = await _create_node(session)
    # 配置节点运行时参数，验证时应透传
    node.protocol_type = ProtocolType.anthropic
    node.auto_v1_api = False
    node.request_proxy_url = "https://proxy.example.com:8443"
    await session.commit()

    verify_calls: list[dict[str, object]] = []

    async def fake_verify(**kwargs):
        verify_calls.append(kwargs)

    monkeypatch.setattr("openaiproxy.api.node_apikey._verify_node_protocols", fake_verify)

    create_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={"api_key": "sk-verify", "priority": 1},
    )
    assert create_resp.status_code == 200
    assert len(verify_calls) == 1
    assert verify_calls[0]["node_url"] == node.url
    assert verify_calls[0]["api_key"] == "sk-verify"
    assert verify_calls[0]["protocol_type"] == ProtocolType.anthropic
    assert verify_calls[0]["auto_v1_api"] is False
    assert verify_calls[0]["request_proxy_url"] == "https://proxy.example.com:8443"
    assert verify_calls[0]["verify"] is True
    assert verify_calls[0]["trusted_without_models_endpoint"] is False


@pytest.mark.asyncio
async def test_create_node_apikey_verify_false_skips_verification(api_client, monkeypatch):
    """创建密钥时显式 verify=False 跳过验证"""
    client, _, session = api_client
    node = await _create_node(session)

    verify_calls: list[dict[str, object]] = []

    async def fake_verify(**kwargs):
        verify_calls.append(kwargs)

    monkeypatch.setattr("openaiproxy.api.node_apikey._verify_node_protocols", fake_verify)

    create_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={"api_key": "sk-no-verify", "priority": 1, "verify": False},
    )
    assert create_resp.status_code == 200
    # verify=False 仍会调用封装函数，但内部 _should_verify_models_endpoint 判定跳过
    assert verify_calls[0]["verify"] is False


@pytest.mark.asyncio
async def test_create_node_apikey_verification_failure_blocks_persist(api_client, monkeypatch):
    """验证失败时返回 400 且不落库"""
    client, _, session = api_client
    node = await _create_node(session)

    async def failing_verify(**kwargs):
        raise HTTPException(status_code=400, detail="节点验证失败，/v1/models返回状态码401")

    monkeypatch.setattr("openaiproxy.api.node_apikey._verify_node_protocols", failing_verify)

    create_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={"api_key": "sk-bad", "priority": 1},
    )
    assert create_resp.status_code == 400
    assert "节点验证失败" in create_resp.json()["detail"]

    list_resp = await client.get(f"/nodes/{node.id}/apikeys")
    assert list_resp.status_code == 200
    assert list_resp.json() == []


@pytest.mark.asyncio
async def test_create_node_apikey_trusted_node_skips_verification(api_client, monkeypatch):
    """节点标记 trusted_without_models_endpoint 时跳过验证（与节点创建行为一致）"""
    client, _, session = api_client
    node = await _create_node(session)
    node.trusted_without_models_endpoint = True
    await session.commit()

    verify_calls: list[dict[str, object]] = []

    async def fake_verify(**kwargs):
        verify_calls.append(kwargs)

    monkeypatch.setattr("openaiproxy.api.node_apikey._verify_node_protocols", fake_verify)

    create_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={"api_key": "sk-trusted", "priority": 1},
    )
    assert create_resp.status_code == 200
    assert verify_calls[0]["trusted_without_models_endpoint"] is True


@pytest.mark.asyncio
async def test_update_node_apikey_new_key_triggers_verification(api_client, monkeypatch):
    """更新接口传入新 api_key 时按节点配置验证；不传 api_key 时不验证"""
    client, _, session = api_client
    node = await _create_node(session)

    create_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={"api_key": "sk-old", "priority": 1, "verify": False},
    )
    key_id = UUID(create_resp.json()["id"])

    verify_calls: list[dict[str, object]] = []

    async def fake_verify(**kwargs):
        verify_calls.append(kwargs)

    monkeypatch.setattr("openaiproxy.api.node_apikey._verify_node_protocols", fake_verify)

    # 仅更新优先级 → 不触发验证
    update_resp = await client.put(
        f"/nodes/{node.id}/apikeys/{key_id}",
        json={"priority": 9},
    )
    assert update_resp.status_code == 200
    assert verify_calls == []

    # 更换密钥 → 触发验证，且使用新密钥
    update_key_resp = await client.put(
        f"/nodes/{node.id}/apikeys/{key_id}",
        json={"api_key": "sk-new"},
    )
    assert update_key_resp.status_code == 200
    assert len(verify_calls) == 1
    assert verify_calls[0]["api_key"] == "sk-new"
    assert verify_calls[0]["node_url"] == node.url

    # 更换密钥但 verify=False → 跳过验证
    update_skip_resp = await client.put(
        f"/nodes/{node.id}/apikeys/{key_id}",
        json={"api_key": "sk-newer", "verify": False},
    )
    assert update_skip_resp.status_code == 200
    assert len(verify_calls) == 2
    assert verify_calls[1]["verify"] is False


@pytest.mark.asyncio
async def test_update_node_apikey_verification_failure_blocks_update(api_client, monkeypatch):
    """更新密钥验证失败时返回 400 且原密钥保持不变"""
    client, _, session = api_client
    node = await _create_node(session)

    create_resp = await client.post(
        f"/nodes/{node.id}/apikeys",
        json={"api_key": "sk-keep", "priority": 1, "verify": False},
    )
    key_id = UUID(create_resp.json()["id"])

    async def failing_verify(**kwargs):
        raise HTTPException(status_code=400, detail="节点验证失败，无法访问/v1/models接口")

    monkeypatch.setattr("openaiproxy.api.node_apikey._verify_node_protocols", failing_verify)

    update_resp = await client.put(
        f"/nodes/{node.id}/apikeys/{key_id}",
        json={"api_key": "sk-broken"},
    )
    assert update_resp.status_code == 400

    # 原密钥未被替换
    stored = await select_node_api_key_by_id(key_id, session=session)
    assert decrypt_api_key(stored.api_key) == "sk-keep"
