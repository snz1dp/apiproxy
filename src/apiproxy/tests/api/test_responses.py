from __future__ import annotations

from http import HTTPStatus
from types import SimpleNamespace
from typing import Optional
from uuid import UUID

import orjson
import pytest
from httpx import ASGITransport, AsyncClient
from starlette.background import BackgroundTask

from openaiproxy.api.schemas import ResponsesRequest
from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.services.database.models.node.model import ModelType, ProtocolType
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.services.deps import get_node_proxy_service
from openaiproxy.services.nodeproxy.exceptions import NodeModelQuotaExceeded
from openaiproxy.services.nodeproxy.service import create_error_response


class DummyResponsesNodeProxyService:
    """Lightweight responses proxy stub used by runtime route tests."""

    def __init__(
        self,
        *,
        check_response=None,
        get_node_url_result: Optional[str] = 'http://node.example.com',
        get_node_url_results: Optional[list[Optional[str]]] = None,
        get_node_url_exception: Optional[BaseException] = None,
        pre_call_exception: Optional[BaseException] = None,
        response_payload: Optional[dict] = None,
        response_payloads: Optional[list[dict]] = None,
        stream_chunks: Optional[list[bytes]] = None,
        status: Optional[dict[str, SimpleNamespace]] = None,
    ) -> None:
        self.check_response = check_response
        self.get_node_url_result = get_node_url_result
        self.get_node_url_results = list(get_node_url_results or [])
        self.get_node_url_exception = get_node_url_exception
        self.pre_call_exception = pre_call_exception
        default_response_payload = response_payload or {
            'id': 'resp_1',
            'object': 'response',
            'model': 'gpt-4.1-mini',
            'output': [
                {
                    'id': 'msg_1',
                    'type': 'message',
                    'role': 'assistant',
                    'content': [
                        {
                            'type': 'output_text',
                            'text': 'hello world',
                        }
                    ],
                }
            ],
            'usage': {
                'input_tokens': 5,
                'output_tokens': 7,
                'total_tokens': 12,
            },
        }
        self.response_payload = default_response_payload
        self.response_payloads = list(response_payloads or [default_response_payload])
        self.stream_chunks = stream_chunks or [
            b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1"}}\n\n',
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hel"}\n\n',
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"lo"}\n\n',
            b'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":4,"output_tokens":2,"total_tokens":6}}}\n\n',
            b'data: [DONE]\n\n',
        ]
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
        self.stream_generate_calls: list[dict] = []
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
        return self.check_response

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
        if self.get_node_url_exception is not None:
            raise self.get_node_url_exception
        if self.get_node_url_results:
            return self.get_node_url_results.pop(0)
        return self.get_node_url_result

    def handle_unavailable_model(self, model_name: str, model_type: Optional[str] = None):
        del model_type
        return create_error_response(
            HTTPStatus.NOT_FOUND,
            f'The model `{model_name}` does not exist.',
        )

    def pre_call(self, node_url: str, **kwargs):
        self.pre_call_calls.append({'node_url': node_url, **kwargs})
        if self.pre_call_exception is not None:
            raise self.pre_call_exception
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
    ):
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

    def stream_generate(
        self,
        request_payload,
        node_url: str,
        endpoint: str,
        api_key: Optional[str],
        *,
        protocol_type: ProtocolType,
        request_proxy_url: Optional[str] = None,
    ):
        self.stream_generate_calls.append(
            {
                'request_payload': request_payload,
                'node_url': node_url,
                'endpoint': endpoint,
                'api_key': api_key,
                'protocol_type': protocol_type,
                'request_proxy_url': request_proxy_url,
            }
        )
        return iter(self.stream_chunks)

    def create_background_tasks(self, node_url: str, request_ctx):
        return BackgroundTask(self.post_call, node_url, request_ctx)

    def cleanup_backend_capacity_exhausted_attempt(self, node_url: str, request_ctx, payload) -> None:
        self.cleanup_calls.append(
            {
                'node_url': node_url,
                'request_ctx': request_ctx,
                'payload': payload,
            }
        )

    def post_call(self, node_url: str, request_ctx) -> None:
        self.post_call_calls.append(
            {
                'node_url': node_url,
                'request_ctx': request_ctx,
            }
        )


@pytest.fixture
def runtime_client_factory():
    """Create a runtime client with an overridable responses proxy service."""
    from openaiproxy.main import setup_app

    async def _build_client(*, nodeproxy: DummyResponsesNodeProxyService, effective_allowed_models: Optional[list[str]] = None):
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


def test_responses_request_preserves_extra_fields() -> None:
    """Responses schema should preserve provider-specific passthrough fields."""
    request = ResponsesRequest(
        model='gpt-4.1-mini',
        input='hello world',
        metadata={'source': 'tests'},
        reasoning={'effort': 'low'},
    )

    payload = request.model_dump(exclude_none=True)

    assert payload['metadata'] == {'source': 'tests'}
    assert payload['reasoning'] == {'effort': 'low'}


