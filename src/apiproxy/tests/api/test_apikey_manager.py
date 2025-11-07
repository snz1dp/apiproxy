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
from openaiproxy.services.database.models.apikey.model import ApiKey
from openaiproxy.services.deps import get_async_session


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
        "key": "test-key",
        "description": "demo key",
        "ownerapp_id": "app-1",
    }
    create_resp = await api_client.post("/apikeys", json=payload)
    assert create_resp.status_code == 200
    created = create_resp.json()
    key_id = UUID(created["id"])

    list_resp = await api_client.get("/apikeys")
    assert list_resp.status_code == 200
    list_payload = list_resp.json()
    assert list_payload["total"] == 1
    assert list_payload["data"][0]["id"] == str(key_id)

    detail_resp = await api_client.get(f"/apikeys/{key_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["name"] == payload["name"]

    query_resp = await api_client.post("/apikeys/query", params={"key": payload["key"]})
    assert query_resp.status_code == 200
    assert query_resp.json()["id"] == str(key_id)

    update_payload = {"description": "updated", "ownerapp_id": "app-2"}
    update_resp = await api_client.post(f"/apikeys/{key_id}", json=update_payload)
    assert update_resp.status_code == 200
    updated = update_resp.json()
    assert updated["description"] == "updated"
    assert updated["ownerapp_id"] == "app-2"

    delete_resp = await api_client.delete(f"/apikeys/{key_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {"code": 0, "message": "删除成功"}

    missing_resp = await api_client.get(f"/apikeys/{key_id}")
    assert missing_resp.status_code == 404


@pytest.mark.asyncio
async def test_update_apikey_detects_duplicates(api_client: AsyncClient):
    first_resp = await api_client.post("/apikeys", json={"name": "first", "key": "dup-key-1"})
    assert first_resp.status_code == 200

    second_resp = await api_client.post("/apikeys", json={"name": "second", "key": "dup-key-2"})
    assert second_resp.status_code == 200
    second_id = second_resp.json()["id"]

    dup_resp = await api_client.post(f"/apikeys/{second_id}", json={"key": "dup-key-1"})
    assert dup_resp.status_code == 400
    assert dup_resp.json()["detail"] == "API Key已存在"
