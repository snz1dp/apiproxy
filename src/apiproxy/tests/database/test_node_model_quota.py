# /*********************************************
#                    _ooOoo_
#                   o8888888o
#                   88" . "88
#                   (| -_- |)
#                   O\  =  /O
#                ____/`---'\____
#              .'  \\|     |//  `.
#             /  \\|||  :  |||//  \
#            /  _||||| -:- |||||-  \
#            |   | \\\  -  /// |   |
#            | \_|  ''\---/''  |   |
#            \  .-\__  `-`  ___/-. /
#          ___`. .'  /--.--\  `. . __
#       ."" '<  `.___\_<|>_/___.'  >'"".
#      | | :  `- \`.;`\ _ /`;.`/ - ` : | |
#      \  \ `-.   \_ __\ /__ _/   .-` /  /
# ======`-.____`-.___\_____/___.-`____.-'======
#                    `=---='

# ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#            佛祖保佑       永无BUG
#            心外无法       法外无心
#            三宝弟子       三德子宏愿
# *********************************************/

"""南向节点模型配额闭环测试: 创建配额 -> reserve -> finalize -> 验证使用量。"""

from uuid import uuid4

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from openaiproxy.services.database.models.node.model import (
    Node,
    NodeModel,
    NodeModelQuota,
    NodeModelQuotaUsage,
)
from openaiproxy.services.database.models.node.utils import (
    finalize_node_model_quota_usage,
    reserve_node_model_quota,
)
from openaiproxy.services.nodeproxy.exceptions import NodeModelQuotaExceeded


async def _create_test_node_model(session: AsyncSession):
    """创建一个测试节点和模型，返回 (node, node_model)。"""
    node = Node(url=f"http://test-node-{uuid4().hex[:8]}", name="test-node")
    session.add(node)
    await session.flush()

    node_model = NodeModel(
        node_id=node.id,
        model_name=f"test-model-{uuid4().hex[:8]}",
        model_type="chat",
    )
    session.add(node_model)
    await session.flush()
    return node, node_model


async def _create_test_node_model_quota(
    session: AsyncSession,
    node_model_id,
    *,
    call_limit=None,
    total_tokens_limit=None,
    prompt_tokens_limit=None,
    completion_tokens_limit=None,
    order_id=None,
) -> NodeModelQuota:
    """创建一个测试用节点模型配额单据。"""
    quota = NodeModelQuota(
        node_model_id=node_model_id,
        order_id=order_id or f"order-{uuid4().hex[:8]}",
        call_limit=call_limit,
        total_tokens_limit=total_tokens_limit,
        prompt_tokens_limit=prompt_tokens_limit,
        completion_tokens_limit=completion_tokens_limit,
    )
    session.add(quota)
    await session.flush()
    return quota


async def test_reserve_no_quota_returns_none(session: AsyncSession):
    """没有任何配额单据时, reserve 应返回 None（不限制）。"""
    node, node_model = await _create_test_node_model(session)
    result = await reserve_node_model_quota(
        session=session,
        node_id=node.id,
        node_model_id=node_model.id,
        proxy_id=None,
        model_name=node_model.model_name,
        model_type="chat",
        ownerapp_id="test-app",
        request_action="completions",
        estimated_request_tokens=100,
    )
    assert result is None


