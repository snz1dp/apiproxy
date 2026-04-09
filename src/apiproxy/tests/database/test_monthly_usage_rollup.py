from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlmodel import delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from openaiproxy.services.database.models import Node as OpenAINode
from openaiproxy.services.database.models import NodeModel as OpenAINodeModel
from openaiproxy.services.database.models.node.crud import (
    aggregate_monthly_model_usage,
    upsert_app_monthly_model_usage,
)
from openaiproxy.services.database.models.node.model import AppMonthlyModelUsage
from openaiproxy.services.database.models.proxy.model import (
    ProxyInstance,
    ProxyNodeStatus,
    ProxyNodeStatusLog,
    RequestAction,
)
from openaiproxy.utils.timezone import current_time_in_timezone


@pytest.fixture
async def clean_session(session: AsyncSession):
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
        await session.exec(delete(AppMonthlyModelUsage))
        await session.exec(delete(ProxyNodeStatusLog))
        await session.exec(delete(ProxyNodeStatus))
        await session.exec(delete(ProxyInstance))
        await session.exec(delete(OpenAINodeModel))
        await session.exec(delete(OpenAINode))
        await session.commit()


@pytest.mark.asyncio
async def test_aggregate_and_upsert_monthly_usage(clean_session: AsyncSession):
    now = current_time_in_timezone()
    current_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_month_end = current_month_start
    previous_month_start = (current_month_start - timedelta(days=1)).replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    node = OpenAINode(url=f"http://usage-node-{uuid4()}", name="usage-node")
    clean_session.add(node)
    await clean_session.flush()

    node_model = OpenAINodeModel(node_id=node.id, model_name="gpt-4o-mini")
    clean_session.add(node_model)
    await clean_session.flush()

    proxy = ProxyInstance(instance_name=f"usage-proxy-{uuid4()}", instance_ip="127.0.0.1")
    clean_session.add(proxy)
    await clean_session.flush()

    status = ProxyNodeStatus(node_id=node.id, proxy_id=proxy.id, avaiaible=True)
    clean_session.add(status)
    await clean_session.flush()

    log_1 = ProxyNodeStatusLog(
        node_id=node.id,
        proxy_id=proxy.id,
        status_id=status.id,
        ownerapp_id="app-rollup",
        action=RequestAction.completions,
        model_name="gpt-4o-mini",
        start_at=previous_month_start + timedelta(days=3),
        end_at=previous_month_start + timedelta(days=3, seconds=1),
        request_tokens=10,
        response_tokens=20,
        total_tokens=30,
    )
    log_2 = ProxyNodeStatusLog(
        node_id=node.id,
        proxy_id=proxy.id,
        status_id=status.id,
        ownerapp_id="app-rollup",
        action=RequestAction.completions,
        model_name="gpt-4o-mini",
        start_at=previous_month_start + timedelta(days=5),
        end_at=previous_month_start + timedelta(days=5, seconds=1),
        request_tokens=7,
        response_tokens=8,
        total_tokens=15,
    )
    log_outside_month = ProxyNodeStatusLog(
        node_id=node.id,
        proxy_id=proxy.id,
        status_id=status.id,
        ownerapp_id="app-rollup",
        action=RequestAction.completions,
        model_name="gpt-4o-mini",
        start_at=current_month_start + timedelta(days=1),
        end_at=current_month_start + timedelta(days=1, seconds=1),
        request_tokens=1,
        response_tokens=1,
        total_tokens=2,
    )

    clean_session.add(log_1)
    clean_session.add(log_2)
    clean_session.add(log_outside_month)
    await clean_session.commit()

    rows = await aggregate_monthly_model_usage(
        month_start=previous_month_start,
        month_end=previous_month_end,
        session=clean_session,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.ownerapp_id == "app-rollup"
    assert row.model_name == "gpt-4o-mini"
    assert row.call_count == 2
    assert row.request_tokens == 17
    assert row.response_tokens == 28
    assert row.total_tokens == 45

    await upsert_app_monthly_model_usage(
        month_start=previous_month_start,
        usage=row,
        session=clean_session,
    )
    await clean_session.commit()

    saved_rows = (await clean_session.exec(select(AppMonthlyModelUsage))).all()
    assert len(saved_rows) == 1
    assert saved_rows[0].call_count == 2

    row.call_count = 3
    row.total_tokens = 50
    await upsert_app_monthly_model_usage(
        month_start=previous_month_start,
        usage=row,
        session=clean_session,
    )
    await clean_session.commit()

    refreshed_rows = (await clean_session.exec(select(AppMonthlyModelUsage))).all()
    assert len(refreshed_rows) == 1
    assert refreshed_rows[0].call_count == 3
    assert refreshed_rows[0].total_tokens == 50
