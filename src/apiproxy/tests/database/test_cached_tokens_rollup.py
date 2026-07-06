"""cached_tokens 在聚合查询和 upsert 中的全链路测试。

验证：
1. ProxyNodeStatusLog 中的 cached_tokens 被正确聚合到 DailyUsageAggregate
2. upsert 时 cached_tokens 被正确写入日/周/月表
3. 跨 session upsert 时 cached_tokens 被正确更新
"""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlmodel import delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from openaiproxy.services.deps import get_db_service
from openaiproxy.services.database.models import Node as OpenAINode
from openaiproxy.services.database.models import NodeModel as OpenAINodeModel
from openaiproxy.services.database.models.node.crud import (
    DailyUsageAggregate,
    aggregate_daily_model_usage,
    upsert_app_daily_model_usage,
)
from openaiproxy.services.database.models.node.model import (
    AppDailyModelUsage,
    AppMonthlyModelUsage,
    AppWeeklyModelUsage,
)
from openaiproxy.services.database.models.proxy.model import (
    ProxyInstance,
    ProxyNodeStatus,
    ProxyNodeStatusLog,
    RequestAction,
)
from openaiproxy.utils.timezone import current_time_in_timezone


@pytest.fixture
async def clean_session(session: AsyncSession):
    """清理所有用量表和节点表后提供隔离 session。"""
    await session.exec(delete(AppWeeklyModelUsage))
    await session.exec(delete(AppDailyModelUsage))
    await session.exec(delete(AppMonthlyModelUsage))
    await session.exec(delete(ProxyNodeStatusLog))
    await session.exec(delete(ProxyNodeStatus))
    await session.exec(delete(ProxyInstance))
    await session.exec(delete(OpenAINodeModel))
    await session.exec(delete(OpenAINode))
    await session.commit()
    try:
        yield session
    finally:
        await session.rollback()
        await session.exec(delete(AppWeeklyModelUsage))
        await session.exec(delete(AppDailyModelUsage))
        await session.exec(delete(AppMonthlyModelUsage))
        await session.exec(delete(ProxyNodeStatusLog))
        await session.exec(delete(ProxyNodeStatus))
        await session.exec(delete(ProxyInstance))
        await session.exec(delete(OpenAINodeModel))
        await session.exec(delete(OpenAINode))
        await session.commit()