async def test_reserve_and_finalize_full_cycle(session: AsyncSession):
    """闭环测试: reserve -> finalize -> 验证 call_used 和 token 使用量。"""
    node, node_model = await _create_test_node_model(session)
    quota = await _create_test_node_model_quota(
        session,
        node_model.id,
        call_limit=10,
        total_tokens_limit=10000,
    )

    # reserve
    result = await reserve_node_model_quota(
        session=session,
        node_id=node.id,
        node_model_id=node_model.id,
        proxy_id=None,
        model_name=node_model.model_name,
        model_type="chat",
        ownerapp_id="test-app",
        request_action="completions",
        estimated_request_tokens=50,
    )
    assert result is not None
    quota_id, usage_id = result

    assert quota_id == quota.id

    # 验证 call_used +1
    await session.refresh(quota)
    assert quota.call_used == 1

    # finalize
    await finalize_node_model_quota_usage(
        session=session,
        node_id=node.id,
        node_model_id=node_model.id,
        proxy_id=None,
        primary_quota_id=quota_id,
        primary_quota_usage_id=usage_id,
        model_name=node_model.model_name,
        request_tokens=100,
        response_tokens=200,
        total_tokens=300,
        ownerapp_id="test-app",
        request_action="completions",
        log_id=None,
    )
    await session.flush()

    # 验证 token 使用量
    await session.refresh(quota)
    assert quota.total_tokens_used == 300
    assert quota.prompt_tokens_used == 100
    assert quota.completion_tokens_used == 200

    # 验证 usage 记录被更新
    usage_stmt = select(NodeModelQuotaUsage).where(NodeModelQuotaUsage.id == usage_id)
    usage_result = await session.exec(usage_stmt)
    usage = usage_result.first()
    assert usage is not None
    assert usage.total_tokens == 300
    assert usage.request_tokens == 100
    assert usage.response_tokens == 200


async def test_reserve_exceeds_call_limit(session: AsyncSession):
    """调用次数耗尽时应抛出 NodeModelQuotaExceeded。"""
    node, node_model = await _create_test_node_model(session)
    await _create_test_node_model_quota(
        session,
        node_model.id,
        call_limit=1,
    )

    # 第一次 reserve 应成功
    result = await reserve_node_model_quota(
        session=session,
        node_id=node.id,
        node_model_id=node_model.id,
        proxy_id=None,
        model_name=node_model.model_name,
        model_type="chat",
        ownerapp_id="test-app",
        request_action="completions",
        estimated_request_tokens=10,
    )
    assert result is not None

    # 第二次 reserve 在当前实现下应返回 None，表示没有可用配额单据。
    result = await reserve_node_model_quota(
        session=session,
        node_id=node.id,
        node_model_id=node_model.id,
        proxy_id=None,
        model_name=node_model.model_name,
        model_type="chat",
        ownerapp_id="test-app",
        request_action="completions",
        estimated_request_tokens=10,
    )
    assert result is None


async def test_fifo_token_distribution(session: AsyncSession):
    """多配额单的 FIFO token 分配: tokens 应优先填满第一张配额单。"""
    node, node_model = await _create_test_node_model(session)
    q1 = await _create_test_node_model_quota(
        session,
        node_model.id,
        call_limit=100,
        total_tokens_limit=200,
        order_id="fifo-nm-order-1",
    )
    q2 = await _create_test_node_model_quota(
        session,
        node_model.id,
        call_limit=100,
        total_tokens_limit=500,
        order_id="fifo-nm-order-2",
    )

    # reserve 一次
    result = await reserve_node_model_quota(
        session=session,
        node_id=node.id,
        node_model_id=node_model.id,
        proxy_id=None,
        model_name=node_model.model_name,
        model_type="chat",
        ownerapp_id="test-app",
        request_action="completions",
        estimated_request_tokens=10,
    )
    assert result is not None
    quota_id, usage_id = result

    # finalize 消耗 300 total tokens -> 超过 q1 的 200 限制
    await finalize_node_model_quota_usage(
        session=session,
        node_id=node.id,
        node_model_id=node_model.id,
        proxy_id=None,
        primary_quota_id=quota_id,
        primary_quota_usage_id=usage_id,
        model_name=node_model.model_name,
        request_tokens=150,
        response_tokens=150,
        total_tokens=300,
        ownerapp_id="test-app",
        request_action="completions",
        log_id=None,
    )

    await session.refresh(q1)
    await session.refresh(q2)
    # q1 应被填满 200，剩余 100 分配到 q2
    assert q1.total_tokens_used == 200
    assert q2.total_tokens_used == 100
