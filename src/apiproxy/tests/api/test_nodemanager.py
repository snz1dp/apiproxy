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

from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import delete

from openaiproxy.api.nodemanager_router import router as nodemanager_api_router
from openaiproxy.api.utils import check_api_key
from openaiproxy.services.database.models import (
	Node as OpenAINode,
	NodeModel as OpenAINodeModel,
)
from openaiproxy.services.database.models.node.model import ModelType
from openaiproxy.services.deps import get_async_session, get_node_manager

class DummyNodeManager:
	"""Lightweight stand-in for NodeManager used in legacy endpoint tests."""

	def __init__(self) -> None:
		self.status = {"running": True}
		self.add_calls: list[tuple[str, dict | None]] = []
		self.remove_calls: list[str] = []

	def add(self, url: str, status):
		item = status.model_dump() if hasattr(status, "model_dump") else status
		self.add_calls.append((url, item))
		return None

	def remove(self, url: str) -> None:
		self.remove_calls.append(url)
		return None


@pytest.fixture
async def clean_session(session):
	await session.exec(delete(OpenAINodeModel))
	await session.exec(delete(OpenAINode))
	await session.commit()
	try:
		yield session
	finally:
		await session.rollback()
		await session.exec(delete(OpenAINodeModel))
		await session.exec(delete(OpenAINode))
		await session.commit()


@pytest.fixture
async def api_client(clean_session):
	from openaiproxy.main import setup_app

	app = setup_app(backend_only=True)
	dummy_manager = DummyNodeManager()

	async def override_session():
		yield clean_session

	async def override_api_key():
		return None

	app.dependency_overrides[get_async_session] = override_session
	app.dependency_overrides[get_node_manager] = lambda: dummy_manager
	app.dependency_overrides[check_api_key] = override_api_key

	# Ensure legacy routes take precedence over dynamic UUID routes during tests
	legacy_paths = {"/nodes/status", "/nodes/add", "/nodes/remove"}
	for legacy_path in legacy_paths:
		for idx, route in enumerate(app.router.routes):
			if getattr(route, "path", None) == legacy_path:
				app.router.routes.insert(0, app.router.routes.pop(idx))
				break
	transport = ASGITransport(app=app)

	try:
		async with AsyncClient(transport=transport, base_url="http://testserver") as client:
			yield client, dummy_manager
	finally:
		app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_node_crud_flow(api_client):
	client, _ = api_client

	list_resp = await client.get("/nodes")
	assert list_resp.status_code == 200
	assert list_resp.json()["total"] == 0

	node_payload = {
		"url": "http://node.example.com",
		"name": "example-node",
		"description": "demo node",
		"api_key": "secret",
	}
	create_resp = await client.post("/nodes", json=node_payload)
	assert create_resp.status_code == 200
	created_node = create_resp.json()
	node_id = UUID(created_node["id"])
	assert created_node["enabled"] is True

	list_resp = await client.get("/nodes")
	assert list_resp.status_code == 200
	payload = list_resp.json()
	assert payload["total"] == 1
	assert payload["data"][0]["id"] == str(node_id)

	query_resp = await client.post("/nodes/query", params={"url": node_payload["url"]})
	assert query_resp.status_code == 200
	assert query_resp.json()["id"] == str(node_id)

	detail_resp = await client.get(f"/nodes/{node_id}")
	assert detail_resp.status_code == 200
	assert detail_resp.json()["name"] == node_payload["name"]

	update_resp = await client.post(f"/nodes/{node_id}", json={"name": "updated-node"})
	assert update_resp.status_code == 200
	assert update_resp.json()["name"] == "updated-node"

	delete_fail_resp = await client.delete(f"/nodes/{node_id}")
	assert delete_fail_resp.status_code == 400
	assert delete_fail_resp.json()["detail"] == "请先禁用节点后再删除"

	disable_resp = await client.post(f"/nodes/{node_id}", json={"enabled": False})
	assert disable_resp.status_code == 200
	assert disable_resp.json()["enabled"] is False

	delete_resp = await client.delete(f"/nodes/{node_id}")
	assert delete_resp.status_code == 200
	assert delete_resp.json() == {"code": 0, "message": "删除成功"}

	list_after_resp = await client.get("/nodes")
	assert list_after_resp.status_code == 200
	assert list_after_resp.json()["total"] == 0

	missing_detail = await client.get(f"/nodes/{node_id}")
	assert missing_detail.status_code == 404

	missing_query = await client.post("/nodes/query", params={"url": node_payload["url"]})
	assert missing_query.status_code == 404


