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

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import delete

from openaiproxy.api.utils import check_api_key
from openaiproxy.services.deps import get_async_session
from openaiproxy.services.database.models.apikey.model import ApiKey
from openaiproxy.utils.apikey import parse_api_key_token


@pytest.fixture
async def clean_session(session):
    await session.exec(delete(ApiKey))
    await session.commit()
    try:
        yield session
    finally:
        await session.rollback()
        await session.exec(delete(ApiKey))
        await session.commit()


@pytest.fixture
async def api_client(clean_session):
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
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_apikey_crud_flow(api_client: AsyncClient):
    list_resp = await api_client.get("/apikeys")
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 0

    payload = {
        "name": "primary",
        "description": "demo key",
        "ownerapp_id": "app-1",
    }
    create_resp = await api_client.post("/apikeys", json=payload)
    assert create_resp.status_code == 200
    created = create_resp.json()
    created_key = created["key"]
    ownerapp_id, raw_key = parse_api_key_token(created_key)
    assert ownerapp_id == payload["ownerapp_id"]
    assert len(raw_key) == 12
    key_id = UUID(created["id"])

    list_resp = await api_client.get("/apikeys")
    assert list_resp.status_code == 200
    list_payload = list_resp.json()
    assert list_payload["total"] == 1
    first_item = list_payload["data"][0]
    assert first_item["id"] == str(key_id)
    assert first_item["key"] == created_key

    detail_resp = await api_client.get(f"/apikeys/{key_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["name"] == payload["name"]
    assert detail_resp.json()["key"] == created_key

    query_resp = await api_client.post("/apikeys/query", params={"key": created_key})
    assert query_resp.status_code == 200
    assert query_resp.json()["id"] == str(key_id)
    assert query_resp.json()["key"] == created_key

    update_payload = {"description": "updated"}
    update_resp = await api_client.post(f"/apikeys/{key_id}", json=update_payload)
    assert update_resp.status_code == 200
    updated = update_resp.json()
    assert updated["description"] == "updated"
    assert updated["ownerapp_id"] == payload["ownerapp_id"]
    assert updated["key"] == created_key

    delete_resp = await api_client.delete(f"/apikeys/{key_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {"code": 0, "message": "删除成功"}

    missing_resp = await api_client.get(f"/apikeys/{key_id}")
    assert missing_resp.status_code == 404


@pytest.mark.asyncio
async def test_query_requires_composite_key(api_client: AsyncClient):
    payload = {
        "name": "composite-check",
        "ownerapp_id": "app-check",
    }
    create_resp = await api_client.post("/apikeys", json=payload)
    assert create_resp.status_code == 200
    created = create_resp.json()
    composite_key = created["key"]
    _, raw_key = parse_api_key_token(composite_key)

    invalid_resp = await api_client.post("/apikeys/query", params={"key": raw_key})
    assert invalid_resp.status_code == 400
    assert invalid_resp.json()["detail"] == "API Key格式错误"
