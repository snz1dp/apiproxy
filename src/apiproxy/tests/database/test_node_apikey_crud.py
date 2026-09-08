"""节点独立API密钥 CRUD 集成测试（依赖真实数据库会话）"""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from openaiproxy.services.database.models.node.crud import (
    count_node_api_keys_by_node_id,
    create_node_api_key_record,
    delete_node_api_key_record,
    disable_node_api_key,
    enable_node_api_key,
    increment_node_api_key_tokens_used,
    select_active_node_api_keys,
    select_node_api_key_by_hash,
    select_node_api_key_by_id,
    select_node_api_keys_by_node_id,
    update_node_api_key_record,
)
from openaiproxy.services.database.models.node.model import Node, NodeApiKey
from openaiproxy.utils.timezone import current_time_in_timezone


def _make_payload(node_id, api_key: str, **overrides) -> dict:
    """构造 NodeApiKey 创建 payload（api_key 此处直接存明文占位）"""
    payload = {
        "node_id": node_id,
        "name": f"key-{api_key[-6:]}",
        "api_key": f"encrypted::{api_key}",
        "api_key_hash": f"hash-{api_key}",
        "priority": 1,
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
async def node_with_keys(session):
    """创建测试节点与三个不同配置的密钥"""
    node = Node(url=f"http://apikey-node-{uuid4().hex[:8]}.example.com", name="apikey-node")
    session.add(node)
    await session.commit()
    await session.refresh(node)

    now = datetime.now().astimezone()
    normal = await create_node_api_key_record(
        session=session, payload=_make_payload(node.id, "sk-normal")
    )
    zero_priority = await create_node_api_key_record(
        session=session, payload=_make_payload(node.id, "sk-zero", priority=0)
    )
    expiring = await create_node_api_key_record(
        session=session,
        payload=_make_payload(node.id, "sk-expiring", expires_at=now + timedelta(days=7)),
    )
    expired = await create_node_api_key_record(
        session=session,
        payload=_make_payload(node.id, "sk-expired", expires_at=now - timedelta(days=1)),
    )
    capped = await create_node_api_key_record(
        session=session,
        payload=_make_payload(node.id, "sk-capped", max_tokens=100, tokens_used=100),
    )
    disabled = await create_node_api_key_record(
        session=session, payload=_make_payload(node.id, "sk-disabled", enabled=False)
    )
    return {
        "node": node,
        "normal": normal,
        "zero_priority": zero_priority,
        "expiring": expiring,
        "expired": expired,
        "capped": capped,
        "disabled": disabled,
    }


async def test_create_and_query_by_hash(session, node_with_keys):
    """创建后可按 ID 与 node_id+hash 查询，密文与哈希持久化一致"""
    node = node_with_keys["node"]
    normal = node_with_keys["normal"]

    fetched = await select_node_api_key_by_id(normal.id, session=session)
    assert fetched is not None
    assert fetched.api_key == normal.api_key
    assert fetched.priority == 1
    assert fetched.tokens_used == 0
    assert fetched.enabled is True

    by_hash = await select_node_api_key_by_hash(
        node_id=node.id, api_key_hash="hash-sk-normal", session=session
    )
    assert by_hash is not None
    assert by_hash.id == normal.id


async def test_list_and_count_by_node(session, node_with_keys):
    """列表查询与计数：全量 6 条，启用 5 条"""
    node = node_with_keys["node"]

    all_keys = await select_node_api_keys_by_node_id(node_id=node.id, session=session)
    assert len(all_keys) == 6

    enabled_keys = await select_node_api_keys_by_node_id(
        node_id=node.id, enabled=True, session=session
    )
    assert len(enabled_keys) == 5

    total = await count_node_api_keys_by_node_id(node_id=node.id, session=session)
    assert total == 6


async def test_select_active_excludes_expired_overquota_disabled(session, node_with_keys):
    """可用密钥筛选：排除过期、超额、禁用；保留 priority=0（由运行时选择层处理）"""
    node = node_with_keys["node"]

    active = await select_active_node_api_keys(node_id=node.id, session=session)
    active_ids = {record.id for record in active}

    assert node_with_keys["normal"].id in active_ids
    assert node_with_keys["zero_priority"].id in active_ids
    assert node_with_keys["expiring"].id in active_ids
    assert node_with_keys["expired"].id not in active_ids
    assert node_with_keys["capped"].id not in active_ids
    assert node_with_keys["disabled"].id not in active_ids


async def test_increment_tokens_used_accumulates(session, node_with_keys):
    """tokens_used 原子累加"""
    normal = node_with_keys["normal"]

    await increment_node_api_key_tokens_used(
        session=session, api_key_id=normal.id, delta=30
    )
    await increment_node_api_key_tokens_used(
        session=session, api_key_id=normal.id, delta=45
    )
    refreshed = await select_node_api_key_by_id(normal.id, session=session)
    assert refreshed.tokens_used == 75

    # delta<=0 不产生写入
    await increment_node_api_key_tokens_used(
        session=session, api_key_id=normal.id, delta=0
    )
    refreshed = await select_node_api_key_by_id(normal.id, session=session)
    assert refreshed.tokens_used == 75


async def test_disable_then_enable_resets_usage(session, node_with_keys):
    """自动禁用记录原因与时间；重新启用清空痕迹并重置 Tokens"""
    normal = node_with_keys["normal"]
    await increment_node_api_key_tokens_used(
        session=session, api_key_id=normal.id, delta=10
    )

    disabled_at = current_time_in_timezone()
    await disable_node_api_key(
        session=session,
        api_key_id=normal.id,
        reason="已用Tokens达到上限",
        disabled_at=disabled_at,
    )
    record = await select_node_api_key_by_id(normal.id, session=session)
    assert record.enabled is False
    assert record.disable_reason == "已用Tokens达到上限"
    assert record.disabled_at is not None

    await enable_node_api_key(session=session, api_key_id=normal.id)
    record = await select_node_api_key_by_id(normal.id, session=session)
    assert record.enabled is True
    assert record.disable_reason is None
    assert record.disabled_at is None
    assert record.tokens_used == 0


async def test_update_and_delete_record(session, node_with_keys):
    """更新字段与删除记录"""
    expiring = node_with_keys["expiring"]

    updated = await update_node_api_key_record(
        session=session,
        record=expiring,
        update_payload={"priority": 9, "max_tokens": 5000},
        updated_at=current_time_in_timezone(),
    )
    assert updated.priority == 9
    assert updated.max_tokens == 5000

    await delete_node_api_key_record(session=session, record=updated)
    gone = await select_node_api_key_by_id(expiring.id, session=session)
    assert gone is None


async def test_unique_constraint_node_hash(session, node_with_keys):
    """同节点重复 hash 触发唯一约束"""
    from sqlalchemy.exc import IntegrityError

    node = node_with_keys["node"]
    with pytest.raises(IntegrityError):
        await create_node_api_key_record(
            session=session, payload=_make_payload(node.id, "sk-normal")
        )
    await session.rollback()
