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

"""应用配额闭环测试: 创建配额 -> reserve -> finalize -> 验证使用量。"""

from uuid import uuid4

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from openaiproxy.services.database.models.app.model import AppQuota, AppQuotaUsage
from openaiproxy.services.database.models.app.utils import (
    finalize_app_quota_usage,
    reserve_app_quota,
)
from openaiproxy.services.nodeproxy.exceptions import AppQuotaExceeded


async def _create_test_app_quota(
    session: AsyncSession,
    ownerapp_id: str,
    *,
    call_limit=None,
    total_tokens_limit=None,
    order_id=None,
) -> AppQuota:
    """创建一个测试用应用配额单据。"""
    quota = AppQuota(
        ownerapp_id=ownerapp_id,
        order_id=order_id or f"order-{uuid4().hex[:8]}",
        call_limit=call_limit,
        total_tokens_limit=total_tokens_limit,
    )
    session.add(quota)
    await session.flush()
    return quota


async def test_reserve_no_quota_returns_none(session: AsyncSession):
    """没有任何配额单据时, reserve 应返回 None（不限制）。"""
    ownerapp_id = f"app-{uuid4().hex[:8]}"
    result = await reserve_app_quota(
        session=session,
        ownerapp_id=ownerapp_id,
        api_key_id=None,
        proxy_id=None,
        model_name="gpt-4",
        request_action="completions",
    )
    assert result is None


async def test_reserve_and_finalize_full_cycle(session: AsyncSession):
    """闭环测试: reserve -> finalize -> 验证 call_used 和 total_tokens_used。"""
    ownerapp_id = f"app-{uuid4().hex[:8]}"
    quota = await _create_test_app_quota(
        session,
        ownerapp_id,
        call_limit=10,
        total_tokens_limit=10000,
    )

    # reserve
    result = await reserve_app_quota(
        session=session,
        ownerapp_id=ownerapp_id,
        api_key_id=uuid4(),
        proxy_id=None,
        model_name="gpt-4",
        request_action="completions",
    )
    assert result is not None
    quota_id, usage_id = result

    assert quota_id == quota.id

    # 验证 call_used +1
    await session.refresh(quota)
    assert quota.call_used == 1

    # finalize
    await finalize_app_quota_usage(
        session=session,
        ownerapp_id=ownerapp_id,
        primary_quota_id=quota_id,
        primary_quota_usage_id=usage_id,
        total_tokens=800,
        api_key_id=uuid4(),
        model_name="gpt-4",
        request_action="completions",
        log_id=None,
    )

    # 验证 total_tokens_used
    await session.refresh(quota)
    assert quota.total_tokens_used == 800

    # 验证 usage 记录被更新
    usage_stmt = select(AppQuotaUsage).where(AppQuotaUsage.id == usage_id)
    usage_result = await session.exec(usage_stmt)
    usage = usage_result.first()
    assert usage is not None
    assert usage.total_tokens == 800
    assert usage.call_count == 1


async def test_reserve_exceeds_call_limit(session: AsyncSession):
    """调用次数耗尽时应抛出 AppQuotaExceeded。"""
    ownerapp_id = f"app-{uuid4().hex[:8]}"
    await _create_test_app_quota(
        session,
        ownerapp_id,
        call_limit=1,
        total_tokens_limit=None,
    )

    # 第一次 reserve 应成功
    result = await reserve_app_quota(
        session=session,
        ownerapp_id=ownerapp_id,
        api_key_id=uuid4(),
        proxy_id=None,
        model_name="gpt-4",
        request_action="completions",
    )
    assert result is not None

    # 第二次 reserve 应抛出 AppQuotaExceeded
    with pytest.raises(AppQuotaExceeded):
        await reserve_app_quota(
            session=session,
            ownerapp_id=ownerapp_id,
            api_key_id=uuid4(),
            proxy_id=None,
            model_name="gpt-4",
            request_action="completions",
        )


async def test_reserve_rejects_when_estimated_tokens_exceed_capacity(session: AsyncSession):
    """预占时若估算 token 超过总剩余额度，应直接拒绝。"""
    ownerapp_id = f"app-{uuid4().hex[:8]}"
    await _create_test_app_quota(
        session,
        ownerapp_id,
        call_limit=10,
        total_tokens_limit=100,
    )

    with pytest.raises(AppQuotaExceeded):
        await reserve_app_quota(
            session=session,
            ownerapp_id=ownerapp_id,
            api_key_id=uuid4(),
            proxy_id=None,
            model_name="gpt-4",
            request_action="completions",
            estimated_total_tokens=101,
        )


async def test_fifo_token_distribution(session: AsyncSession):
    """多配额单的 FIFO token 分配: tokens 应优先填满第一张配额单。"""
    ownerapp_id = f"app-{uuid4().hex[:8]}"
    q1 = await _create_test_app_quota(
        session,
        ownerapp_id,
        call_limit=100,
        total_tokens_limit=200,
        order_id="fifo-app-order-1",
    )
    q2 = await _create_test_app_quota(
        session,
        ownerapp_id,
        call_limit=100,
        total_tokens_limit=500,
        order_id="fifo-app-order-2",
    )

    # reserve 一次
    result = await reserve_app_quota(
        session=session,
        ownerapp_id=ownerapp_id,
        api_key_id=uuid4(),
        proxy_id=None,
        model_name="gpt-4",
        request_action="completions",
    )
    assert result is not None
    quota_id, usage_id = result

    # finalize 消耗 300 tokens -> 超过 q1 的 200 限制
    await finalize_app_quota_usage(
        session=session,
        ownerapp_id=ownerapp_id,
        primary_quota_id=quota_id,
        primary_quota_usage_id=usage_id,
        total_tokens=300,
        api_key_id=uuid4(),
        model_name="gpt-4",
        request_action="completions",
        log_id=None,
    )

    await session.refresh(q1)
    await session.refresh(q2)
    # q1 应被填满 200，剩余 100 分配到 q2
    assert q1.total_tokens_used == 200
    assert q2.total_tokens_used == 100


async def test_finalize_raises_when_tokens_exceed_total_capacity(session: AsyncSession):
    """结算时若实际 token 超过总容量，必须显式失败而不是少记账。"""
    ownerapp_id = f"app-{uuid4().hex[:8]}"
    quota = await _create_test_app_quota(
        session,
        ownerapp_id,
        call_limit=100,
        total_tokens_limit=200,
        order_id="overflow-app-order",
    )

    result = await reserve_app_quota(
        session=session,
        ownerapp_id=ownerapp_id,
        api_key_id=uuid4(),
        proxy_id=None,
        model_name="gpt-4",
        request_action="completions",
        estimated_total_tokens=200,
    )
    assert result is not None
    quota_id, usage_id = result

    with pytest.raises(AppQuotaExceeded):
        await finalize_app_quota_usage(
            session=session,
            ownerapp_id=ownerapp_id,
            primary_quota_id=quota_id,
            primary_quota_usage_id=usage_id,
            total_tokens=300,
            api_key_id=uuid4(),
            model_name="gpt-4",
            request_action="completions",
            log_id=None,
        )

    await session.refresh(quota)
    assert quota.total_tokens_used == 200
