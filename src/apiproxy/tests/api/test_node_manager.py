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

from openaiproxy.api.utils import check_api_key
from openaiproxy.services.database.models import (
	Node as OpenAINode,
	NodeModel as OpenAINodeModel,
	ProxyInstance,
	ProxyNodeStatus,
	ProxyNodeStatusLog,
)
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.services.database.models.node.model import ModelType, ProtocolType
from openaiproxy.services.deps import get_async_session, get_node_proxy_service
from openaiproxy.utils.apikey import decrypt_api_key
from openaiproxy.utils.timezone import current_time_in_timezone

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
	from openaiproxy.main import setup_app

	app = setup_app(backend_only=True)
	dummy_manager = DummyNodeManager()

	async def override_session():
		yield clean_session

	async def override_api_key():
		return None

	app.dependency_overrides[get_async_session] = override_session
	app.dependency_overrides[get_node_proxy_service] = lambda: dummy_manager
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
			yield client, dummy_manager, clean_session
	finally:
		app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_node_crud_flow(api_client):
	client, _, session = api_client

	list_resp = await client.get("/nodes")
	assert list_resp.status_code == 200
	assert list_resp.json()["total"] == 0

	node_payload = {
		"url": "http://node.example.com",
		"name": "example-node",
		"description": "demo node",
		"api_key": "secret",
		"protocol_type": ProtocolType.anthropic.value,
		"request_proxy_url": "https://proxy.example.com:8443",
		"verify": False,
	}
	create_resp = await client.post("/nodes", json=node_payload)
	assert create_resp.status_code == 200
	created_node = create_resp.json()
	node_id = UUID(created_node["id"])
	assert created_node["enabled"] is True
	assert created_node["trusted_without_models_endpoint"] is False
	assert created_node["protocol_type"] == ProtocolType.anthropic.value
	assert created_node["request_proxy_url"] == node_payload["request_proxy_url"]
	stored_node = await session.get(OpenAINode, node_id)
	assert stored_node is not None
	assert stored_node.api_key is not None
	assert stored_node.api_key != node_payload["api_key"]
	assert decrypt_api_key(stored_node.api_key) == node_payload["api_key"]
	assert stored_node.trusted_without_models_endpoint is False
	assert stored_node.protocol_type == ProtocolType.anthropic
	assert stored_node.request_proxy_url == node_payload["request_proxy_url"]

	list_resp = await client.get("/nodes")
	assert list_resp.status_code == 200
	payload = list_resp.json()
	assert payload["total"] == 1
	assert payload["data"][0]["id"] == str(node_id)
	assert payload["data"][0]["api_key"] == node_payload["api_key"]

	query_resp = await client.post("/nodes/query", params={"url": node_payload["url"]})
	assert query_resp.status_code == 200
	assert query_resp.json()["id"] == str(node_id)
	assert query_resp.json()["api_key"] == node_payload["api_key"]

	detail_resp = await client.get(f"/nodes/{node_id}")
	assert detail_resp.status_code == 200
	assert detail_resp.json()["name"] == node_payload["name"]
	assert detail_resp.json()["api_key"] == node_payload["api_key"]

	update_resp = await client.post(f"/nodes/{node_id}", json={"name": "updated-node"})
	assert update_resp.status_code == 200
	assert update_resp.json()["name"] == "updated-node"
	assert update_resp.json()["url"] == node_payload["url"]

	update_url_resp = await client.post(
		f"/nodes/{node_id}",
		json={"url": "http://updated-node.example.com", "verify": False},
	)
	assert update_url_resp.status_code == 200
	assert update_url_resp.json()["url"] == "http://updated-node.example.com"

	updated_detail_resp = await client.get(f"/nodes/{node_id}")
	assert updated_detail_resp.status_code == 200
	assert updated_detail_resp.json()["url"] == "http://updated-node.example.com"

	updated_query_resp = await client.post("/nodes/query", params={"url": "http://updated-node.example.com"})
	assert updated_query_resp.status_code == 200
	assert updated_query_resp.json()["id"] == str(node_id)

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
async def test_delete_disabled_node_with_only_healthcheck_logs(api_client):
	"""验证禁用节点只存在心跳检查日志时可连同日志一起删除。"""
	client, _, session = api_client
	node_resp = await client.post(
		"/nodes",
		json={
			"url": f"http://healthcheck-only-node-{uuid4()}.example.com",
			"name": "healthcheck-only-node",
			"verify": False,
		},
	)
	assert node_resp.status_code == 200
	node_id = UUID(node_resp.json()["id"])

	disable_resp = await client.post(f"/nodes/{node_id}", json={"enabled": False})
	assert disable_resp.status_code == 200

	proxy = ProxyInstance(instance_name=f"proxy-{uuid4()}", instance_ip="127.0.0.1")
	status_row = ProxyNodeStatus(node_id=node_id, proxy_id=proxy.id, avaiaible=False)
	status_id = status_row.id
	now = current_time_in_timezone()
	log_row = ProxyNodeStatusLog(
		node_id=node_id,
		proxy_id=proxy.id,
		status_id=status_id,
		ownerapp_id=None,
		request_protocol=ProtocolType.openai,
		model_name=None,
		action=RequestAction.healthcheck,
		start_at=now,
		end_at=now,
		latency=0.1,
		stream=False,
		request_tokens=0,
		response_tokens=0,
		total_tokens=0,
		error=False,
		abort=False,
	)
	log_id = log_row.id
	session.add(proxy)
	session.add(status_row)
	session.add(log_row)
	await session.commit()

	delete_resp = await client.delete(f"/nodes/{node_id}")
	assert delete_resp.status_code == 200
	assert delete_resp.json() == {"code": 0, "message": "删除成功"}
	session.expire_all()
	assert await session.get(OpenAINode, node_id) is None
	assert await session.get(ProxyNodeStatus, status_id) is None
	assert await session.get(ProxyNodeStatusLog, log_id) is None


