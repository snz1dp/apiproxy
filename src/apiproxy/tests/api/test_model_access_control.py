from __future__ import annotations

from http import HTTPStatus
from types import SimpleNamespace
from typing import Optional
from uuid import UUID

import orjson
import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import delete

from openaiproxy.api.utils import AccessKeyContext, check_access_key, check_strict_api_key
from openaiproxy.services.database.models.app.model import AppModelAccessPolicy
from openaiproxy.services.database.models.node.model import ProtocolType
from openaiproxy.services.deps import get_async_session, get_node_proxy_service
from openaiproxy.services.nodeproxy.service import create_error_response


class DummyNodeProxyService:
    """用于模型访问控制测试的轻量级节点代理。"""

    def __init__(self) -> None:
        self.status = {
            'http://node.example.com': SimpleNamespace(
                protocol_type=ProtocolType.openai,
                api_key='backend-token',
                request_proxy_url=None,
            )
        }

    def list_models_for_protocol(
        self,
        request_protocol: ProtocolType = ProtocolType.openai,
        *,
        allow_cross_protocol: bool = True,
    ) -> list[str]:
        del request_protocol, allow_cross_protocol
        return ['gpt-4o-mini', 'claude-3-5-sonnet']

    def filter_models_by_allowed_models(
        self,
        model_names: list[str],
        effective_allowed_models: Optional[list[str]] = None,
    ) -> list[str]:
        if effective_allowed_models is None:
            return list(dict.fromkeys(model_names))
        allowed_model_set = set(effective_allowed_models)
        return [model_name for model_name in model_names if model_name in allowed_model_set]

    async def check_request_model(
        self,
        model_name: str,
        model_type: Optional[str] = None,
        *,
        request_protocol: ProtocolType = ProtocolType.openai,
        allow_cross_protocol: bool = False,
        effective_allowed_models: Optional[list[str]] = None,
    ):
        del model_type, request_protocol, allow_cross_protocol
        if effective_allowed_models is not None and model_name not in set(effective_allowed_models):
            return create_error_response(
                HTTPStatus.FORBIDDEN,
                f'Access to model `{model_name}` is denied by access policy.',
                error_type='permission_error',
            )
        if model_name not in self.list_models_for_protocol():
            return create_error_response(
                HTTPStatus.NOT_FOUND,
                f'The model `{model_name}` does not exist.',
            )
        return None

    def get_node_url(
        self,
        model_name: str,
        model_type: Optional[str] = None,
        *,
        request_protocol: ProtocolType = ProtocolType.openai,
        allow_cross_protocol: bool = False,
    ) -> str:
        del model_name, model_type, request_protocol, allow_cross_protocol
        return 'http://node.example.com'

    def pre_call(self, *args, **kwargs):
        del args, kwargs
        return SimpleNamespace(
            first_response_time=None,
            request_tokens=0,
            response_tokens=0,
            total_tokens=0,
            error=False,
            error_message=None,
            error_stack=None,
            response_data=None,
            abort=False,
            request_data=None,
            log_id=None,
        )

    async def generate(
        self,
        request_payload,
        node_url: str,
        endpoint: str,
        api_key: Optional[str],
        *,
        protocol_type: ProtocolType,
        request_proxy_url: Optional[str] = None,
    ) -> str:
        del request_payload, node_url, endpoint, api_key, protocol_type, request_proxy_url
        return orjson.dumps(
            {
                'id': 'chatcmpl-test',
                'model': 'gpt-4o-mini',
                'choices': [
                    {
                        'index': 0,
                        'message': {'role': 'assistant', 'content': 'ok'},
                        'finish_reason': 'stop',
                    }
                ],
                'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5},
            }
        ).decode('utf-8')

    def post_call(self, node_url: str, request_ctx) -> None:
        del node_url, request_ctx


@pytest.fixture
async def management_client(session):
    from openaiproxy.main import setup_app

    await session.exec(delete(AppModelAccessPolicy))
    await session.commit()

    app = setup_app(backend_only=True)

    async def override_session():
        yield session

    async def override_strict_api_key():
        return None

    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[check_strict_api_key] = override_strict_api_key
    transport = ASGITransport(app=app)

    try:
        async with AsyncClient(transport=transport, base_url='http://testserver') as client:
            yield client, session
    finally:
        app.dependency_overrides.clear()
        await session.rollback()
        await session.exec(delete(AppModelAccessPolicy))
        await session.commit()


