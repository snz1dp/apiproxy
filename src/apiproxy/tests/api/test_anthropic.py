from __future__ import annotations

from types import SimpleNamespace
from typing import Optional
from uuid import UUID

import orjson
import pytest
from httpx import ASGITransport, AsyncClient

from openaiproxy.api.v1 import anthropic as anthropic_api
from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.services.database.models.node.model import ModelType, ProtocolType
from openaiproxy.services.deps import get_node_proxy_service


class DummyAnthropicNodeProxyService:
    """Lightweight proxy stub for Anthropic runtime compatibility tests."""

    def __init__(
        self,
        *,
        get_node_url_results: Optional[list[Optional[str]]] = None,
        response_payloads: Optional[list[dict]] = None,
        status: Optional[dict[str, SimpleNamespace]] = None,
    ) -> None:
        default_success_payload = {
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
            'usage': {'prompt_tokens': 12, 'completion_tokens': 4, 'total_tokens': 16},
        }
        self.get_node_url_results = list(get_node_url_results or [])
        self.response_payloads = list(response_payloads or [default_success_payload])
        self.status = status or {
            'http://node.example.com': SimpleNamespace(
                protocol_type=ProtocolType.openai,
                api_key='backend-token',
                request_proxy_url=None,
            )
        }
        self.check_request_calls: list[dict] = []
        self.get_node_url_calls: list[dict] = []
        self.pre_call_calls: list[dict] = []
        self.generate_calls: list[dict] = []
        self.post_call_calls: list[dict] = []
        self.cleanup_calls: list[dict] = []
        self.mark_unavailable_calls: list[dict] = []

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
        return 'http://node.example.com'

    def pre_call(self, node_url: str, **kwargs):
        self.pre_call_calls.append({'node_url': node_url, **kwargs})
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
        request_content: Optional[bytes] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> str:
        self.generate_calls.append(
            {
                'request_payload': request_payload,
                'node_url': node_url,
                'endpoint': endpoint,
                'api_key': api_key,
                'protocol_type': protocol_type,
                'request_proxy_url': request_proxy_url,
                'request_content': request_content,
                'extra_headers': extra_headers,
            }
        )
        payload = self.response_payloads.pop(0) if len(self.response_payloads) > 1 else self.response_payloads[0]
        return orjson.dumps(payload).decode('utf-8')

    def cleanup_backend_capacity_exhausted_attempt(self, node_url: str, request_ctx, payload) -> None:
        self.cleanup_calls.append({'node_url': node_url, 'request_ctx': request_ctx, 'payload': payload})

    def mark_backend_node_unavailable(self, node_url: str, *, reason: Optional[str] = None) -> bool:
        self.mark_unavailable_calls.append({'node_url': node_url, 'reason': reason})
        return True

    def post_call(self, node_url: str, request_ctx) -> None:
        self.post_call_calls.append({'node_url': node_url, 'request_ctx': request_ctx})