@pytest.mark.asyncio
async def test_aggregate_daily_usage_includes_cached_tokens(clean_session: AsyncSession):
    """验证聚合查询正确累加 cached_tokens。"""
    now = current_time_in_timezone()
    current_day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    previous_day_start = current_day_start - timedelta(days=1)

    node = OpenAINode(url=f"http://cached-node-{uuid4()}", name="cached-node")
    clean_session.add(node)
    await clean_session.flush()

    node_model = OpenAINodeModel(node_id=node.id, model_name="gpt-4o-mini")
    clean_session.add(node_model)
    await clean_session.flush()

    proxy = ProxyInstance(instance_name=f"cached-proxy-{uuid4()}", instance_ip="127.0.0.1")
    clean_session.add(proxy)
    await clean_session.flush()

    status = ProxyNodeStatus(node_id=node.id, proxy_id=proxy.id, avaiaible=True)
    clean_session.add(status)
    await clean_session.flush()

    # 两条日志：cached_tokens 分别为 30 和 20
    clean_session.add_all([
        ProxyNodeStatusLog(
            node_id=node.id,
            proxy_id=proxy.id,
            status_id=status.id,
            ownerapp_id="app-cached",
            action=RequestAction.completions,
            model_name="gpt-4o-mini",
            start_at=previous_day_start + timedelta(hours=2),
            end_at=previous_day_start + timedelta(hours=2, seconds=1),
            request_tokens=100,
            response_tokens=50,
            total_tokens=150,
            cached_tokens=30,
        ),
        ProxyNodeStatusLog(
            node_id=node.id,
            proxy_id=proxy.id,
            status_id=status.id,
            ownerapp_id="app-cached",
            action=RequestAction.completions,
            model_name="gpt-4o-mini",
            start_at=previous_day_start + timedelta(hours=5),
            end_at=previous_day_start + timedelta(hours=5, seconds=1),
            request_tokens=80,
            response_tokens=40,
            total_tokens=120,
            cached_tokens=20,
        ),
    ])
    await clean_session.commit()

    rows = await aggregate_daily_model_usage(
        day_start=previous_day_start,
        day_end=current_day_start,
        session=clean_session,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.ownerapp_id == "app-cached"
    assert row.call_count == 2
    assert row.request_tokens == 180
    assert row.response_tokens == 90
    assert row.total_tokens == 270
    # cached_tokens 应被正确累加
    assert row.cached_tokens == 50


@pytest.mark.asyncio
async def test_upsert_daily_usage_persists_cached_tokens(clean_session: AsyncSession):
    """验证 upsert 将 cached_tokens 写入日表。"""
    previous_day_start = current_time_in_timezone().replace(
        hour=0, minute=0, second=0, microsecond=0,
    ) - timedelta(days=1)

    usage = DailyUsageAggregate(
        ownerapp_id="app-cached-upsert",
        model_name="gpt-4o-mini",
        call_count=3,
        request_tokens=150,
        response_tokens=75,
        total_tokens=225,
        cached_tokens=60,
    )
    await upsert_app_daily_model_usage(
        day_start=previous_day_start,
        usage=usage,
        session=clean_session,
    )
    await clean_session.commit()

    saved_rows = (await clean_session.exec(select(AppDailyModelUsage))).all()
    assert len(saved_rows) == 1
    assert saved_rows[0].cached_tokens == 60
    assert saved_rows[0].total_tokens == 225


@pytest.mark.asyncio
async def test_upsert_daily_usage_updates_cached_tokens(clean_session: AsyncSession):
    """验证跨 session upsert 时 cached_tokens 被正确更新。"""
    previous_day_start = current_time_in_timezone().replace(
        hour=0, minute=0, second=0, microsecond=0,
    ) - timedelta(days=1)

    usage = DailyUsageAggregate(
        ownerapp_id="app-cached-update",
        model_name="gpt-4o-mini",
        call_count=1,
        request_tokens=50,
        response_tokens=25,
        total_tokens=75,
        cached_tokens=10,
    )
    db_service = get_db_service()

    async with db_service.with_async_session() as first_session:
        await upsert_app_daily_model_usage(
            day_start=previous_day_start,
            usage=usage,
            session=first_session,
        )
        await first_session.commit()

    # 更新 cached_tokens
    usage.call_count = 5
    usage.total_tokens = 200
    usage.cached_tokens = 80
    async with db_service.with_async_session() as second_session:
        await upsert_app_daily_model_usage(
            day_start=previous_day_start,
            usage=usage,
            session=second_session,
        )
        await second_session.commit()

    saved_rows = (await clean_session.exec(select(AppDailyModelUsage))).all()
    assert len(saved_rows) == 1
    assert saved_rows[0].call_count == 5
    assert saved_rows[0].total_tokens == 200
    assert saved_rows[0].cached_tokens == 80


@pytest.mark.asyncio
async def test_aggregate_daily_usage_default_cached_tokens_zero(clean_session: AsyncSession):
    """验证未设置 cached_tokens 的日志聚合时默认为 0。"""
    now = current_time_in_timezone()
    current_day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    previous_day_start = current_day_start - timedelta(days=1)

    node = OpenAINode(url=f"http://no-cache-node-{uuid4()}", name="no-cache-node")
    clean_session.add(node)
    await clean_session.flush()

    node_model = OpenAINodeModel(node_id=node.id, model_name="gpt-4o-mini")
    clean_session.add(node_model)
    await clean_session.flush()

    proxy = ProxyInstance(instance_name=f"no-cache-proxy-{uuid4()}", instance_ip="127.0.0.1")
    clean_session.add(proxy)
    await clean_session.flush()

    status = ProxyNodeStatus(node_id=node.id, proxy_id=proxy.id, avaiaible=True)
    clean_session.add(status)
    await clean_session.flush()

    # 不设置 cached_tokens（默认为 0）
    clean_session.add(
        ProxyNodeStatusLog(
            node_id=node.id,
            proxy_id=proxy.id,
            status_id=status.id,
            ownerapp_id="app-no-cache",
            action=RequestAction.completions,
            model_name="gpt-4o-mini",
            start_at=previous_day_start + timedelta(hours=2),
            end_at=previous_day_start + timedelta(hours=2, seconds=1),
            request_tokens=100,
            response_tokens=50,
            total_tokens=150,
        ),
    )
    await clean_session.commit()

    rows = await aggregate_daily_model_usage(
        day_start=previous_day_start,
        day_end=current_day_start,
        session=clean_session,
    )
    assert len(rows) == 1
    assert rows[0].cached_tokens == 0
