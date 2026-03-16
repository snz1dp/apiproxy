from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import delete

from openaiproxy.api.utils import check_api_key
from openaiproxy.services.database.models import Node as OpenAINode
from openaiproxy.services.database.models import NodeModel as OpenAINodeModel
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
