from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import delete

from openaiproxy.api.utils import check_api_key, check_strict_api_key
from openaiproxy.services.database.models import Node as OpenAINode
from openaiproxy.services.database.models import NodeModel as OpenAINodeModel
from openaiproxy.services.database.models.apikey.model import (
    ApiKey,
    ApiKeyQuota,
    ApiKeyQuotaUsage,
)
from openaiproxy.services.database.models.app.model import AppQuota, AppQuotaUsage
from openaiproxy.services.database.models.node.model import (
    ModelType,
    NodeModelQuota,
    NodeModelQuotaUsage,
)
from openaiproxy.services.deps import get_async_session
from openaiproxy.utils.apikey import hash_api_key
from openaiproxy.utils.timezone import current_time_in_timezone


@pytest.fixture
async def clean_session(session):
    """清理配额相关数据并提供隔离会话。"""
    await session.exec(delete(AppQuotaUsage))
    await session.exec(delete(ApiKeyQuotaUsage))
    await session.exec(delete(NodeModelQuotaUsage))
    await session.exec(delete(AppQuota))
    await session.exec(delete(ApiKeyQuota))
    await session.exec(delete(NodeModelQuota))
    await session.exec(delete(ApiKey))
    await session.exec(delete(OpenAINodeModel))
    await session.exec(delete(OpenAINode))
    await session.commit()
    try:
        yield session
    finally:
        await session.rollback()
        await session.exec(delete(AppQuotaUsage))
        await session.exec(delete(ApiKeyQuotaUsage))
        await session.exec(delete(NodeModelQuotaUsage))
        await session.exec(delete(AppQuota))
        await session.exec(delete(ApiKeyQuota))
        await session.exec(delete(NodeModelQuota))
        await session.exec(delete(ApiKey))
        await session.exec(delete(OpenAINodeModel))
        await session.exec(delete(OpenAINode))
        await session.commit()


@pytest.fixture
async def api_client(clean_session):
    """创建关闭鉴权依赖的 API 客户端。"""
    from openaiproxy.main import setup_app

    app = setup_app(backend_only=True)

    async def override_session():
        yield clean_session

    async def override_api_key():
        return None

    async def override_strict_api_key():
        return None

    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[check_api_key] = override_api_key
    app.dependency_overrides[check_strict_api_key] = override_strict_api_key

    transport = ASGITransport(app=app)

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, clean_session
    finally:
        app.dependency_overrides.clear()


async def _seed_node_model(session):
    """创建一条节点和节点模型测试数据。"""
    node = OpenAINode(url="http://quota-node.example.com", name="quota-node")
    session.add(node)
    await session.flush()

    node_model = OpenAINodeModel(
        node_id=node.id,
        model_name="gpt-4o-mini",
        model_type=ModelType.chat,
        enabled=True,
    )
    session.add(node_model)
    await session.commit()
    await session.refresh(node)
    await session.refresh(node_model)
    return node, node_model


