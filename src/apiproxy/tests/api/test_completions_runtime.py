from __future__ import annotations

from types import SimpleNamespace
from typing import Optional
from uuid import UUID

import orjson
import pytest
from httpx import ASGITransport, AsyncClient

from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.services.database.models.node.model import ModelType, ProtocolType
from openaiproxy.services.deps import get_node_proxy_service
from openaiproxy.services.nodeproxy.service import create_error_response


class DummyCompletionsNodeProxyService:
    """Lightweight proxy stub used by completions runtime failover tests."""

    def __init__(
        self,
        *,
        get_node_url_results: Optional[list[Optional[str]]] = None,
        response_payloads: Optional[list[dict]] = None,
        status: Optional[dict[str, SimpleNamespace]] = None,
    ) -> None:
        default_payload = {
            'id': 'chatcmpl-1',
            'object': 'chat.completion',
            'model': 'gpt-4o-mini',
            'choices': [
                {
                    'index': 0,
                    'message': {'role': 'assistant', 'content': 'done'},
                    'finish_reason': 'stop',
                }
            ],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5},
        }
        self.get_node_url_results = list(get_node_url_results or ['http://node.example.com'])
        self.response_payloads = list(response_payloads or [default_payload])
        self.status = status or {
            'http://node.example.com': SimpleNamespace(
                protocol_type=ProtocolType.openai,
                api_key='backend-token',
                request_proxy_url='https://proxy.example.com:8443',
            )
        }
        self.check_request_calls: list[dict] = []
        self.get_node_url_calls: list[dict] = []
        self.pre_call_calls: list[dict] = []
        self.generate_calls: list[dict] = []
        self.post_call_calls: list[dict] = []
        self.cleanup_calls: list[dict] = []

    async def check_request_model(
        self,
        model_name: str,
        model_type: Optional[str] = None,
        *,
        request_protocol: ProtocolType = ProtocolType.openai,
        allow_cross_protocol: bool = False,
        effective_allowed_models: Optional[list[str]] = None,
    ):
        self.check_request_calls.append(
            {
                'model_name': model_name,
                'model_type': model_type,
                'request_protocol': request_protocol,
                'allow_cross_protocol': allow_cross_protocol,
                'effective_allowed_models': effective_allowed_models,
            }
        )
        return None

    def get_node_url(
        self,
        model_name: str,
        model_type: Optional[str] = None,
        *,
        request_protocol: ProtocolType = ProtocolType.openai,
        allow_cross_protocol: bool = False,
        exclude_node_urls: Optional[set[str]] = None,
    ) -> Optional[str]:
        self.get_node_url_calls.append(
            {
                'model_name': model_name,
                'model_type': model_type,
                'request_protocol': request_protocol,
                'allow_cross_protocol': allow_cross_protocol,
                'exclude_node_urls': set(exclude_node_urls or ()),
            }
        )
        if self.get_node_url_results:
            return self.get_node_url_results.pop(0)
        return None

    def handle_unavailable_model(self, model_name: str, model_type: Optional[str] = None):
        del model_type
        return create_error_response(
            404,
            f'The model `{model_name}` does not exist.',
        )

    def pre_call(self, node_url: str, **kwargs):
        self.pre_call_calls.append({'node_url': node_url, **kwargs})
        return SimpleNamespace(
            first_response_time=None,
            request_tokens=None,
            response_tokens=None,
            total_tokens=None,
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
        self.generate_calls.append(
            {
                'request_payload': request_payload,
                'node_url': node_url,
                'endpoint': endpoint,
                'api_key': api_key,
                'protocol_type': protocol_type,
                'request_proxy_url': request_proxy_url,
            }
        )
        payload = self.response_payloads.pop(0) if len(self.response_payloads) > 1 else self.response_payloads[0]
        return orjson.dumps(payload).decode('utf-8')

    def cleanup_backend_capacity_exhausted_attempt(self, node_url: str, request_ctx, payload) -> None:
        self.cleanup_calls.append({'node_url': node_url, 'request_ctx': request_ctx, 'payload': payload})

    def post_call(self, node_url: str, request_ctx) -> None:
        self.post_call_calls.append({'node_url': node_url, 'request_ctx': request_ctx})


@pytest.fixture
def runtime_client_factory():
    """Create a runtime client with an overridable completions proxy service."""
    from openaiproxy.main import setup_app

    async def _build_client(*, nodeproxy: DummyCompletionsNodeProxyService, effective_allowed_models: Optional[list[str]] = None):
        app = setup_app(backend_only=True)

        async def override_access_key() -> AccessKeyContext:
            return AccessKeyContext(
                ownerapp_id='runtime-app',
                api_key_id=str(UUID('00000000-0000-0000-0000-000000000001')),
                request_protocol=ProtocolType.openai,
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
async def test_chat_completions_retries_next_node_when_backend_capacity_is_exhausted(runtime_client_factory):
    """Chat completions route should retry another node on backend quota exhaustion."""
    exhausted_payload = {
        'error': {
            'message': 'You exceeded your current quota.',
            'type': 'invalid_request_error',
            'code': 'insufficient_quota',
        }
    }
    success_payload = {
        'id': 'chatcmpl-2',
        'object': 'chat.completion',
        'model': 'gpt-4o-mini',
        'choices': [
            {
                'index': 0,
                'message': {'role': 'assistant', 'content': 'retry success'},
                'finish_reason': 'stop',
            }
        ],
        'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6},
    }
    nodeproxy = DummyCompletionsNodeProxyService(
        get_node_url_results=['http://node-a.example.com', 'http://node-b.example.com'],
        response_payloads=[exhausted_payload, success_payload],
        status={
            'http://node-a.example.com': SimpleNamespace(
                protocol_type=ProtocolType.openai,
                api_key='backend-token-a',
                request_proxy_url='https://proxy-a.example.com:8443',
            ),
            'http://node-b.example.com': SimpleNamespace(
                protocol_type=ProtocolType.openai,
                api_key='backend-token-b',
                request_proxy_url='https://proxy-b.example.com:8443',
            ),
        },
    )
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['gpt-4o-mini'],
    )

    try:
        response = await client.post(
            '/v1/chat/completions',
            json={
                'model': 'gpt-4o-mini',
                'messages': [{'role': 'user', 'content': 'hello'}],
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['choices'][0]['message']['content'] == 'retry success'
    assert nodeproxy.check_request_calls[0]['model_type'] == ModelType.chat.value
    assert len(nodeproxy.generate_calls) == 2
    assert nodeproxy.generate_calls[0]['node_url'] == 'http://node-a.example.com'
    assert nodeproxy.generate_calls[1]['node_url'] == 'http://node-b.example.com'
    assert nodeproxy.get_node_url_calls[1]['exclude_node_urls'] == {'http://node-a.example.com'}
    assert len(nodeproxy.cleanup_calls) == 1
    assert nodeproxy.cleanup_calls[0]['node_url'] == 'http://node-a.example.com'
    assert len(nodeproxy.post_call_calls) == 1
    assert nodeproxy.post_call_calls[0]['node_url'] == 'http://node-b.example.com'


@pytest.mark.asyncio
async def test_completions_retries_next_node_when_backend_capacity_is_exhausted(runtime_client_factory):
    """Legacy completions route should retry another node on backend quota exhaustion."""
    exhausted_payload = {
        'error': {
            'message': 'You exceeded your current quota.',
            'type': 'invalid_request_error',
            'code': 'insufficient_quota',
        }
    }
    success_payload = {
        'id': 'cmpl-2',
        'object': 'text_completion',
        'model': 'gpt-4o-mini',
        'choices': [
            {
                'index': 0,
                'text': 'retry success',
                'finish_reason': 'stop',
            }
        ],
        'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6},
    }
    nodeproxy = DummyCompletionsNodeProxyService(
        get_node_url_results=['http://node-a.example.com', 'http://node-b.example.com'],
        response_payloads=[exhausted_payload, success_payload],
        status={
            'http://node-a.example.com': SimpleNamespace(
                protocol_type=ProtocolType.openai,
                api_key='backend-token-a',
                request_proxy_url='https://proxy-a.example.com:8443',
            ),
            'http://node-b.example.com': SimpleNamespace(
                protocol_type=ProtocolType.openai,
                api_key='backend-token-b',
                request_proxy_url='https://proxy-b.example.com:8443',
            ),
        },
    )
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['gpt-4o-mini'],
    )

    try:
        response = await client.post(
            '/v1/completions',
            json={
                'model': 'gpt-4o-mini',
                'prompt': 'hello',
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['choices'][0]['text'] == 'retry success'
    assert len(nodeproxy.generate_calls) == 2
    assert nodeproxy.generate_calls[0]['endpoint'] == '/v1/completions'
    assert nodeproxy.generate_calls[1]['node_url'] == 'http://node-b.example.com'
    assert nodeproxy.get_node_url_calls[1]['exclude_node_urls'] == {'http://node-a.example.com'}
    assert len(nodeproxy.cleanup_calls) == 1
    assert nodeproxy.cleanup_calls[0]['node_url'] == 'http://node-a.example.com'
    assert len(nodeproxy.post_call_calls) == 1
    assert nodeproxy.post_call_calls[0]['node_url'] == 'http://node-b.example.com'