@pytest.mark.asyncio
async def test_responses_proxy_json_request_and_return_response(runtime_client_factory):
    """Responses route should proxy JSON payloads and preserve usage metadata."""
    nodeproxy = DummyResponsesNodeProxyService()
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['gpt-4.1-mini'],
    )

    try:
        response = await client.post(
            '/v1/responses',
            json={
                'model': 'gpt-4.1-mini',
                'input': 'say hello',
                'metadata': {'source': 'tests'},
                'max_output_tokens': 64,
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['id'] == 'resp_1'

    assert nodeproxy.check_request_calls[0]['model_type'] == ModelType.chat.value
    assert nodeproxy.check_request_calls[0]['allow_cross_protocol'] is False
    assert nodeproxy.get_node_url_calls[0]['allow_cross_protocol'] is False
    assert nodeproxy.generate_calls[0]['endpoint'] == '/v1/responses'
    assert nodeproxy.generate_calls[0]['request_payload']['metadata'] == {'source': 'tests'}
    assert nodeproxy.pre_call_calls[0]['request_action'] == RequestAction.responses
    assert nodeproxy.pre_call_calls[0]['request_count'] > 0
    assert nodeproxy.pre_call_calls[0]['estimated_total_tokens'] >= nodeproxy.pre_call_calls[0]['request_count']
    assert nodeproxy.post_call_calls[0]['request_ctx'].response_tokens == 7
    assert 'resp_1' in nodeproxy.post_call_calls[0]['request_ctx'].response_data


@pytest.mark.asyncio
async def test_responses_stream_preserves_sse_event_frames(runtime_client_factory):
    """Responses route should preserve typed SSE events for streaming clients."""
    nodeproxy = DummyResponsesNodeProxyService()
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['gpt-4.1-mini'],
    )

    try:
        response = await client.post(
            '/v1/responses',
            json={
                'model': 'gpt-4.1-mini',
                'input': 'say hello',
                'stream': True,
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    assert 'event: response.created' in response.text
    assert 'event: response.output_text.delta' in response.text
    assert 'data: [DONE]' in response.text

    assert nodeproxy.stream_generate_calls[0]['endpoint'] == '/v1/responses'
    assert nodeproxy.pre_call_calls[0]['request_action'] == RequestAction.responses
    assert nodeproxy.pre_call_calls[0]['stream'] is True
    assert len(nodeproxy.post_call_calls) == 1
    assert nodeproxy.post_call_calls[0]['request_ctx'].response_tokens == 2
    assert 'response.output_text.delta' in nodeproxy.post_call_calls[0]['request_ctx'].response_data


@pytest.mark.asyncio
async def test_responses_returns_quota_error_when_node_model_exhausted(runtime_client_factory):
    """Responses route should surface node model quota exhaustion consistently."""
    nodeproxy = DummyResponsesNodeProxyService(
        get_node_url_exception=NodeModelQuotaExceeded(
            '节点模型配额已耗尽',
            detail='gpt-4.1-mini (chat)',
        )
    )
    client, app = await runtime_client_factory(nodeproxy=nodeproxy)

    try:
        response = await client.post(
            '/v1/responses',
            json={
                'model': 'gpt-4.1-mini',
                'input': 'say hello',
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 429
    payload = response.json()
    assert payload['type'] == 'quota_exceeded'
    assert 'gpt-4.1-mini' in payload['message']


@pytest.mark.asyncio
async def test_responses_retries_next_node_when_backend_capacity_is_exhausted(runtime_client_factory):
    """Responses route should retry another node when the backend reports quota exhaustion."""
    exhausted_payload = {
        'error': {
            'message': 'You exceeded your current quota.',
            'type': 'invalid_request_error',
            'code': 'insufficient_quota',
        }
    }
    success_payload = {
        'id': 'resp_2',
        'object': 'response',
        'model': 'gpt-4.1-mini',
        'output': [
            {
                'id': 'msg_2',
                'type': 'message',
                'role': 'assistant',
                'content': [
                    {
                        'type': 'output_text',
                        'text': 'retry success',
                    }
                ],
            }
        ],
        'usage': {
            'input_tokens': 3,
            'output_tokens': 2,
            'total_tokens': 5,
        },
    }
    nodeproxy = DummyResponsesNodeProxyService(
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
        effective_allowed_models=['gpt-4.1-mini'],
    )

    try:
        response = await client.post(
            '/v1/responses',
            json={
                'model': 'gpt-4.1-mini',
                'input': 'say hello',
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['id'] == 'resp_2'
    assert len(nodeproxy.generate_calls) == 2
    assert nodeproxy.generate_calls[0]['node_url'] == 'http://node-a.example.com'
    assert nodeproxy.generate_calls[1]['node_url'] == 'http://node-b.example.com'
    assert nodeproxy.get_node_url_calls[1]['exclude_node_urls'] == {'http://node-a.example.com'}
    assert len(nodeproxy.cleanup_calls) == 1
    assert nodeproxy.cleanup_calls[0]['node_url'] == 'http://node-a.example.com'
    assert len(nodeproxy.post_call_calls) == 1
    assert nodeproxy.post_call_calls[0]['node_url'] == 'http://node-b.example.com'