@pytest.fixture
def runtime_client_factory():
    from openaiproxy.main import setup_app

    async def _build_client(*, effective_allowed_models: Optional[list[str]], request_protocol: ProtocolType):
        app = setup_app(backend_only=True)
        nodeproxy = DummyNodeProxyService()

        async def override_access_key() -> AccessKeyContext:
            return AccessKeyContext(
                ownerapp_id='runtime-app',
                api_key_id=str(UUID('00000000-0000-0000-0000-000000000001')),
                request_protocol=request_protocol,
                api_key_allowed_models=effective_allowed_models,
                app_allowed_models=effective_allowed_models,
                effective_allowed_models=effective_allowed_models,
            )

        app.dependency_overrides[get_node_proxy_service] = lambda: nodeproxy
        app.dependency_overrides[check_access_key] = override_access_key
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url='http://testserver')
        return client, app

    return _build_client


@pytest.mark.asyncio
async def test_app_model_access_policy_api_crud(management_client):
    client, session = management_client

    create_resp = await client.post(
        '/app-model-access-policies',
        json={
            'ownerapp_id': 'app-policy-1',
            'allowed_models': [' gpt-4o-mini ', '', 'gpt-4o-mini'],
        },
    )
    assert create_resp.status_code == 200
    created = create_resp.json()
    policy_id = UUID(created['id'])
    assert created['allowed_models'] == ['gpt-4o-mini']

    duplicate_resp = await client.post(
        '/app-model-access-policies',
        json={'ownerapp_id': 'app-policy-1', 'allowed_models': ['claude-3-5-sonnet']},
    )
    assert duplicate_resp.status_code == 409

    list_resp = await client.get('/app-model-access-policies', params={'ownerapp_id': 'app-policy-1'})
    assert list_resp.status_code == 200
    assert list_resp.json()['total'] == 1

    detail_resp = await client.get(f'/app-model-access-policies/{policy_id}')
    assert detail_resp.status_code == 200
    assert detail_resp.json()['ownerapp_id'] == 'app-policy-1'

    update_resp = await client.post(
        f'/app-model-access-policies/{policy_id}',
        json={'allowed_models': []},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()['allowed_models'] == []

    stored = await session.get(AppModelAccessPolicy, policy_id)
    assert stored is not None
    assert stored.allowed_models is None


@pytest.mark.asyncio
async def test_v1_models_only_returns_effective_allowed_models(runtime_client_factory):
    client, app = await runtime_client_factory(
        effective_allowed_models=['gpt-4o-mini'],
        request_protocol=ProtocolType.openai,
    )
    try:
        response = await client.get('/v1/models')
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert [item['id'] for item in response.json()['data']] == ['gpt-4o-mini']


@pytest.mark.asyncio
async def test_openai_chat_request_rejects_model_outside_effective_allowed_models(runtime_client_factory):
    client, app = await runtime_client_factory(
        effective_allowed_models=['gpt-4o-mini'],
        request_protocol=ProtocolType.openai,
    )
    try:
        response = await client.post(
            '/v1/chat/completions',
            json={'model': 'claude-3-5-sonnet', 'messages': [{'role': 'user', 'content': 'hello'}]},
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()['message'] == 'Access to model `claude-3-5-sonnet` is denied by access policy.'


@pytest.mark.asyncio
async def test_openai_chat_request_allows_model_inside_effective_allowed_models(runtime_client_factory):
    client, app = await runtime_client_factory(
        effective_allowed_models=['gpt-4o-mini'],
        request_protocol=ProtocolType.openai,
    )
    try:
        response = await client.post(
            '/v1/chat/completions',
            json={'model': 'gpt-4o-mini', 'messages': [{'role': 'user', 'content': 'hello'}]},
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['choices'][0]['message']['content'] == 'ok'


@pytest.mark.asyncio
async def test_anthropic_count_tokens_rejects_model_outside_effective_allowed_models(runtime_client_factory):
    client, app = await runtime_client_factory(
        effective_allowed_models=['gpt-4o-mini'],
        request_protocol=ProtocolType.anthropic,
    )
    try:
        response = await client.post(
            '/v1/messages/count_tokens',
            json={'model': 'claude-3-5-sonnet', 'messages': [{'role': 'user', 'content': 'hello'}]},
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()['error']['message'] == 'Access to model `claude-3-5-sonnet` is denied by access policy.'


@pytest.mark.asyncio
async def test_anthropic_batch_rejects_model_outside_effective_allowed_models(runtime_client_factory):
    client, app = await runtime_client_factory(
        effective_allowed_models=['gpt-4o-mini'],
        request_protocol=ProtocolType.anthropic,
    )
    try:
        response = await client.post(
            '/v1/messages/batches',
            json={
                'requests': [
                    {
                        'custom_id': 'req-1',
                        'params': {
                            'model': 'claude-3-5-sonnet',
                            'messages': [{'role': 'user', 'content': 'hello'}],
                        },
                    }
                ]
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()['error']['message'] == 'Access to model `claude-3-5-sonnet` is denied by access policy.'