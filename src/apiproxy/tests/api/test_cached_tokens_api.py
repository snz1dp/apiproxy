"""cached_tokens 在 API Schema 层的透传测试。

验证：
1. 请求日志 API 响应中包含 cached_tokens 字段
2. 日/周/月/年用量 API 响应中包含 cached_tokens 字段
3. 实时聚合的日度用量也能返回 cached_tokens
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import delete

from openaiproxy.api.utils import check_api_key
from openaiproxy.services.database.models import Node as OpenAINode
from openaiproxy.services.database.models import NodeModel as OpenAINodeModel
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
from openaiproxy.services.deps import get_async_session
from openaiproxy.utils.timezone import current_time_in_timezone


@pytest.fixture
async def clean_session(session):
    """清理并提供隔离数据库会话。"""
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


@pytest.fixture
async def api_client(clean_session):
    """创建用于 API 测试的客户端。"""
    from openaiproxy.main import setup_app

    app = setup_app(backend_only=True)

    async def override_session():
        yield clean_session

    async def override_api_key():
        return None

    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[check_api_key] = override_api_key

    transport = ASGITransport(app=app)

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, clean_session
    finally:
        app.dependency_overrides.clear()


async def _seed_log_with_cached_tokens(clean_session):
    """写入带 cached_tokens 的请求日志。"""
    now = current_time_in_timezone()

    node = OpenAINode(url="http://cached-log-node.example.com", name="cached-log-node")
    proxy = ProxyInstance(instance_name="cached-proxy-1", instance_ip="127.0.0.1")
    clean_session.add(node)
    clean_session.add(proxy)
    await clean_session.flush()

    status = ProxyNodeStatus(node_id=node.id, proxy_id=proxy.id, avaiaible=True)
    clean_session.add(status)
    await clean_session.flush()

    finished_log = ProxyNodeStatusLog(
        node_id=node.id,
        proxy_id=proxy.id,
        status_id=status.id,
        ownerapp_id="app-cached-api",
        action=RequestAction.completions,
        model_name="gpt-4o-mini",
        start_at=now - timedelta(seconds=20),
        end_at=now - timedelta(seconds=10),
        first_response_at=now - timedelta(seconds=18),
        latency=0.42,
        stream=False,
        request_tokens=100,
        response_tokens=50,
        total_tokens=150,
        cached_tokens=80,
        error=False,
        abort=False,
    )
    clean_session.add(finished_log)
    await clean_session.commit()
    await clean_session.refresh(finished_log)

    return {
        "node_id": node.id,
        "proxy_id": proxy.id,
        "status_id": status.id,
        "finished_log_id": finished_log.id,
    }


# ── 请求日志 API ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_log_response_includes_cached_tokens(api_client):
    """验证请求日志 API 响应中包含 cached_tokens 字段。"""
    client, clean_session = api_client
    sample = await _seed_log_with_cached_tokens(clean_session)

    resp = await client.get("/request-logs", params={"ownerapp_id": "app-cached-api"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    log_data = payload["data"][0]
    assert "cached_tokens" in log_data
    assert log_data["cached_tokens"] == 80
    assert log_data["request_tokens"] == 100
    assert log_data["response_tokens"] == 50
    assert log_data["total_tokens"] == 150


# ── 日度用量 API（实时聚合） ──────────────────────────────────


@pytest.mark.asyncio
async def test_daily_usage_realtime_includes_cached_tokens(api_client):
    """验证实时聚合的日度用量返回 cached_tokens。"""
    client, clean_session = api_client
    await _seed_log_with_cached_tokens(clean_session)

    resp = await client.get(
        "/request-logs/daily-usage",
        params={"ownerapp_id": "app-cached-api"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    data = payload["data"][0]
    assert data["ownerapp_id"] == "app-cached-api"
    assert data["cached_tokens"] == 80
    assert data["request_tokens"] == 100
    assert data["response_tokens"] == 50
    assert data["total_tokens"] == 150


# ── 日度用量 API（历史表） ────────────────────────────────────


@pytest.mark.asyncio
async def test_daily_usage_history_includes_cached_tokens(api_client):
    """验证历史日表的用量响应包含 cached_tokens。"""
    client, clean_session = api_client
    now = current_time_in_timezone()

    three_days_ago = (now - timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
    day_text = three_days_ago.strftime("%Y-%m-%d")

    clean_session.add(
        AppDailyModelUsage(
            ownerapp_id="app-cached-history",
            model_name="gpt-4o-mini",
            day_start=three_days_ago,
            call_count=5,
            request_tokens=200,
            response_tokens=100,
            total_tokens=300,
            cached_tokens=120,
            created_at=now,
            updated_at=now,
        ),
    )
    await clean_session.commit()

    resp = await client.get(
        "/request-logs/daily-usage",
        params={"ownerapp_id": "app-cached-history", "start_date": day_text, "end_date": day_text},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    data = payload["data"][0]
    assert data["cached_tokens"] == 120
    assert data["total_tokens"] == 300


# ── 月度用量 API ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_monthly_usage_includes_cached_tokens(api_client):
    """验证月度用量 API 响应包含 cached_tokens。"""
    client, clean_session = api_client
    now = current_time_in_timezone()

    two_months_ago = (now.replace(day=1) - timedelta(days=32)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )
    day_text = two_months_ago.strftime("%Y-%m-%d")

    clean_session.add(
        AppMonthlyModelUsage(
            ownerapp_id="app-cached-monthly",
            model_name="gpt-4o-mini",
            month_start=two_months_ago,
            call_count=10,
            request_tokens=500,
            response_tokens=300,
            total_tokens=800,
            cached_tokens=200,
            created_at=now,
            updated_at=now,
        ),
    )
    await clean_session.commit()

    resp = await client.get(
        "/request-logs/monthly-usage",
        params={"ownerapp_id": "app-cached-monthly", "start_date": day_text, "end_date": day_text},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    data = payload["data"][0]
    assert data["cached_tokens"] == 200
    assert data["total_tokens"] == 800


# ── 周度用量 API ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_weekly_usage_includes_cached_tokens(api_client):
    """验证周度用量 API 响应包含 cached_tokens。"""
    client, clean_session = api_client
    now = current_time_in_timezone()

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    two_weeks_ago_monday = today_start - timedelta(days=today_start.weekday()) - timedelta(weeks=2)
    two_weeks_ago_sunday = two_weeks_ago_monday + timedelta(days=6)

    clean_session.add(
        AppWeeklyModelUsage(
            ownerapp_id="app-cached-weekly",
            model_name="gpt-4o-mini",
            week_start=two_weeks_ago_monday,
            call_count=8,
            request_tokens=400,
            response_tokens=200,
            total_tokens=600,
            cached_tokens=150,
            created_at=now,
            updated_at=now,
        ),
    )
    await clean_session.commit()

    resp = await client.get(
        "/request-logs/weekly-usage",
        params={
            "ownerapp_id": "app-cached-weekly",
            "start_date": two_weeks_ago_monday.strftime("%Y-%m-%d"),
            "end_date": two_weeks_ago_sunday.strftime("%Y-%m-%d"),
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    data = payload["data"][0]
    assert data["cached_tokens"] == 150
    assert data["total_tokens"] == 600


# ── 年度用量 API ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_yearly_usage_includes_cached_tokens(api_client):
    """验证年度用量 API 响应包含 cached_tokens。"""
    client, clean_session = api_client
    now = current_time_in_timezone()

    jan_2026 = now.replace(year=2026, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    feb_2026 = now.replace(year=2026, month=2, day=1, hour=0, minute=0, second=0, microsecond=0)

    clean_session.add_all([
        AppMonthlyModelUsage(
            ownerapp_id="app-cached-yearly",
            model_name="gpt-4o-mini",
            month_start=jan_2026,
            call_count=5,
            request_tokens=300,
            response_tokens=150,
            total_tokens=450,
            cached_tokens=100,
            created_at=now,
            updated_at=now,
        ),
        AppMonthlyModelUsage(
            ownerapp_id="app-cached-yearly",
            model_name="gpt-4o-mini",
            month_start=feb_2026,
            call_count=3,
            request_tokens=200,
            response_tokens=100,
            total_tokens=300,
            cached_tokens=50,
            created_at=now,
            updated_at=now,
        ),
    ])
    await clean_session.commit()

    resp = await client.get(
        "/request-logs/yearly-usage",
        params={
            "ownerapp_id": "app-cached-yearly",
            "start_date": "2026-01-01",
            "end_date": "2026-02-28",
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    data = payload["data"][0]
    # cached_tokens 应被累加：100 + 50 = 150
    assert data["cached_tokens"] == 150
    assert data["total_tokens"] == 750


# ── 年度用量总计 API ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_yearly_usage_total_includes_cached_tokens(api_client):
    """验证年度用量总计 API 响应包含 cached_tokens。"""
    client, clean_session = api_client
    now = current_time_in_timezone()

    jan_2026 = now.replace(year=2026, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    clean_session.add(
        AppMonthlyModelUsage(
            ownerapp_id="app-cached-total",
            model_name="gpt-4o-mini",
            month_start=jan_2026,
            call_count=10,
            request_tokens=500,
            response_tokens=300,
            total_tokens=800,
            cached_tokens=250,
            created_at=now,
            updated_at=now,
        ),
    )
    await clean_session.commit()

    resp = await client.get(
        "/request-logs/yearly-usage-total",
        params={
            "ownerapp_id": "app-cached-total",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    data = payload["data"][0]
    assert data["cached_tokens"] == 250
    assert data["total_tokens"] == 800
