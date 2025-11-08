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

from datetime import timedelta
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from sqlmodel import delete
from starlette.requests import Request

from openaiproxy.api.utils import check_api_key, check_access_key
from openaiproxy.services.deps import get_async_session
from openaiproxy.services.database.models.apikey.model import ApiKey
from openaiproxy.utils.apikey import decrypt_api_key, parse_api_key_token
from openaiproxy.utils.timezone import current_time_in_timezone

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
async def test_apikey_crud_flow(api_client: AsyncClient, clean_session):
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
    assert created["ownerapp_id"] == payload["ownerapp_id"]
    assert "token" in created
    assert "key" not in created
    key_id = UUID(created["id"])

    stored = await clean_session.get(ApiKey, key_id)
    assert stored is not None
    plaintext = decrypt_api_key(stored.key)
    assert len(plaintext) == 12
    decrypted_token = decrypt_api_key(created["token"])
    owner_from_token, key_from_token = parse_api_key_token(decrypted_token)
    assert owner_from_token == payload["ownerapp_id"]
    assert key_from_token == plaintext

    list_resp = await api_client.get("/apikeys")
    assert list_resp.status_code == 200
    list_payload = list_resp.json()
    assert list_payload["total"] == 1
    first_item = list_payload["data"][0]
    assert first_item["id"] == str(key_id)
    assert "token" not in first_item
    assert "key" not in first_item

    detail_resp = await api_client.get(f"/apikeys/{key_id}")
    assert detail_resp.status_code == 200
    detail_payload = detail_resp.json()
    assert detail_payload["name"] == payload["name"]
    assert "token" not in detail_payload
    assert "key" not in detail_payload

    update_payload = {"description": "updated"}
    update_resp = await api_client.post(f"/apikeys/{key_id}", json=update_payload)
    assert update_resp.status_code == 200
    updated = update_resp.json()
    assert updated["description"] == "updated"
    assert updated["ownerapp_id"] == payload["ownerapp_id"]
    assert "token" not in updated
    assert "key" not in updated

    delete_resp = await api_client.delete(f"/apikeys/{key_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {"code": 0, "message": "删除成功"}

    missing_resp = await api_client.get(f"/apikeys/{key_id}")
    assert missing_resp.status_code == 404


@pytest.mark.asyncio
async def test_api_responses_do_not_expose_key(api_client: AsyncClient, clean_session):
    payload = {
        "name": "safety-check",
        "description": "safety",
        "ownerapp_id": "app-safe",
    }
    create_resp = await api_client.post("/apikeys", json=payload)
    assert create_resp.status_code == 200
    key_id = UUID(create_resp.json()["id"])

    list_resp = await api_client.get("/apikeys")
    assert list_resp.status_code == 200
    for item in list_resp.json()["data"]:
        assert "token" not in item
        assert "key" not in item

    detail_resp = await api_client.get(f"/apikeys/{key_id}")
    assert detail_resp.status_code == 200
    assert "token" not in detail_resp.json()
    assert "key" not in detail_resp.json()

    updated_resp = await api_client.post(
        f"/apikeys/{key_id}",
        json={"description": "still-safe"},
    )
    assert updated_resp.status_code == 200
    assert "token" not in updated_resp.json()
    assert "key" not in updated_resp.json()


@pytest.mark.asyncio
async def test_check_access_key_valid_flow(api_client: AsyncClient, clean_session):
    payload = {
        "name": "access-check",
        "description": "integration",
        "ownerapp_id": "owner-ctx",
    }
    create_resp = await api_client.post("/apikeys", json=payload)
    assert create_resp.status_code == 200
    created = create_resp.json()
    auth = HTTPAuthorizationCredentials(scheme="Bearer", credentials=created["token"])
    request = Request({"type": "http", "headers": []})
    context = await check_access_key(auth=auth, session=clean_session, request=request)
    assert context.ownerapp_id == payload["ownerapp_id"]
    assert request.state.ownerapp_id == payload["ownerapp_id"]
    assert context.record.id == UUID(created["id"])
    assert context.key


@pytest.mark.asyncio
async def test_check_access_key_invalid_token(clean_session):
    auth = HTTPAuthorizationCredentials(scheme="Bearer", credentials="invalid-token")
    request = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as exc:
        await check_access_key(auth=auth, session=clean_session, request=request)
    assert exc.value.status_code == 401
    assert exc.value.detail["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_check_access_key_expired(api_client: AsyncClient, clean_session):
    payload = {
        "name": "expired",
        "description": "expired",
        "ownerapp_id": "owner-expired",
    }
    create_resp = await api_client.post("/apikeys", json=payload)
    assert create_resp.status_code == 200
    created = create_resp.json()
    key_id = UUID(created["id"])
    stored = await clean_session.get(ApiKey, key_id)
    assert stored is not None
    stored.expires_at = current_time_in_timezone() - timedelta(seconds=1)
    clean_session.add(stored)
    await clean_session.commit()
    await clean_session.refresh(stored)
    auth = HTTPAuthorizationCredentials(scheme="Bearer", credentials=created["token"])
    request = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as exc:
        await check_access_key(auth=auth, session=clean_session, request=request)
    assert exc.value.status_code == 401
    assert exc.value.detail["error"]["code"] == "expired_api_key"