@pytest.mark.asyncio
async def test_delete_disabled_node_with_non_healthcheck_logs_still_rejected(api_client):
	"""验证节点存在非心跳检查日志时仍然拒绝删除。"""
	client, _, session = api_client
	node_resp = await client.post(
		"/nodes",
		json={
			"url": f"http://request-log-node-{uuid4()}.example.com",
			"name": "request-log-node",
			"verify": False,
		},
	)
	assert node_resp.status_code == 200
	node_id = UUID(node_resp.json()["id"])

	disable_resp = await client.post(f"/nodes/{node_id}", json={"enabled": False})
	assert disable_resp.status_code == 200

	proxy = ProxyInstance(instance_name=f"proxy-{uuid4()}", instance_ip="127.0.0.1")
	status_row = ProxyNodeStatus(node_id=node_id, proxy_id=proxy.id, avaiaible=False)
	status_id = status_row.id
	now = current_time_in_timezone()
	log_row = ProxyNodeStatusLog(
		node_id=node_id,
		proxy_id=proxy.id,
		status_id=status_id,
		ownerapp_id="app-test",
		request_protocol=ProtocolType.openai,
		model_name="gpt-4o-mini",
		action=RequestAction.completions,
		start_at=now,
		end_at=now,
		latency=0.2,
		stream=False,
		request_tokens=10,
		response_tokens=20,
		total_tokens=30,
		error=False,
		abort=False,
	)
	log_id = log_row.id
	session.add(proxy)
	session.add(status_row)
	session.add(log_row)
	await session.commit()

	delete_resp = await client.delete(f"/nodes/{node_id}")
	assert delete_resp.status_code == 400
	assert delete_resp.json()["detail"] == "节点存在非心跳检查日志，请先清理日志后再删除"
	session.expire_all()
	assert await session.get(OpenAINode, node_id) is not None
	assert await session.get(ProxyNodeStatus, status_id) is not None
	assert await session.get(ProxyNodeStatusLog, log_id) is not None


