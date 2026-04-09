from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import delete

from openaiproxy.api.utils import check_api_key
from openaiproxy.services.database.models import Node as OpenAINode
from openaiproxy.services.database.models import NodeModel as OpenAINodeModel
from openaiproxy.services.database.models.node.model import AppMonthlyModelUsage
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


async def _seed_request_logs(clean_session):
    """写入测试所需的请求日志样本。"""
    now = current_time_in_timezone()

    node = OpenAINode(url="http://log-node.example.com", name="log-node")
    proxy = ProxyInstance(instance_name="proxy-1", instance_ip="127.0.0.1")
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
        ownerapp_id="app-a",
        action=RequestAction.completions,
        model_name="gpt-4o-mini",
        start_at=now - timedelta(seconds=20),
        end_at=now - timedelta(seconds=10),
        first_response_at=now - timedelta(seconds=18),
        latency=0.42,
        stream=False,
        request_tokens=10,
        response_tokens=15,
        total_tokens=25,
        error=False,
        abort=False,
    )
    processing_log = ProxyNodeStatusLog(
        node_id=node.id,
        proxy_id=proxy.id,
        status_id=status.id,
        ownerapp_id="app-b",
        action=RequestAction.embeddings,
        model_name="text-embedding-3-small",
        start_at=now - timedelta(seconds=5),
        end_at=None,
        latency=0.11,
        stream=True,
        request_tokens=20,
        response_tokens=0,
        total_tokens=20,
        error=True,
        abort=True,
    )

    clean_session.add(finished_log)
    clean_session.add(processing_log)
    await clean_session.commit()
    await clean_session.refresh(finished_log)
    await clean_session.refresh(processing_log)

    return {
        "node_id": node.id,
        "proxy_id": proxy.id,
        "status_id": status.id,
        "finished_log_id": finished_log.id,
        "processing_log_id": processing_log.id,
    }


@pytest.mark.asyncio
async def test_list_request_logs_with_filters(api_client):
    """验证请求记录列表查询与过滤。"""
    client, clean_session = api_client
    sample = await _seed_request_logs(clean_session)

    list_resp = await client.get("/request-logs")
    assert list_resp.status_code == 200
    payload = list_resp.json()
    assert payload["total"] == 2
    assert payload["offset"] == 0

    filtered_resp = await client.get(
        "/request-logs",
        params={
            "node_id": str(sample["node_id"]),
            "action": "completions",
            "ownerapp_id": "app-a",
        },
    )
    assert filtered_resp.status_code == 200
    filtered_payload = filtered_resp.json()
    assert filtered_payload["total"] == 1
    assert filtered_payload["data"][0]["id"] == str(sample["finished_log_id"])
    assert filtered_payload["data"][0]["action"] == "completions"


@pytest.mark.asyncio
async def test_list_request_logs_processing_and_pagination(api_client):
    """验证处理中筛选与分页能力。"""
    client, clean_session = api_client
    sample = await _seed_request_logs(clean_session)

    processing_resp = await client.get(
        "/request-logs",
        params={"processing": "true", "error": "true", "abort": "true"},
    )
    assert processing_resp.status_code == 200
    processing_payload = processing_resp.json()
    assert processing_payload["total"] == 1
    assert processing_payload["data"][0]["id"] == str(sample["processing_log_id"])
    assert processing_payload["data"][0]["end_at"] is None

    pagination_resp = await client.get(
        "/request-logs",
        params={"offset": 1, "limit": 1, "orderby": "start_at asc"},
    )
    assert pagination_resp.status_code == 200
    pagination_payload = pagination_resp.json()
    assert pagination_payload["total"] == 2
    assert pagination_payload["offset"] == 1
    assert len(pagination_payload["data"]) == 1
    assert pagination_payload["data"][0]["id"] == str(sample["processing_log_id"])


