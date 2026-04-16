from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlmodel import delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from openaiproxy.services.database.models import Node as OpenAINode
from openaiproxy.services.database.models import NodeModel as OpenAINodeModel
from openaiproxy.services.database.models.node.crud import (
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
async def test_aggregate_and_upsert_daily_usage(clean_session: AsyncSession):
    now = current_time_in_timezone()
    current_day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    previous_day_start = current_day_start - timedelta(days=1)

    node = OpenAINode(url=f"http://daily-usage-node-{uuid4()}", name="daily-usage-node")
    clean_session.add(node)
    await clean_session.flush()

    node_model = OpenAINodeModel(node_id=node.id, model_name="gpt-4o-mini")
    clean_session.add(node_model)
    await clean_session.flush()

    proxy = ProxyInstance(instance_name=f"daily-usage-proxy-{uuid4()}", instance_ip="127.0.0.1")
    clean_session.add(proxy)
    await clean_session.flush()

    status = ProxyNodeStatus(node_id=node.id, proxy_id=proxy.id, avaiaible=True)
    clean_session.add(status)
    await clean_session.flush()

    clean_session.add_all([
        ProxyNodeStatusLog(
            node_id=node.id,
            proxy_id=proxy.id,
            status_id=status.id,
            ownerapp_id="app-daily-rollup",
            action=RequestAction.completions,
            model_name="gpt-4o-mini",
            start_at=previous_day_start + timedelta(hours=2),
            end_at=previous_day_start + timedelta(hours=2, seconds=1),
            request_tokens=10,
            response_tokens=20,
            total_tokens=30,
        ),
        ProxyNodeStatusLog(
            node_id=node.id,
            proxy_id=proxy.id,
            status_id=status.id,
            ownerapp_id="app-daily-rollup",
            action=RequestAction.completions,
            model_name="gpt-4o-mini",
            start_at=previous_day_start + timedelta(hours=5),
            end_at=previous_day_start + timedelta(hours=5, seconds=1),
            request_tokens=7,
            response_tokens=8,
            total_tokens=15,
        ),
        ProxyNodeStatusLog(
            node_id=node.id,
            proxy_id=proxy.id,
            status_id=status.id,
            ownerapp_id="app-daily-rollup",
            action=RequestAction.completions,
            model_name="gpt-4o-mini",
            start_at=current_day_start + timedelta(hours=1),
            end_at=current_day_start + timedelta(hours=1, seconds=1),
            request_tokens=1,
            response_tokens=1,
            total_tokens=2,
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
    assert row.ownerapp_id == "app-daily-rollup"
    assert row.call_count == 2
    assert row.request_tokens == 17
    assert row.response_tokens == 28
    assert row.total_tokens == 45

    await upsert_app_daily_model_usage(
        day_start=previous_day_start,
        usage=row,
        session=clean_session,
    )
    await clean_session.commit()

    saved_rows = (await clean_session.exec(select(AppDailyModelUsage))).all()
    assert len(saved_rows) == 1
    assert saved_rows[0].call_count == 2

    row.call_count = 3
    row.total_tokens = 50
    await upsert_app_daily_model_usage(
        day_start=previous_day_start,
        usage=row,
        session=clean_session,
    )
    await clean_session.commit()

    refreshed_rows = (await clean_session.exec(select(AppDailyModelUsage))).all()
    assert len(refreshed_rows) == 1
    assert refreshed_rows[0].call_count == 3
    assert refreshed_rows[0].total_tokens == 50