@pytest.mark.asyncio
async def test_update_node_runtime_config_triggers_verification(api_client, monkeypatch):
	"""验证更新节点协议配置时会携带新的运行时参数重新校验。"""
	client, _, _ = api_client

	create_resp = await client.post(
		"/nodes",
		json={
			"url": "http://runtime-config-node.example.com",
			"name": "runtime-config-node",
			"verify": False,
		},
	)
	assert create_resp.status_code == 200
	node_id = create_resp.json()["id"]

	verify_calls: list[dict[str, object]] = []

	async def fake_verify(**kwargs):
		verify_calls.append(kwargs)

	monkeypatch.setattr("openaiproxy.api.node_manager._verify_node_protocols", fake_verify)

	update_resp = await client.post(
		f"/nodes/{node_id}",
		json={
			"protocol_type": ProtocolType.both.value,
			"request_proxy_url": "https://proxy.example.com:9443",
			"verify": True,
		},
	)
	assert update_resp.status_code == 200
	assert update_resp.json()["protocol_type"] == ProtocolType.both.value
	assert update_resp.json()["request_proxy_url"] == "https://proxy.example.com:9443"
	assert len(verify_calls) == 1
	assert verify_calls[0]["node_url"] == "http://runtime-config-node.example.com"
	assert verify_calls[0]["protocol_type"] == ProtocolType.both
	assert verify_calls[0]["request_proxy_url"] == "https://proxy.example.com:9443"


@pytest.mark.asyncio
async def test_fetch_node_models_uses_stored_runtime_config(api_client, monkeypatch):
	"""验证节点模型探测接口会读取节点保存的协议与代理配置。"""
	client, _, _ = api_client
	request_calls: list[dict[str, object]] = []

	async def fake_request_models_payload(
		node_url: str,
		api_key: str | None,
		*,
		protocol_type: ProtocolType,
		request_proxy_url: str | None,
		error_status: int,
		error_prefix: str,
	):
		request_calls.append(
			{
				"node_url": node_url,
				"api_key": api_key,
				"protocol_type": protocol_type,
				"request_proxy_url": request_proxy_url,
				"error_status": error_status,
				"error_prefix": error_prefix,
			}
		)
		return {"data": [{"id": "claude-3-7-sonnet"}]}

	monkeypatch.setattr(
		"openaiproxy.api.node_manager._request_node_models_payload",
		fake_request_models_payload,
	)

	create_resp = await client.post(
		"/nodes",
		json={
			"url": "http://stored-runtime-node.example.com",
			"name": "stored-runtime-node",
			"api_key": "stored-secret",
			"protocol_type": ProtocolType.anthropic.value,
			"request_proxy_url": "https://proxy.example.com:9443",
			"verify": False,
		},
	)
	assert create_resp.status_code == 200
	node_id = create_resp.json()["id"]

	response = await client.post("/nodes/models", data={"node_id": node_id})
	assert response.status_code == 200
	assert response.json()["data"][0]["id"] == "claude-3-7-sonnet"
	assert len(request_calls) == 1
	assert request_calls[0]["node_url"] == "http://stored-runtime-node.example.com"
	assert request_calls[0]["api_key"] == "stored-secret"
	assert request_calls[0]["protocol_type"] == ProtocolType.anthropic
	assert request_calls[0]["request_proxy_url"] == "https://proxy.example.com:9443"