@pytest.fixture
def runtime_client_factory():
    """Create a runtime client with an overridable Anthropc proxy service."""
    from openaiproxy.main import setup_app

    async def _build_client(*, nodeproxy: DummyAnthropicNodeProxyService, effective_allowed_models: Optional[list[str]] = None):
        anthropic_api._BATCH_STORE.clear()
        app = setup_app(backend_only=True)

        async def override_access_key() -> AccessKeyContext:
            return AccessKeyContext(
                ownerapp_id='runtime-app',
                api_key_id=str(UUID('00000000-0000-0000-0000-000000000001')),
                request_protocol=ProtocolType.anthropic,
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
async def test_anthropic_messages_converts_image_blocks_for_openai_backend(runtime_client_factory):
    """Anthropic messages route should convert image blocks into OpenAI content parts."""
    nodeproxy = DummyAnthropicNodeProxyService()
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['gpt-4o-mini'],
    )

    try:
        response = await client.post(
            '/v1/messages',
            json={
                'model': 'gpt-4o-mini',
                'system': 'follow instructions',
                'messages': [
                    {
                        'role': 'user',
                        'content': [
                            {'type': 'text', 'text': 'describe this image'},
                            {
                                'type': 'image',
                                'source': {
                                    'type': 'base64',
                                    'media_type': 'image/png',
                                    'data': 'abc123',
                                },
                            },
                        ],
                    }
                ],
                'max_tokens': 64,
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    response_payload = response.json()
    assert response_payload['type'] == 'message'
    assert response_payload['content'][0]['text'] == 'done'

    backend_request = nodeproxy.generate_calls[0]['request_payload']
    assert nodeproxy.check_request_calls[0]['model_type'] == ModelType.chat.value
    assert nodeproxy.get_node_url_calls[0]['request_protocol'] == ProtocolType.anthropic
    assert nodeproxy.generate_calls[0]['endpoint'] == '/v1/chat/completions'
    assert backend_request['messages'][0] == {'role': 'system', 'content': 'follow instructions'}
    assert backend_request['messages'][1]['content'] == [
        {'type': 'text', 'text': 'describe this image'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,abc123'}},
    ]


@pytest.mark.asyncio
async def test_anthropic_messages_retries_next_node_when_backend_capacity_is_exhausted(runtime_client_factory):
    """Anthropic messages route should retry another node on backend quota exhaustion."""
    exhausted_payload = {
        'type': 'error',
        'error': {
            'type': 'rate_limit_error',
            'message': 'credit_balance_too_low',
        },
        'code': 'insufficient_quota',
    }
    success_payload = {
        'id': 'msg_2',
        'type': 'message',
        'role': 'assistant',
        'model': 'claude-3-5-sonnet',
        'content': [
            {
                'type': 'text',
                'text': 'retry success',
            }
        ],
        'usage': {
            'input_tokens': 3,
            'output_tokens': 2,
        },
    }
    nodeproxy = DummyAnthropicNodeProxyService(
        get_node_url_results=[
            'http://node-a.example.com',
            'http://node-b.example.com',
        ],
        response_payloads=[
            exhausted_payload,
            success_payload,
        ],
        status={
            'http://node-a.example.com': SimpleNamespace(
                protocol_type=ProtocolType.anthropic,
                api_key='backend-token-a',
                request_proxy_url=None,
            ),
            'http://node-b.example.com': SimpleNamespace(
                protocol_type=ProtocolType.anthropic,
                api_key='backend-token-b',
                request_proxy_url=None,
            ),
        },
    )
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['claude-3-5-sonnet'],
    )

    try:
        response = await client.post(
            '/v1/messages',
            json={
                'model': 'claude-3-5-sonnet',
                'messages': [{'role': 'user', 'content': 'hello'}],
                'max_tokens': 64,
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['content'][0]['text'] == 'retry success'
    assert len(nodeproxy.generate_calls) == 2
    assert nodeproxy.generate_calls[0]['node_url'] == 'http://node-a.example.com'
    assert nodeproxy.generate_calls[1]['node_url'] == 'http://node-b.example.com'
    assert nodeproxy.get_node_url_calls[1]['exclude_node_urls'] == {'http://node-a.example.com'}
    assert len(nodeproxy.cleanup_calls) == 1
    assert nodeproxy.cleanup_calls[0]['node_url'] == 'http://node-a.example.com'
    assert len(nodeproxy.post_call_calls) == 1
    assert nodeproxy.post_call_calls[0]['node_url'] == 'http://node-b.example.com'


@pytest.mark.asyncio
async def test_anthropic_count_tokens_retries_next_node_when_backend_capacity_is_exhausted(
    runtime_client_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    """Anthropic count_tokens should retry another native node on backend quota exhaustion."""
    exhausted_payload = {
        'type': 'error',
        'error': {
            'type': 'rate_limit_error',
            'message': 'credit_balance_too_low',
        },
        'code': 'insufficient_quota',
    }
    success_payload = {
        'input_tokens': 42,
    }
    native_payloads = [exhausted_payload, success_payload]

    async def fake_native_request(**kwargs):
        del kwargs
        payload = native_payloads.pop(0) if len(native_payloads) > 1 else native_payloads[0]
        return payload

    monkeypatch.setattr(anthropic_api, '_request_native_anthropic_json', fake_native_request)

    nodeproxy = DummyAnthropicNodeProxyService(
        get_node_url_results=[
            'http://node-a.example.com',
            'http://node-b.example.com',
        ],
        status={
            'http://node-a.example.com': SimpleNamespace(
                protocol_type=ProtocolType.anthropic,
                api_key='backend-token-a',
                request_proxy_url=None,
            ),
            'http://node-b.example.com': SimpleNamespace(
                protocol_type=ProtocolType.anthropic,
                api_key='backend-token-b',
                request_proxy_url=None,
            ),
        },
    )
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['claude-3-5-sonnet'],
    )

    try:
        response = await client.post(
            '/v1/messages/count_tokens',
            json={
                'model': 'claude-3-5-sonnet',
                'messages': [{'role': 'user', 'content': 'hello'}],
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['input_tokens'] == 42
    assert nodeproxy.get_node_url_calls[1]['exclude_node_urls'] == {'http://node-a.example.com'}
    assert len(nodeproxy.mark_unavailable_calls) == 1
    assert nodeproxy.mark_unavailable_calls[0]['node_url'] == 'http://node-a.example.com'


@pytest.mark.asyncio
async def test_anthropic_native_batch_creation_retries_next_node_when_backend_capacity_is_exhausted(
    runtime_client_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    """Anthropic native batch creation should retry another node on backend quota exhaustion."""
    exhausted_payload = {
        'type': 'error',
        'error': {
            'type': 'rate_limit_error',
            'message': 'credit_balance_too_low',
        },
        'code': 'insufficient_quota',
    }
    success_payload = {
        'id': 'msgbatch_native_1',
        'type': 'message_batch',
        'processing_status': 'in_progress',
        'request_counts': {
            'processing': 1,
            'succeeded': 0,
            'errored': 0,
            'canceled': 0,
            'expired': 0,
        },
    }
    native_payloads = [exhausted_payload, success_payload]

    async def fake_native_request(**kwargs):
        del kwargs
        payload = native_payloads.pop(0) if len(native_payloads) > 1 else native_payloads[0]
        return payload

    monkeypatch.setattr(anthropic_api, '_request_native_anthropic_json', fake_native_request)

    nodeproxy = DummyAnthropicNodeProxyService(
        get_node_url_results=[
            'http://node-a.example.com',
            'http://node-b.example.com',
        ],
        status={
            'http://node-a.example.com': SimpleNamespace(
                protocol_type=ProtocolType.anthropic,
                api_key='backend-token-a',
                request_proxy_url=None,
            ),
            'http://node-b.example.com': SimpleNamespace(
                protocol_type=ProtocolType.anthropic,
                api_key='backend-token-b',
                request_proxy_url=None,
            ),
        },
    )
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['claude-3-5-sonnet'],
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
                            'max_tokens': 64,
                        },
                    }
                ]
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['id'] == 'msgbatch_native_1'
    assert nodeproxy.get_node_url_calls[1]['exclude_node_urls'] == {'http://node-a.example.com'}
    assert len(nodeproxy.mark_unavailable_calls) == 1
    assert nodeproxy.mark_unavailable_calls[0]['node_url'] == 'http://node-a.example.com'


@pytest.mark.asyncio
async def test_anthropic_synthetic_batch_creation_retries_next_node_when_backend_capacity_is_exhausted(
    runtime_client_factory,
):
    """Anthropic synthetic batch creation should retry another node on backend quota exhaustion."""
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
        'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5},
    }
    nodeproxy = DummyAnthropicNodeProxyService(
        get_node_url_results=[
            'http://node-a.example.com',
            'http://node-b.example.com',
        ],
        response_payloads=[
            exhausted_payload,
            success_payload,
        ],
        status={
            'http://node-a.example.com': SimpleNamespace(
                protocol_type=ProtocolType.openai,
                api_key='backend-token-a',
                request_proxy_url=None,
            ),
            'http://node-b.example.com': SimpleNamespace(
                protocol_type=ProtocolType.openai,
                api_key='backend-token-b',
                request_proxy_url=None,
            ),
        },
    )
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['gpt-4o-mini'],
    )

    try:
        create_response = await client.post(
            '/v1/messages/batches',
            json={
                'requests': [
                    {
                        'custom_id': 'req-1',
                        'params': {
                            'model': 'gpt-4o-mini',
                            'messages': [{'role': 'user', 'content': 'hello'}],
                            'max_tokens': 64,
                        },
                    }
                ]
            },
        )
        batch_id = create_response.json()['id']
        results_response = await client.get(f'/v1/messages/batches/{batch_id}/results')
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert create_response.status_code == 200
    assert results_response.status_code == 200
    assert results_response.json()['data'][0]['result']['message']['content'][0]['text'] == 'retry success'
    assert len(nodeproxy.generate_calls) == 2
    assert nodeproxy.generate_calls[0]['node_url'] == 'http://node-a.example.com'
    assert nodeproxy.generate_calls[1]['node_url'] == 'http://node-b.example.com'
    assert nodeproxy.get_node_url_calls[1]['exclude_node_urls'] == {'http://node-a.example.com'}
    assert len(nodeproxy.mark_unavailable_calls) == 1
    assert nodeproxy.mark_unavailable_calls[0]['node_url'] == 'http://node-a.example.com'