@pytest.mark.asyncio
async def test_list_monthly_model_usage_by_owner_and_month(api_client):
    """验证按应用按月查询模型用量。"""
    client, clean_session = api_client
    now = current_time_in_timezone()
    current_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_text = current_month_start.strftime("%Y-%m")

    row_1 = AppMonthlyModelUsage(
        ownerapp_id="app-usage",
        model_name="gpt-4o-mini",
        month_start=current_month_start,
        call_count=12,
        request_tokens=100,
        response_tokens=220,
        total_tokens=320,
        created_at=now,
        updated_at=now,
    )
    row_2 = AppMonthlyModelUsage(
        ownerapp_id="other-app",
        model_name="gpt-4o-mini",
        month_start=current_month_start,
        call_count=2,
        request_tokens=10,
        response_tokens=20,
        total_tokens=30,
        created_at=now,
        updated_at=now,
    )
    clean_session.add(row_1)
    clean_session.add(row_2)
    await clean_session.commit()

    resp = await client.get(
        "/request-logs/monthly-usage",
        params={"ownerapp_id": "app-usage", "month": month_text},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    assert payload["offset"] == 0
    assert payload["data"][0]["ownerapp_id"] == "app-usage"
    assert payload["data"][0]["model_name"] == "gpt-4o-mini"
    assert payload["data"][0]["total_tokens"] == 320


@pytest.mark.asyncio
async def test_list_monthly_model_usage_invalid_month(api_client):
    """验证非法month参数返回422。"""
    client, _ = api_client
    resp = await client.get(
        "/request-logs/monthly-usage",
        params={"ownerapp_id": "app-usage", "month": "2026/04"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_yearly_model_usage_by_owner_and_year(api_client):
    """验证按应用按年查询模型用量聚合。"""
    client, clean_session = api_client
    now = current_time_in_timezone()

    jan_2026 = now.replace(year=2026, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    feb_2026 = now.replace(year=2026, month=2, day=1, hour=0, minute=0, second=0, microsecond=0)
    mar_2026 = now.replace(year=2026, month=3, day=1, hour=0, minute=0, second=0, microsecond=0)
    dec_2025 = now.replace(year=2025, month=12, day=1, hour=0, minute=0, second=0, microsecond=0)

    clean_session.add_all([
        AppMonthlyModelUsage(
            ownerapp_id="app-year",
            model_name="gpt-4o-mini",
            month_start=jan_2026,
            call_count=3,
            request_tokens=30,
            response_tokens=60,
            total_tokens=90,
            created_at=now,
            updated_at=now,
        ),
        AppMonthlyModelUsage(
            ownerapp_id="app-year",
            model_name="gpt-4o-mini",
            month_start=feb_2026,
            call_count=5,
            request_tokens=50,
            response_tokens=100,
            total_tokens=150,
            created_at=now,
            updated_at=now,
        ),
        AppMonthlyModelUsage(
            ownerapp_id="app-year",
            model_name="gpt-4.1",
            month_start=mar_2026,
            call_count=2,
            request_tokens=20,
            response_tokens=40,
            total_tokens=60,
            created_at=now,
            updated_at=now,
        ),
        AppMonthlyModelUsage(
            ownerapp_id="app-year",
            model_name="gpt-4o-mini",
            month_start=dec_2025,
            call_count=100,
            request_tokens=1000,
            response_tokens=2000,
            total_tokens=3000,
            created_at=now,
            updated_at=now,
        ),
        AppMonthlyModelUsage(
            ownerapp_id="other-app",
            model_name="gpt-4o-mini",
            month_start=jan_2026,
            call_count=7,
            request_tokens=70,
            response_tokens=140,
            total_tokens=210,
            created_at=now,
            updated_at=now,
        ),
    ])
    await clean_session.commit()

    resp = await client.get(
        "/request-logs/yearly-usage",
        params={"ownerapp_id": "app-year", "year": "2026"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 2

    by_model = {item["model_name"]: item for item in payload["data"]}
    assert by_model["gpt-4o-mini"]["call_count"] == 8
    assert by_model["gpt-4o-mini"]["request_tokens"] == 80
    assert by_model["gpt-4o-mini"]["response_tokens"] == 160
    assert by_model["gpt-4o-mini"]["total_tokens"] == 240
    assert by_model["gpt-4o-mini"]["year"] == 2026

    assert by_model["gpt-4.1"]["call_count"] == 2
    assert by_model["gpt-4.1"]["total_tokens"] == 60
    assert by_model["gpt-4.1"]["year"] == 2026


@pytest.mark.asyncio
async def test_list_yearly_model_usage_invalid_year(api_client):
    """验证非法year参数返回422。"""
    client, _ = api_client
    resp = await client.get(
        "/request-logs/yearly-usage",
        params={"ownerapp_id": "app-year", "year": "2026/xx"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_yearly_usage_total_by_owner_and_year(api_client):
    """验证按应用按年查询模型用量总计（不分模型）。"""
    client, clean_session = api_client
    now = current_time_in_timezone()

    jan_2026 = now.replace(year=2026, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    feb_2026 = now.replace(year=2026, month=2, day=1, hour=0, minute=0, second=0, microsecond=0)
    dec_2025 = now.replace(year=2025, month=12, day=1, hour=0, minute=0, second=0, microsecond=0)

    clean_session.add_all([
        AppMonthlyModelUsage(
            ownerapp_id="app-total",
            model_name="gpt-4o-mini",
            month_start=jan_2026,
            call_count=4,
            request_tokens=40,
            response_tokens=80,
            total_tokens=120,
            created_at=now,
            updated_at=now,
        ),
        AppMonthlyModelUsage(
            ownerapp_id="app-total",
            model_name="gpt-4.1",
            month_start=feb_2026,
            call_count=6,
            request_tokens=60,
            response_tokens=120,
            total_tokens=180,
            created_at=now,
            updated_at=now,
        ),
        AppMonthlyModelUsage(
            ownerapp_id="app-total",
            model_name="gpt-4o-mini",
            month_start=dec_2025,
            call_count=100,
            request_tokens=1000,
            response_tokens=2000,
            total_tokens=3000,
            created_at=now,
            updated_at=now,
        ),
        AppMonthlyModelUsage(
            ownerapp_id="other-total",
            model_name="gpt-4o-mini",
            month_start=jan_2026,
            call_count=7,
            request_tokens=70,
            response_tokens=140,
            total_tokens=210,
            created_at=now,
            updated_at=now,
        ),
    ])
    await clean_session.commit()

    resp = await client.get(
        "/request-logs/yearly-usage-total",
        params={"ownerapp_id": "app-total", "year": "2026"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    assert payload["data"][0]["ownerapp_id"] == "app-total"
    assert payload["data"][0]["year"] == 2026
    assert payload["data"][0]["call_count"] == 10
    assert payload["data"][0]["request_tokens"] == 100
    assert payload["data"][0]["response_tokens"] == 200
    assert payload["data"][0]["total_tokens"] == 300


@pytest.mark.asyncio
async def test_list_yearly_usage_total_invalid_year(api_client):
    """验证年度总计接口非法year参数返回422。"""
    client, _ = api_client
    resp = await client.get(
        "/request-logs/yearly-usage-total",
        params={"ownerapp_id": "app-total", "year": "2026/xx"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_monthly_model_usage_with_models_filter(api_client):
    """验证月度用量接口支持按模型列表过滤。"""
    client, clean_session = api_client
    now = current_time_in_timezone()
    current_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_text = current_month_start.strftime("%Y-%m")

    clean_session.add_all([
        AppMonthlyModelUsage(
            ownerapp_id="app-filter",
            model_name="gpt-4o-mini",
            month_start=current_month_start,
            call_count=10,
            request_tokens=100,
            response_tokens=120,
            total_tokens=220,
            created_at=now,
            updated_at=now,
        ),
        AppMonthlyModelUsage(
            ownerapp_id="app-filter",
            model_name="gpt-4.1",
            month_start=current_month_start,
            call_count=3,
            request_tokens=30,
            response_tokens=40,
            total_tokens=70,
            created_at=now,
            updated_at=now,
        ),
    ])
    await clean_session.commit()

    resp = await client.get(
        "/request-logs/monthly-usage",
        params={
            "ownerapp_id": "app-filter",
            "month": month_text,
            "models": "gpt-4.1",
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    assert payload["data"][0]["model_name"] == "gpt-4.1"
    assert payload["data"][0]["total_tokens"] == 70


@pytest.mark.asyncio
async def test_list_yearly_model_usage_with_models_filter(api_client):
    """验证年度用量接口支持按模型列表过滤。"""
    client, clean_session = api_client
    now = current_time_in_timezone()
    jan_2026 = now.replace(year=2026, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    feb_2026 = now.replace(year=2026, month=2, day=1, hour=0, minute=0, second=0, microsecond=0)

    clean_session.add_all([
        AppMonthlyModelUsage(
            ownerapp_id="app-year-filter",
            model_name="gpt-4o-mini",
            month_start=jan_2026,
            call_count=5,
            request_tokens=50,
            response_tokens=60,
            total_tokens=110,
            created_at=now,
            updated_at=now,
        ),
        AppMonthlyModelUsage(
            ownerapp_id="app-year-filter",
            model_name="gpt-4.1",
            month_start=feb_2026,
            call_count=2,
            request_tokens=20,
            response_tokens=25,
            total_tokens=45,
            created_at=now,
            updated_at=now,
        ),
    ])
    await clean_session.commit()

    resp = await client.get(
        "/request-logs/yearly-usage",
        params={
            "ownerapp_id": "app-year-filter",
            "year": "2026",
            "models": "gpt-4o-mini",
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    assert payload["data"][0]["model_name"] == "gpt-4o-mini"
    assert payload["data"][0]["total_tokens"] == 110


@pytest.mark.asyncio
async def test_list_yearly_usage_total_with_models_filter(api_client):
    """验证年度总计接口支持按模型列表过滤。"""
    client, clean_session = api_client
    now = current_time_in_timezone()
    jan_2026 = now.replace(year=2026, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    clean_session.add_all([
        AppMonthlyModelUsage(
            ownerapp_id="app-total-filter",
            model_name="gpt-4o-mini",
            month_start=jan_2026,
            call_count=8,
            request_tokens=80,
            response_tokens=90,
            total_tokens=170,
            created_at=now,
            updated_at=now,
        ),
        AppMonthlyModelUsage(
            ownerapp_id="app-total-filter",
            model_name="gpt-4.1",
            month_start=jan_2026,
            call_count=6,
            request_tokens=60,
            response_tokens=70,
            total_tokens=130,
            created_at=now,
            updated_at=now,
        ),
    ])
    await clean_session.commit()

    resp = await client.get(
        "/request-logs/yearly-usage-total",
        params={
            "ownerapp_id": "app-total-filter",
            "year": "2026",
            "models": "gpt-4o-mini",
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    assert payload["data"][0]["ownerapp_id"] == "app-total-filter"
    assert payload["data"][0]["total_tokens"] == 170


@pytest.mark.asyncio
async def test_list_monthly_usage_total_with_models_filter(api_client):
    """验证月度总计接口支持按模型列表过滤。"""
    client, clean_session = api_client
    now = current_time_in_timezone()
    current_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_text = current_month_start.strftime("%Y-%m")

    clean_session.add_all([
        AppMonthlyModelUsage(
            ownerapp_id="app-month-total",
            model_name="gpt-4o-mini",
            month_start=current_month_start,
            call_count=11,
            request_tokens=110,
            response_tokens=220,
            total_tokens=330,
            created_at=now,
            updated_at=now,
        ),
        AppMonthlyModelUsage(
            ownerapp_id="app-month-total",
            model_name="gpt-4.1",
            month_start=current_month_start,
            call_count=7,
            request_tokens=70,
            response_tokens=90,
            total_tokens=160,
            created_at=now,
            updated_at=now,
        ),
    ])
    await clean_session.commit()

    resp = await client.get(
        "/request-logs/monthly-usage-total",
        params={
            "ownerapp_id": "app-month-total",
            "month": month_text,
            "models": "gpt-4o-mini",
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    assert payload["data"][0]["ownerapp_id"] == "app-month-total"
    assert payload["data"][0]["month_start"].startswith(month_text)
    assert payload["data"][0]["call_count"] == 11
    assert payload["data"][0]["total_tokens"] == 330


@pytest.mark.asyncio
async def test_list_monthly_usage_total_invalid_month(api_client):
    """验证月度总计接口非法month参数返回422。"""
    client, _ = api_client
    resp = await client.get(
        "/request-logs/monthly-usage-total",
        params={"ownerapp_id": "app-month-total", "month": "2026/xx"},
    )
    assert resp.status_code == 422