async def _seed_api_key(session, ownerapp_id: str = "quota-app"):
    """创建一条 API Key 测试数据。"""
    api_key = ApiKey(
        name=f"{ownerapp_id}-key",
        description="quota key",
        ownerapp_id=ownerapp_id,
        key=None,
        key_hash=hash_api_key(ownerapp_id, f"{ownerapp_id}-secret"),
        key_prefix=ownerapp_id[:8],
        key_version=2,
        created_at=current_time_in_timezone(),
        enabled=True,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return api_key


@pytest.mark.asyncio
async def test_node_model_quota_crud_and_usage(api_client):
    """验证节点模型配额接口的 CRUD 与使用记录查询。"""
    client, clean_session = api_client
    node, node_model = await _seed_node_model(clean_session)

    create_resp = await client.post(
        "/quotas",
        json={
            "node_model_id": str(node_model.id),
            "order_id": " order-001 ",
            "call_limit": 10,
            "total_tokens_limit": 1000,
        },
    )
    assert create_resp.status_code == 200
    quota_payload = create_resp.json()
    quota_id = quota_payload["id"]
    assert quota_payload["order_id"] == "order-001"

    usage_now = current_time_in_timezone()
    clean_session.add(
        NodeModelQuotaUsage(
            quota_id=quota_payload["id"],
            node_id=node.id,
            node_model_id=node_model.id,
            proxy_id=None,
            nodelog_id=None,
            ownerapp_id="quota-app",
            request_action="completions",
            call_count=1,
            request_tokens=12,
            response_tokens=18,
            total_tokens=30,
            created_at=usage_now,
            updated_at=usage_now,
        )
    )
    await clean_session.commit()

    list_resp = await client.get("/quotas", params={"node_model_id": str(node_model.id)})
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    usage_resp = await client.get("/quotas/usages", params={"quota_id": quota_id, "ownerapp_id": "quota-app"})
    assert usage_resp.status_code == 200
    usage_payload = usage_resp.json()
    assert usage_payload["total"] == 1
    assert usage_payload["data"][0]["request_action"] == "completions"
    assert usage_payload["data"][0]["total_tokens"] == 30

    detail_resp = await client.get(f"/quotas/{quota_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["node_model_id"] == str(node_model.id)

    update_resp = await client.post(
        f"/quotas/{quota_id}",
        json={
            "order_id": " order-002 ",
            "call_limit": 20,
            "total_tokens_limit": 2000,
        },
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["order_id"] == "order-002"
    assert update_resp.json()["call_limit"] == 20

    delete_resp = await client.delete(f"/quotas/{quota_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {"code": 0, "message": "删除成功"}
    stored_quota = await clean_session.get(NodeModelQuota, quota_payload["id"])
    assert stored_quota is not None
    assert stored_quota.expired_at is not None


@pytest.mark.asyncio
async def test_apikey_quota_crud_and_usage(api_client):
    """验证 API Key 配额接口的 CRUD 与使用记录查询。"""
    client, clean_session = api_client
    api_key = await _seed_api_key(clean_session, ownerapp_id="quota-owner")

    create_resp = await client.post(
        "/apikey-quotas",
        json={
            "api_key_id": str(api_key.id),
            "order_id": " order-ak-1 ",
            "call_limit": 8,
            "total_tokens_limit": 800,
        },
    )
    assert create_resp.status_code == 200
    quota_payload = create_resp.json()
    quota_id = quota_payload["id"]
    assert quota_payload["order_id"] == "order-ak-1"

    usage_now = current_time_in_timezone()
    clean_session.add(
        ApiKeyQuotaUsage(
            quota_id=quota_payload["id"],
            api_key_id=api_key.id,
            proxy_id=None,
            nodelog_id=None,
            ownerapp_id="quota-owner",
            model_name="gpt-4o-mini",
            request_action="completions",
            call_count=2,
            total_tokens=64,
            created_at=usage_now,
            updated_at=usage_now,
        )
    )
    await clean_session.commit()

    list_resp = await client.get("/apikey-quotas", params={"api_key_id": str(api_key.id)})
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    usage_resp = await client.get(
        "/apikey-quotas/usages",
        params={"quota_id": quota_id, "ownerapp_id": "quota-owner"},
    )
    assert usage_resp.status_code == 200
    usage_payload = usage_resp.json()
    assert usage_payload["total"] == 1
    assert usage_payload["data"][0]["model_name"] == "gpt-4o-mini"
    assert usage_payload["data"][0]["total_tokens"] == 64

    detail_resp = await client.get(f"/apikey-quotas/{quota_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["api_key_id"] == str(api_key.id)

    update_resp = await client.post(
        f"/apikey-quotas/{quota_id}",
        json={
            "order_id": " order-ak-2 ",
            "call_limit": 16,
            "total_tokens_limit": 1600,
        },
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["order_id"] == "order-ak-2"
    assert update_resp.json()["call_limit"] == 16

    delete_resp = await client.delete(f"/apikey-quotas/{quota_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {"code": 0, "message": "删除成功"}
    stored_quota = await clean_session.get(ApiKeyQuota, quota_payload["id"])
    assert stored_quota is not None
    assert stored_quota.expired_at is not None


@pytest.mark.asyncio
async def test_app_quota_crud_and_usage(api_client):
    """验证应用配额接口的 CRUD 与使用记录查询。"""
    client, clean_session = api_client
    api_key = await _seed_api_key(clean_session, ownerapp_id="app-quota-owner")

    create_resp = await client.post(
        "/app-quotas",
        json={
            "ownerapp_id": "app-quota-owner",
            "order_id": " app-order-1 ",
            "call_limit": 6,
            "total_tokens_limit": 600,
        },
    )
    assert create_resp.status_code == 200
    quota_payload = create_resp.json()
    quota_id = quota_payload["id"]
    assert quota_payload["order_id"] == "app-order-1"

    usage_now = current_time_in_timezone()
    clean_session.add(
        AppQuotaUsage(
            quota_id=quota_payload["id"],
            ownerapp_id="app-quota-owner",
            api_key_id=api_key.id,
            proxy_id=None,
            nodelog_id=None,
            model_name="gpt-4o-mini",
            request_action="embeddings",
            call_count=3,
            total_tokens=72,
            created_at=usage_now,
            updated_at=usage_now,
        )
    )
    await clean_session.commit()

    list_resp = await client.get("/app-quotas", params={"ownerapp_id": "app-quota-owner"})
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    usage_resp = await client.get(
        "/app-quotas/usages",
        params={"quota_id": quota_id, "ownerapp_id": "app-quota-owner"},
    )
    assert usage_resp.status_code == 200
    usage_payload = usage_resp.json()
    assert usage_payload["total"] == 1
    assert usage_payload["data"][0]["request_action"] == "embeddings"
    assert usage_payload["data"][0]["total_tokens"] == 72

    detail_resp = await client.get(f"/app-quotas/{quota_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["ownerapp_id"] == "app-quota-owner"

    update_resp = await client.post(
        f"/app-quotas/{quota_id}",
        json={
            "order_id": " app-order-2 ",
            "call_limit": 12,
            "total_tokens_limit": 1200,
            "expired_at": (current_time_in_timezone() + timedelta(days=30)).isoformat(),
        },
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["order_id"] == "app-order-2"
    assert update_resp.json()["call_limit"] == 12

    delete_resp = await client.delete(f"/app-quotas/{quota_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {"code": 0, "message": "删除成功"}
    stored_quota = await clean_session.get(AppQuota, quota_payload["id"])
    assert stored_quota is not None
    assert stored_quota.expired_at is not None