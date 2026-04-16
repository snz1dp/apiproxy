from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlmodel import delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from openaiproxy.services.database.models import Node as OpenAINode
from openaiproxy.services.database.models import NodeModel as OpenAINodeModel
from openaiproxy.services.database.models.node.crud import (
    aggregate_weekly_model_usage,
    upsert_app_weekly_model_usage,
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
async def test_aggregate_and_upsert_weekly_usage(clean_session: AsyncSession):
    now = current_time_in_timezone()
    current_week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    previous_week_start = current_week_start - timedelta(days=7)
    previous_week_end = current_week_start

    node = OpenAINode(url=f"http://weekly-usage-node-{uuid4()}", name="weekly-usage-node")
    clean_session.add(node)
    await clean_session.flush()

    node_model = OpenAINodeModel(node_id=node.id, model_name="gpt-4o-mini")
    clean_session.add(node_model)
    await clean_session.flush()

    proxy = ProxyInstance(instance_name=f"weekly-usage-proxy-{uuid4()}", instance_ip="127.0.0.1")
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
            ownerapp_id="app-weekly-rollup",
            action=RequestAction.completions,
            model_name="gpt-4o-mini",
            start_at=previous_week_start + timedelta(days=1),
            end_at=previous_week_start + timedelta(days=1, seconds=1),
            request_tokens=11,
            response_tokens=21,
            total_tokens=32,
        ),
        ProxyNodeStatusLog(
            node_id=node.id,
            proxy_id=proxy.id,
            status_id=status.id,
            ownerapp_id="app-weekly-rollup",
            action=RequestAction.completions,
            model_name="gpt-4o-mini",
            start_at=previous_week_start + timedelta(days=5, hours=2),
            end_at=previous_week_start + timedelta(days=5, hours=2, seconds=1),
            request_tokens=9,
            response_tokens=14,
            total_tokens=23,
        ),
        ProxyNodeStatusLog(
            node_id=node.id,
            proxy_id=proxy.id,
            status_id=status.id,
            ownerapp_id="app-weekly-rollup",
            action=RequestAction.completions,
            model_name="gpt-4o-mini",
            start_at=previous_week_end + timedelta(hours=1),
            end_at=previous_week_end + timedelta(hours=1, seconds=1),
            request_tokens=1,
            response_tokens=1,
            total_tokens=2,
        ),
    ])
    await clean_session.commit()

    rows = await aggregate_weekly_model_usage(
        week_start=previous_week_start,
        week_end=previous_week_end,
        session=clean_session,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.ownerapp_id == "app-weekly-rollup"
    assert row.call_count == 2
    assert row.request_tokens == 20
    assert row.response_tokens == 35
    assert row.total_tokens == 55

    await upsert_app_weekly_model_usage(
        week_start=previous_week_start,
        usage=row,
        session=clean_session,
    )
    await clean_session.commit()

    saved_rows = (await clean_session.exec(select(AppWeeklyModelUsage))).all()
    assert len(saved_rows) == 1
    assert saved_rows[0].call_count == 2

    row.call_count = 4
    row.total_tokens = 60
    await upsert_app_weekly_model_usage(
        week_start=previous_week_start,
        usage=row,
        session=clean_session,
    )
    await clean_session.commit()

    refreshed_rows = (await clean_session.exec(select(AppWeeklyModelUsage))).all()
    assert len(refreshed_rows) == 1
    assert refreshed_rows[0].call_count == 4
    assert refreshed_rows[0].total_tokens == 60