@pytest.mark.asyncio
async def test_node_model_crud_flow(api_client):
	client, _ = api_client

	node_payload = {"url": "http://model-node.example.com", "name": "model-node"}
	node_resp = await client.post("/nodes", json=node_payload)
	node_id = UUID(node_resp.json()["id"])

	empty_models_resp = await client.get(f"/nodes/{node_id}/models")
	assert empty_models_resp.status_code == 200
	assert empty_models_resp.json()["total"] == 0

	model_payload = {"model_name": "gpt-3.5", "model_type": ModelType.chat.value}
	create_model_resp = await client.post(f"/nodes/{node_id}/models", json=model_payload)
	assert create_model_resp.status_code == 200
	created_model = create_model_resp.json()
	model_id = UUID(created_model["id"])
	assert created_model["model_name"] == "gpt-3.5"

	list_resp = await client.get(f"/nodes/{node_id}/models")
	list_data = list_resp.json()
	assert list_resp.status_code == 200
	assert list_data["total"] == 1
	assert list_data["data"][0]["id"] == str(model_id)

	query_resp = await client.post(
		f"/nodes/{node_id}/models/query",
		params={"model_name": "gpt-3.5", "model_type": ModelType.chat.value},
	)
	assert query_resp.status_code == 200
	assert query_resp.json()["id"] == str(model_id)

	model_detail = await client.get(f"/nodes/{node_id}/models/{model_id}")
	assert model_detail.status_code == 200
	assert model_detail.json()["model_name"] == "gpt-3.5"

	update_resp = await client.post(
		f"/nodes/{node_id}/models/{model_id}",
		json={"enabled": True},
	)
	assert update_resp.status_code == 200
	assert update_resp.json()["enabled"] is True

	delete_fail_resp = await client.delete(f"/nodes/{node_id}/models/{model_id}")
	assert delete_fail_resp.status_code == 400
	assert delete_fail_resp.json()["detail"] == "请先禁用节点模型后再删除"

	disable_resp = await client.post(
		f"/nodes/{node_id}/models/{model_id}",
		json={"enabled": False},
	)
	assert disable_resp.status_code == 200
	assert disable_resp.json()["enabled"] is False

	delete_resp = await client.delete(f"/nodes/{node_id}/models/{model_id}")
	assert delete_resp.status_code == 200
	assert delete_resp.json() == {"code": 0, "message": "删除成功"}

	list_after_resp = await client.get(f"/nodes/{node_id}/models")
	assert list_after_resp.status_code == 200
	assert list_after_resp.json()["total"] == 0

	missing_model_query = await client.post(
		f"/nodes/{node_id}/models/query",
		params={"model_name": "gpt-3.5", "model_type": ModelType.chat.value},
	)
	assert missing_model_query.status_code == 404


@pytest.mark.asyncio
async def test_node_model_routes_validate_node(api_client):
	client, _ = api_client
	fake_node_id = uuid4()

	missing_node_list = await client.get(f"/nodes/{fake_node_id}/models")
	assert missing_node_list.status_code == 404

	create_resp = await client.post(
		f"/nodes/{fake_node_id}/models",
		json={"model_name": "orphan-model", "model_type": ModelType.chat.value},
	)
	assert create_resp.status_code == 400


@pytest.mark.asyncio
async def test_legacy_node_manager_endpoints(api_client):
	client, dummy_manager = api_client

	status_resp = await client.get("/nodes/status")
	assert status_resp.status_code == 200, status_resp.json()
	assert status_resp.json() == dummy_manager.status

	legacy_node_payload = {"url": "http://legacy-node", "status": {"models": ["demo"]}}
	add_resp = await client.post("/nodes/add", json=legacy_node_payload)
	assert add_resp.status_code == 200
	assert add_resp.json() == "Added successfully"
	assert dummy_manager.add_calls[-1][0] == legacy_node_payload["url"]
	assert dummy_manager.add_calls[-1][1]["models"] == legacy_node_payload["status"]["models"]

	remove_resp = await client.post("/nodes/remove", params={"node_url": legacy_node_payload["url"]})
	assert remove_resp.status_code == 200
	assert remove_resp.json() == "Deleted successfully"
	assert dummy_manager.remove_calls[-1] == legacy_node_payload["url"]
