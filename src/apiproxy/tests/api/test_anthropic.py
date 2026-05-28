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


class DummyAnthropicNodeProxyService:
    """Lightweight proxy stub for Anthropic runtime compatibility tests."""

    def __init__(self) -> None:
        self.status = {
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
    ) -> Optional[str]:
        self.get_node_url_calls.append(
            {
                'model_name': model_name,
                'model_type': model_type,
                'request_protocol': request_protocol,
                'allow_cross_protocol': allow_cross_protocol,
            }
        )
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
        return orjson.dumps(
            {
                'id': 'chatcmpl-1',
                'object': 'chat.completion',
                'model': request_payload.get('model'),
                'choices': [
                    {
                        'index': 0,
                        'message': {'role': 'assistant', 'content': 'done'},
                        'finish_reason': 'stop',
                    }
                ],
                'usage': {'prompt_tokens': 12, 'completion_tokens': 4, 'total_tokens': 16},
            }
        ).decode('utf-8')

    def post_call(self, node_url: str, request_ctx) -> None:
        self.post_call_calls.append({'node_url': node_url, 'request_ctx': request_ctx})


@pytest.fixture
def runtime_client_factory():
    """Create a runtime client with an overridable Anthropc proxy service."""
    from openaiproxy.main import setup_app

    async def _build_client(*, nodeproxy: DummyAnthropicNodeProxyService, effective_allowed_models: Optional[list[str]] = None):
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