@pytest.mark.asyncio
async def test_fetch_node_models_accepts_direct_runtime_config(api_client, monkeypatch):
	"""验证节点模型探测接口支持直接提交临时运行时配置。"""
	client, _, _ = api_client
	request_calls: list[dict[str, object]] = []

	async def fake_request_models_payload(
		node_url: str,
		api_key: str | None,
		*,
		protocol_type: ProtocolType,
		request_proxy_url: str | None,
		error_status: int,
		error_prefix: str,
	):
		request_calls.append(
			{
				"node_url": node_url,
				"api_key": api_key,
				"protocol_type": protocol_type,
				"request_proxy_url": request_proxy_url,
				"error_status": error_status,
				"error_prefix": error_prefix,
			}
		)
		return {"data": [{"id": "gpt-4o-mini"}]}

	monkeypatch.setattr(
		"openaiproxy.api.node_manager._request_node_models_payload",
		fake_request_models_payload,
	)

	response = await client.post(
		"/nodes/models",
		data={
			"url": "http://direct-runtime-node.example.com",
			"api_key": "direct-secret",
			"protocol_type": ProtocolType.both.value,
			"request_proxy_url": "https://proxy.example.com:8080",
		},
	)
	assert response.status_code == 200
	assert response.json()["data"][0]["id"] == "gpt-4o-mini"
	assert len(request_calls) == 1
	assert request_calls[0]["node_url"] == "http://direct-runtime-node.example.com"
	assert request_calls[0]["api_key"] == "direct-secret"
	assert request_calls[0]["protocol_type"] == ProtocolType.both
	assert request_calls[0]["request_proxy_url"] == "https://proxy.example.com:8080"


@pytest.mark.asyncio
async def test_create_node_skips_models_verification_for_trusted_node(api_client, monkeypatch):
	client, _, session = api_client
	verify_calls: list[dict[str, object]] = []

	async def fake_request_models_payload(
		node_url: str,
		api_key: str | None,
		*,
		protocol_type: ProtocolType,
		request_proxy_url: str | None,
		error_status: int,
		error_prefix: str,
	):
		verify_calls.append(
			{
				"node_url": node_url,
				"api_key": api_key,
				"protocol_type": protocol_type,
				"request_proxy_url": request_proxy_url,
				"error_status": error_status,
				"error_prefix": error_prefix,
			}
		)
		return {"data": []}

	monkeypatch.setattr(
		"openaiproxy.api.node_manager._request_node_models_payload",
		fake_request_models_payload,
	)

	payload = {
		"url": "http://trusted-node.example.com",
		"name": "trusted-node",
		"api_key": "trusted-secret",
		"trusted_without_models_endpoint": True,
	}

	response = await client.post("/nodes", json=payload)
	assert response.status_code == 200
	assert response.json()["trusted_without_models_endpoint"] is True
	assert verify_calls == []


@pytest.mark.asyncio
async def test_update_node_skips_models_verification_when_trusted_flag_enabled(api_client, monkeypatch):
	client, _, _ = api_client
	verify_calls: list[dict[str, object]] = []

	async def fake_request_models_payload(
		node_url: str,
		api_key: str | None,
		*,
		protocol_type: ProtocolType,
		request_proxy_url: str | None,
		error_status: int,
		error_prefix: str,
	):
		verify_calls.append(
			{
				"node_url": node_url,
				"api_key": api_key,
				"protocol_type": protocol_type,
				"request_proxy_url": request_proxy_url,
				"error_status": error_status,
				"error_prefix": error_prefix,
			}
		)
		return {"data": []}

	monkeypatch.setattr(
		"openaiproxy.api.node_manager._request_node_models_payload",
		fake_request_models_payload,
	)

	create_resp = await client.post(
		"/nodes",
		json={
			"url": "http://update-trusted-node.example.com",
			"name": "update-trusted-node",
			"verify": False,
		},
	)
	assert create_resp.status_code == 200
	node_id = create_resp.json()["id"]

	update_resp = await client.post(
		f"/nodes/{node_id}",
		json={
			"api_key": "new-secret",
			"trusted_without_models_endpoint": True,
		},
	)
	assert update_resp.status_code == 200
	assert update_resp.json()["trusted_without_models_endpoint"] is True
	assert verify_calls == []


@pytest.mark.asyncio
async def test_node_model_crud_flow(api_client):
	client, _, _ = api_client

	node_payload = {"url": "http://model-node.example.com", "name": "model-node", "verify": False}
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
	client, _, _ = api_client
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
	client, dummy_manager, _ = api_client

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
