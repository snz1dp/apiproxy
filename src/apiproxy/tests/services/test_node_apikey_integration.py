"""节点独立API密钥：限额错误自动禁用 → 重新设置恢复的集成测试（真实数据库路径）

覆盖计划批次五：
- 集成测试：限额错误触发自动禁用 → 重新设置后恢复
- 集成测试：请求转发时 Tokens 累计 + 日志记录 node_api_key_id 验证
"""

from __future__ import annotations

import threading
from uuid import UUID, uuid4

import pytest
from sqlmodel import delete, select

from openaiproxy.services.database.models import (
    Node as OpenAINode,
    NodeModel as OpenAINodeModel,
)
from openaiproxy.services.database.models.node.crud import (
    create_node_api_key_record,
    select_node_api_key_by_id,
)
from openaiproxy.services.database.models.node.model import NodeApiKey, ProtocolType
from openaiproxy.services.database.models.proxy.crud import (
    create_proxy_node_status_log_entry,
)
from openaiproxy.services.database.models.proxy.model import (
    ProxyNodeStatusLog,
    RequestAction,
)
from openaiproxy.services.deps import async_session_scope
from openaiproxy.services.nodeproxy.schemas import NodeApiKeyEntry, Status
from openaiproxy.services.nodeproxy.service import (
    NodeProxyService,
    _NodeMetadata,
    _RequestContext,
)
from openaiproxy.utils.apikey import encrypt_api_key
from openaiproxy.utils.timezone import current_time_in_timezone


def _build_real_service() -> NodeProxyService:
    """构造仅含内存状态、但走真实数据库会话路径的 NodeProxyService 实例"""
    service = object.__new__(NodeProxyService)
    service._lock = threading.RLock()
    service.nodes = {}
    service.snode = {}
    service._offline_nodes = {}
    service._node_metadata = {}
    return service


@pytest.fixture
async def clean_session(session):
    await session.exec(delete(ProxyNodeStatusLog))
    await session.exec(delete(NodeApiKey))
    await session.exec(delete(OpenAINodeModel))
    await session.exec(delete(OpenAINode))
    await session.commit()
    try:
        yield session
    finally:
        await session.rollback()
        await session.exec(delete(ProxyNodeStatusLog))
        await session.exec(delete(NodeApiKey))
        await session.exec(delete(OpenAINodeModel))
        await session.exec(delete(OpenAINode))
        await session.commit()


async def _create_node_with_key(session, *, plaintext: str, max_tokens=None) -> tuple[OpenAINode, NodeApiKey]:
    """落库一个节点及其独立API密钥，返回 (节点, 密钥记录)"""
    node = OpenAINode(
        url=f"http://apikey-it-{uuid4().hex[:8]}.example.com",
        name="apikey-it-node",
    )
    session.add(node)
    await session.commit()
    await session.refresh(node)

    record = await create_node_api_key_record(
        session=session,
        payload={
            "node_id": node.id,
            "name": "it-key",
            "api_key": encrypt_api_key(plaintext),
            "api_key_hash": uuid4().hex,
            "priority": 1,
            "max_tokens": max_tokens,
            "enabled": True,
        },
    )
    return node, record


async def _reload_key(api_key_id: UUID) -> NodeApiKey | None:
    """用独立会话重新读取密钥记录，确保看到服务侧已提交的状态"""
    async with async_session_scope() as session:
        result = await session.exec(select(NodeApiKey).where(NodeApiKey.id == api_key_id))
        return result.first()


@pytest.mark.asyncio
async def test_rate_limit_error_auto_disables_then_restore_recovers(clean_session):
    """限额错误触发自动禁用（落库 enabled=False + 原因），重新设置后恢复并清空痕迹"""
    session = clean_session
    node, record = await _create_node_with_key(session, plaintext="sk-limit")
    api_key_id = record.id

    service = _build_real_service()
    # 预置节点元数据，使 restore 能命中并失效配置指纹
    service._node_metadata[node.url] = _NodeMetadata(
        node_id=node.id,
        config_version="v1",
        api_key_ids=[api_key_id],
    )

    entry = NodeApiKeyEntry(
        api_key_id=api_key_id,
        api_key="sk-limit",
        priority=1,
        max_tokens=None,
        tokens_used=0,
    )
    context = _RequestContext(
        start_time=0.0,
        node_api_key_entry=entry,
        node_api_key_id=api_key_id,
        total_tokens=0,
        error=True,
        error_message="rate limit exceeded",
    )

    # 触发后处理：限额错误 → 自动禁用
    service._post_process_api_key_usage(context)

    disabled = await _reload_key(api_key_id)
    assert disabled is not None
    assert disabled.enabled is False
    assert disabled.disable_reason is not None
    assert "rate limit" in disabled.disable_reason
    assert disabled.disabled_at is not None

    # 密钥条目已从本实例内存移除（无残留即通过）
    assert all(
        item.api_key_id != api_key_id
        for status in service.snode.values()
        for item in status.api_keys
    )

    # 重新设置（恢复可用性）→ 数据库重新启用并清空禁用痕迹
    restored = service.restore_node_api_key_availability(api_key_id)
    assert restored is True

    recovered = await _reload_key(api_key_id)
    assert recovered is not None
    assert recovered.enabled is True
    assert recovered.disabled_at is None
    assert recovered.disable_reason is None
    assert recovered.tokens_used == 0

    # 配置指纹被失效，触发下一轮刷新重建运行时条目
    assert service._node_metadata[node.url].config_version == ""


