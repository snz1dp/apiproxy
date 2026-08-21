from __future__ import annotations

from http import HTTPStatus
from types import SimpleNamespace
from typing import Optional
from uuid import UUID

import orjson
import pytest
from httpx import ASGITransport, AsyncClient

from openaiproxy.api.schemas import ImageGenerationRequest
from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.services.database.models.node.model import ModelType, ProtocolType
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.services.deps import get_node_proxy_service
from openaiproxy.services.nodeproxy.exceptions import NodeModelQuotaExceeded
from openaiproxy.services.nodeproxy.service import create_error_response


class DummyImageNodeProxyService:
    """Lightweight image proxy stub used by runtime route tests."""

    def __init__(
        self,
        *,
        check_response=None,
        get_node_url_result: Optional[str] = 'http://node.example.com',
        get_node_url_results: Optional[list[Optional[str]]] = None,
        get_node_url_exception: Optional[BaseException] = None,
        pre_call_exception: Optional[BaseException] = None,
        backend_payload: Optional[dict] = None,
        response_payloads: Optional[list[dict]] = None,
        status: Optional[dict[str, SimpleNamespace]] = None,
    ) -> None:
        self.check_response = check_response
        self.get_node_url_result = get_node_url_result
        self.get_node_url_results = list(get_node_url_results or [])
        self.get_node_url_exception = get_node_url_exception
        self.pre_call_exception = pre_call_exception
        default_backend_payload = backend_payload or {
            'created': 1730000000,
            'data': [
                {
                    'url': 'https://example.com/generated.png',
                }
            ],
        }
        self.backend_payload = default_backend_payload
        self.response_payloads = list(response_payloads or [default_backend_payload])
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
    """Create a runtime client with an overridable image proxy service."""
    from openaiproxy.main import setup_app

    async def _build_client(*, nodeproxy: DummyImageNodeProxyService, effective_allowed_models: Optional[list[str]] = None):
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


def test_image_generation_request_preserves_extra_fields():
    """Image generation schema should preserve provider-specific passthrough fields."""
    request = ImageGenerationRequest(
        model='gpt-image-1',
        prompt='draw a cat',
        output_format='png',
        custom_provider_option='keep-me',
    )

    payload = request.model_dump(exclude_none=True)

    assert payload['custom_provider_option'] == 'keep-me'
    assert payload['output_format'] == 'png'


