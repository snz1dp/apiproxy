from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from openaiproxy.services.deps import get_async_session


@pytest.fixture
async def api_client(session):
    """创建健康检查与文档接口测试客户端。"""
    from openaiproxy.main import setup_app

    app = setup_app(backend_only=True)

    async def override_session():
        yield session

    app.dependency_overrides[get_async_session] = override_session
    transport = ASGITransport(app=app)

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_health_route_returns_ok(api_client: AsyncClient):
    """验证基础健康检查接口返回正常状态。"""
    response = await api_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_check_route_returns_ok(api_client: AsyncClient):
    """验证服务级健康检查接口在数据库正常时返回成功。"""
    response = await api_client.get("/health_check")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["db"] == "ok"


@pytest.mark.asyncio
async def test_health_check_route_returns_500_when_database_check_fails(api_client: AsyncClient, monkeypatch):
    """验证数据库检查失败时返回 500 且不暴露内部异常。"""

    async def fake_count_nodes(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("openaiproxy.api.health_check.count_nodes", fake_count_nodes)

    response = await api_client.get("/health_check")

    assert response.status_code == 500
    assert response.json()["detail"]["status"] == "nok"
    assert response.json()["detail"]["db"] == "error check the server logs"


@pytest.mark.asyncio
async def test_docs_routes_render_custom_pages(api_client: AsyncClient):
    """验证自定义 Swagger 与 ReDoc 文档页面可访问。"""
    docs_response = await api_client.get("/docs")
    redoc_response = await api_client.get("/redoc")

    assert docs_response.status_code == 200
    assert "swagger-ui" in docs_response.text
    assert redoc_response.status_code == 200
    assert "redoc" in redoc_response.text.lower()