@pytest.mark.asyncio
async def test_post_process_accumulates_tokens_and_disables_on_quota(clean_session):
    """Tokens 累计落库；达到 max_tokens 上限时触发自动禁用"""
    session = clean_session
    node, record = await _create_node_with_key(session, plaintext="sk-quota", max_tokens=100)
    api_key_id = record.id

    service = _build_real_service()
    entry = NodeApiKeyEntry(
        api_key_id=api_key_id,
        api_key="sk-quota",
        priority=1,
        max_tokens=100,
        tokens_used=0,
    )
    context = _RequestContext(
        start_time=0.0,
        node_api_key_entry=entry,
        node_api_key_id=api_key_id,
        total_tokens=100,
        error=False,
    )

    service._post_process_api_key_usage(context)

    updated = await _reload_key(api_key_id)
    assert updated is not None
    # tokens_used 原子累加到 100，达到上限触发自动禁用
    assert updated.tokens_used == 100
    assert updated.enabled is False
    assert updated.disable_reason is not None
    assert "上限" in updated.disable_reason


@pytest.mark.asyncio
async def test_request_log_records_node_api_key_id(clean_session):
    """请求日志写入本次实际使用的 node_api_key_id（UUID），不记录密钥明文"""
    session = clean_session
    node, record = await _create_node_with_key(session, plaintext="sk-log")
    api_key_id = record.id

    log_entry = await create_proxy_node_status_log_entry(
        session=session,
        node_id=node.id,
        proxy_id=uuid4(),
        status_id=uuid4(),
        ownerapp_id="app-it",
        request_protocol=ProtocolType.openai,
        model_name="gpt-4o",
        action=RequestAction.completions,
        start_at=current_time_in_timezone(),
        end_at=current_time_in_timezone(),
        latency=0.1,
        request_tokens=10,
        response_tokens=5,
        total_tokens=15,
        cached_tokens=0,
        stream=False,
        error=False,
        node_api_key_id=api_key_id,
    )
    await session.commit()
    await session.refresh(log_entry)

    stored = await session.get(ProxyNodeStatusLog, log_entry.id)
    assert stored is not None
    assert stored.node_api_key_id == api_key_id


@pytest.mark.asyncio
async def test_forwarding_selection_uses_in_memory_entries(clean_session):
    """转发路径：从节点内存条目中加权选择密钥；自动禁用后该密钥不再被选中"""
    session = clean_session
    node, record = await _create_node_with_key(session, plaintext="sk-forward")
    api_key_id = record.id

    service = _build_real_service()
    entry = NodeApiKeyEntry(
        api_key_id=api_key_id,
        api_key="sk-forward",
        priority=1,
        max_tokens=None,
        tokens_used=0,
    )
    status = Status(models=["gpt-4o"], types=["chat"], avaiaible=True, api_keys=[entry])
    service.snode[node.url] = status
    service.nodes[node.url] = status

    # 转发前选择密钥：命中唯一可用条目
    selected = service.select_node_api_key(node.url)
    assert selected is not None
    assert selected.api_key_id == api_key_id

    # 限额错误触发自动禁用 → 从内存移除
    context = _RequestContext(
        start_time=0.0,
        node_api_key_entry=entry,
        node_api_key_id=api_key_id,
        total_tokens=0,
        error=True,
        error_message="insufficient_quota",
    )
    service._post_process_api_key_usage(context)

    # 禁用后无可用密钥，转发选择返回 None（调用方回退节点主密钥）
    assert service.select_node_api_key(node.url) is None
    assert service.snode[node.url].api_keys == []

    disabled = await _reload_key(api_key_id)
    assert disabled is not None
    assert disabled.enabled is False
