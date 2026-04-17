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

"""API Key 配额闭环测试: 创建配额 -> reserve -> finalize -> 验证使用量。"""

from uuid import uuid4

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from openaiproxy.services.database.models.apikey.model import (
    ApiKey,
    ApiKeyQuota,
    ApiKeyQuotaUsage,
)
from openaiproxy.services.database.models.apikey.utils import (
    finalize_apikey_quota_usage,
    reserve_apikey_quota,
)
from openaiproxy.services.nodeproxy.exceptions import ApiKeyQuotaExceeded
from openaiproxy.utils.timezone import current_time_in_timezone


async def _create_test_apikey(session: AsyncSession) -> ApiKey:
    """创建一个测试用 API Key 记录。"""
    apikey = ApiKey(
        name=f"test-key-{uuid4().hex[:8]}",
        key_hash=uuid4().hex,
        key_prefix="tk_test",
        ownerapp_id=f"app-{uuid4().hex[:8]}",
        created_at=current_time_in_timezone(),
    )
    session.add(apikey)
    await session.flush()
    return apikey


async def _create_test_quota(
    session: AsyncSession,
    api_key_id,
    *,
    call_limit=None,
    total_tokens_limit=None,
    order_id=None,
) -> ApiKeyQuota:
    """创建一个测试用配额单据。"""
    quota = ApiKeyQuota(
        api_key_id=api_key_id,
        order_id=order_id or f"order-{uuid4().hex[:8]}",
        call_limit=call_limit,
        total_tokens_limit=total_tokens_limit,
    )
    session.add(quota)
    await session.flush()
    return quota


async def test_reserve_no_quota_returns_none(session: AsyncSession):
    """没有任何配额单据时, reserve 应返回 None（不限制）。"""
    apikey = await _create_test_apikey(session)
    result = await reserve_apikey_quota(
        session=session,
        api_key_id=apikey.id,
        proxy_id=None,
        ownerapp_id=apikey.ownerapp_id,
        model_name="gpt-4",
        request_action="completions",
    )
    assert result is None


async def test_reserve_and_finalize_full_cycle(session: AsyncSession):
    """闭环测试: reserve -> finalize -> 验证 call_used 和 total_tokens_used。"""
    apikey = await _create_test_apikey(session)
    quota = await _create_test_quota(
        session,
        apikey.id,
        call_limit=10,
        total_tokens_limit=10000,
    )

    # reserve
    result = await reserve_apikey_quota(
        session=session,
        api_key_id=apikey.id,
        proxy_id=None,
        ownerapp_id=apikey.ownerapp_id,
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
    await finalize_apikey_quota_usage(
        session=session,
        api_key_id=apikey.id,
        primary_quota_id=quota_id,
        primary_quota_usage_id=usage_id,
        total_tokens=500,
        ownerapp_id=apikey.ownerapp_id,
        model_name="gpt-4",
        request_action="completions",
        log_id=uuid4(),
        log_id=None,
    )

    # 验证 total_tokens_used
    await session.refresh(quota)
    assert quota.total_tokens_used == 500

    # 验证 usage 记录被更新
    usage_stmt = select(ApiKeyQuotaUsage).where(ApiKeyQuotaUsage.id == usage_id)
    usage_result = await session.exec(usage_stmt)
    usage = usage_result.first()
    assert usage is not None
    assert usage.total_tokens == 500
    assert usage.call_count == 1


async def test_reserve_exceeds_call_limit(session: AsyncSession):
    """调用次数耗尽时应抛出 ApiKeyQuotaExceeded。"""
    apikey = await _create_test_apikey(session)
    await _create_test_quota(
        session,
        apikey.id,
        call_limit=1,
        total_tokens_limit=None,
    )

    # 第一次 reserve 应成功
    result = await reserve_apikey_quota(
        session=session,
        api_key_id=apikey.id,
        proxy_id=None,
        ownerapp_id=apikey.ownerapp_id,
        model_name="gpt-4",
        request_action="completions",
    )
    assert result is not None

    # 第二次 reserve 应抛出 ApiKeyQuotaExceeded
    with pytest.raises(ApiKeyQuotaExceeded):
        await reserve_apikey_quota(
            session=session,
            api_key_id=apikey.id,
            proxy_id=None,
            ownerapp_id=apikey.ownerapp_id,
            model_name="gpt-4",
            request_action="completions",
        )


async def test_reserve_rejects_when_estimated_tokens_exceed_capacity(session: AsyncSession):
    """预占时若估算 token 超过总剩余额度，应直接拒绝。"""
    apikey = await _create_test_apikey(session)
    await _create_test_quota(
        session,
        apikey.id,
        call_limit=10,
        total_tokens_limit=100,
    )

    with pytest.raises(ApiKeyQuotaExceeded):
        await reserve_apikey_quota(
            session=session,
            api_key_id=apikey.id,
            proxy_id=None,
            ownerapp_id=apikey.ownerapp_id,
            model_name="gpt-4",
            request_action="completions",
            estimated_total_tokens=101,
        )


async def test_fifo_token_distribution(session: AsyncSession):
    """多配额单的 FIFO token 分配: tokens 应优先填满第一张配额单。"""
    apikey = await _create_test_apikey(session)
    q1 = await _create_test_quota(
        session,
        apikey.id,
        call_limit=100,
        total_tokens_limit=200,
        order_id="fifo-order-1",
    )
    q2 = await _create_test_quota(
        session,
        apikey.id,
        call_limit=100,
        total_tokens_limit=500,
        order_id="fifo-order-2",
    )

    # reserve 一次
    result = await reserve_apikey_quota(
        session=session,
        api_key_id=apikey.id,
        proxy_id=None,
        ownerapp_id=apikey.ownerapp_id,
        model_name="gpt-4",
        request_action="completions",
    )
    assert result is not None
    quota_id, usage_id = result

    # finalize 消耗 300 tokens -> 超过 q1 的 200 限制
    await finalize_apikey_quota_usage(
        session=session,
        api_key_id=apikey.id,
        primary_quota_id=quota_id,
        primary_quota_usage_id=usage_id,
        total_tokens=300,
        ownerapp_id=apikey.ownerapp_id,
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
    apikey = await _create_test_apikey(session)
    quota = await _create_test_quota(
        session,
        apikey.id,
        call_limit=100,
        total_tokens_limit=200,
        order_id="overflow-order",
    )

    result = await reserve_apikey_quota(
        session=session,
        api_key_id=apikey.id,
        proxy_id=None,
        ownerapp_id=apikey.ownerapp_id,
        model_name="gpt-4",
        request_action="completions",
        estimated_total_tokens=200,
    )
    assert result is not None
    quota_id, usage_id = result

    with pytest.raises(ApiKeyQuotaExceeded):
        await finalize_apikey_quota_usage(
            session=session,
            api_key_id=apikey.id,
            primary_quota_id=quota_id,
            primary_quota_usage_id=usage_id,
            total_tokens=300,
            ownerapp_id=apikey.ownerapp_id,
            model_name="gpt-4",
            request_action="completions",
            log_id=None,
        )

    await session.refresh(quota)
    assert quota.total_tokens_used == 200