@pytest.mark.asyncio
async def test_images_generations_proxies_openai_request(runtime_client_factory):
    """Image generation route should forward payloads and record image-specific actions."""
    nodeproxy = DummyImageNodeProxyService()
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['gpt-image-1'],
    )

    try:
        response = await client.post(
            '/v1/images/generations',
            json={
                'model': 'gpt-image-1',
                'prompt': 'draw a cat',
                'size': '1024x1024',
                'custom_provider_option': 'keep-me',
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['data'][0]['url'] == 'https://example.com/generated.png'

    assert nodeproxy.check_request_calls[0]['model_type'] == ModelType.image_generation.value
    assert nodeproxy.get_node_url_calls[0]['model_type'] == ModelType.image_generation.value
    assert nodeproxy.generate_calls[0]['endpoint'] == '/v1/images/generations'
    assert nodeproxy.generate_calls[0]['request_payload']['custom_provider_option'] == 'keep-me'
    assert nodeproxy.pre_call_calls[0]['request_action'] == RequestAction.images_generations
    assert nodeproxy.pre_call_calls[0]['request_count'] == 0
    assert nodeproxy.pre_call_calls[0]['estimated_total_tokens'] is None
    assert nodeproxy.post_call_calls[0]['request_ctx'].response_data is not None


@pytest.mark.asyncio
async def test_images_generations_returns_quota_error_when_node_model_exhausted(runtime_client_factory):
    """Image generation route should surface node model quota exhaustion consistently."""
    nodeproxy = DummyImageNodeProxyService(
        get_node_url_exception=NodeModelQuotaExceeded(
            '节点模型配额已耗尽',
            detail='gpt-image-1 (image-generation)',
        )
    )
    client, app = await runtime_client_factory(nodeproxy=nodeproxy)

    try:
        response = await client.post(
            '/v1/images/generations',
            json={
                'model': 'gpt-image-1',
                'prompt': 'draw a cat',
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 429
    payload = response.json()
    assert payload['type'] == 'quota_exceeded'
    assert 'gpt-image-1' in payload['message']


@pytest.mark.asyncio
async def test_images_generations_retries_next_node_when_backend_capacity_is_exhausted(runtime_client_factory):
    """Image generation route should fail over to the next node on backend quota exhaustion."""
    exhausted_payload = {
        'error': {
            'message': 'You exceeded your current quota.',
            'type': 'invalid_request_error',
            'code': 'insufficient_quota',
        }
    }
    success_payload = {
        'created': 1730000001,
        'data': [
            {
                'url': 'https://example.com/retry.png',
            }
        ],
    }
    nodeproxy = DummyImageNodeProxyService(
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
        effective_allowed_models=['gpt-image-1'],
    )

    try:
        response = await client.post(
            '/v1/images/generations',
            json={
                'model': 'gpt-image-1',
                'prompt': 'draw a cat',
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['data'][0]['url'] == 'https://example.com/retry.png'
    assert len(nodeproxy.generate_calls) == 2
    assert nodeproxy.generate_calls[0]['node_url'] == 'http://node-a.example.com'
    assert nodeproxy.generate_calls[1]['node_url'] == 'http://node-b.example.com'
    assert nodeproxy.get_node_url_calls[1]['exclude_node_urls'] == {'http://node-a.example.com'}
    assert len(nodeproxy.cleanup_calls) == 1
    assert nodeproxy.cleanup_calls[0]['node_url'] == 'http://node-a.example.com'
    assert len(nodeproxy.post_call_calls) == 1
    assert nodeproxy.post_call_calls[0]['node_url'] == 'http://node-b.example.com'


@pytest.mark.asyncio
async def test_images_edits_proxies_multipart_request(runtime_client_factory):
    """Image edit route should passthrough multipart bodies and log edit actions."""
    nodeproxy = DummyImageNodeProxyService()
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['gpt-image-1'],
    )

    try:
        response = await client.post(
            '/v1/images/edits',
            data={
                'model': 'gpt-image-1',
                'prompt': 'edit the cat',
                'size': '1024x1024',
                'custom_provider_option': 'keep-me',
            },
            files=[
                ('image', ('source.png', b'image-bytes', 'image/png')),
                ('mask', ('mask.png', b'mask-bytes', 'image/png')),
            ],
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['data'][0]['url'] == 'https://example.com/generated.png'

    generate_call = nodeproxy.generate_calls[0]
    assert generate_call['endpoint'] == '/v1/images/edits'
    assert generate_call['request_payload'] is None
    assert isinstance(generate_call['request_content'], bytes)
    assert b'name="prompt"' in generate_call['request_content']
    assert b'edit the cat' in generate_call['request_content']
    assert b'filename="source.png"' in generate_call['request_content']
    assert b'filename="mask.png"' in generate_call['request_content']
    assert generate_call['extra_headers']['Content-Type'].startswith('multipart/form-data; boundary=')

    request_log_payload = orjson.loads(nodeproxy.pre_call_calls[0]['request_data'])
    assert request_log_payload['fields']['custom_provider_option'] == 'keep-me'
    assert request_log_payload['files'][0]['field'] == 'image'
    assert request_log_payload['files'][1]['field'] == 'mask'
    assert nodeproxy.pre_call_calls[0]['request_action'] == RequestAction.images_edits


@pytest.mark.asyncio
async def test_images_variations_proxies_multipart_request(runtime_client_factory):
    """Image variation route should passthrough multipart bodies and log variation actions."""
    nodeproxy = DummyImageNodeProxyService()
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['gpt-image-1'],
    )

    try:
        response = await client.post(
            '/v1/images/variations',
            data={
                'model': 'gpt-image-1',
                'n': '2',
            },
            files=[
                ('image', ('variation.png', b'image-bytes', 'image/png')),
            ],
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['data'][0]['url'] == 'https://example.com/generated.png'

    generate_call = nodeproxy.generate_calls[0]
    assert generate_call['endpoint'] == '/v1/images/variations'
    assert generate_call['request_payload'] is None
    assert isinstance(generate_call['request_content'], bytes)
    assert b'filename="variation.png"' in generate_call['request_content']
    assert generate_call['extra_headers']['Content-Type'].startswith('multipart/form-data; boundary=')

    request_log_payload = orjson.loads(nodeproxy.pre_call_calls[0]['request_data'])
    assert request_log_payload['fields']['n'] == '2'
    assert request_log_payload['files'][0]['field'] == 'image'
    assert nodeproxy.pre_call_calls[0]['request_action'] == RequestAction.images_variations