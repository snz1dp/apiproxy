from __future__ import annotations

from http import HTTPStatus
from types import SimpleNamespace
from typing import Optional
from uuid import UUID

import orjson
import pytest
from httpx import ASGITransport, AsyncClient

from openaiproxy.api.schemas import AudioSpeechRequest
from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.services.database.models.node.model import ModelType, ProtocolType
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.services.deps import get_node_proxy_service
from openaiproxy.services.nodeproxy.exceptions import NodeModelQuotaExceeded
from openaiproxy.services.nodeproxy.service import create_error_response


class DummyAudioNodeProxyService:
    """Lightweight audio proxy stub used by runtime route tests."""

    def __init__(
        self,
        *,
        check_response=None,
        get_node_url_result: Optional[str] = 'http://node.example.com',
        get_node_url_results: Optional[list[Optional[str]]] = None,
        get_node_url_exception: Optional[BaseException] = None,
        pre_call_exception: Optional[BaseException] = None,
        speech_binary_payload: Optional[bytes] = None,
        transcript_payload: Optional[dict] = None,
        translation_text_payload: Optional[bytes] = None,
        response_payloads: Optional[list[bytes | dict]] = None,
        status: Optional[dict[str, SimpleNamespace]] = None,
    ) -> None:
        self.check_response = check_response
        self.get_node_url_result = get_node_url_result
        self.get_node_url_results = list(get_node_url_results or [])
        self.get_node_url_exception = get_node_url_exception
        self.pre_call_exception = pre_call_exception
        self.speech_binary_payload = speech_binary_payload or b'ID3-audio-binary'
        self.transcript_payload = transcript_payload or {
            'text': 'hello world',
        }
        self.translation_text_payload = translation_text_payload or b'WEBVTT\n\n00:00.000 --> 00:01.500\nhello world\n'
        self.response_payloads = list(response_payloads or [])
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
        method: str = 'POST',
        response_mode: str = 'text',
    ):
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
                'method': method,
                'response_mode': response_mode,
            }
        )
        if self.response_payloads:
            payload = self.response_payloads.pop(0) if len(self.response_payloads) > 1 else self.response_payloads[0]
            if isinstance(payload, dict):
                return orjson.dumps(payload)
            return payload
        if endpoint == '/v1/audio/speech':
            return self.speech_binary_payload
        if endpoint == '/v1/audio/translations':
            return self.translation_text_payload
        return orjson.dumps(self.transcript_payload)

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
    """Create a runtime client with an overridable audio proxy service."""
    from openaiproxy.main import setup_app

    async def _build_client(*, nodeproxy: DummyAudioNodeProxyService, effective_allowed_models: Optional[list[str]] = None):
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


def test_audio_speech_request_preserves_extra_fields() -> None:
    """Audio speech schema should preserve provider-specific passthrough fields."""
    request = AudioSpeechRequest(
        model='gpt-4o-mini-tts',
        input='hello world',
        voice='alloy',
        response_format='wav',
        custom_provider_option='keep-me',
    )

    payload = request.model_dump(exclude_none=True)

    assert payload['custom_provider_option'] == 'keep-me'
    assert payload['response_format'] == 'wav'


@pytest.mark.asyncio
async def test_audio_speech_proxies_request_and_returns_binary(runtime_client_factory):
    """Audio speech route should forward JSON payloads and return audio bytes."""
    nodeproxy = DummyAudioNodeProxyService()
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['gpt-4o-mini-tts'],
    )

    try:
        response = await client.post(
            '/v1/audio/speech',
            json={
                'model': 'gpt-4o-mini-tts',
                'input': 'hello world',
                'voice': 'alloy',
                'response_format': 'mp3',
                'custom_provider_option': 'keep-me',
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.content == b'ID3-audio-binary'
    assert response.headers['content-type'].startswith('audio/mpeg')
    assert response.headers['content-disposition'] == 'attachment; filename="speech.mp3"'

    assert nodeproxy.check_request_calls[0]['model_type'] == ModelType.text_to_speech.value
    assert nodeproxy.get_node_url_calls[0]['model_type'] == ModelType.text_to_speech.value
    assert nodeproxy.generate_calls[0]['endpoint'] == '/v1/audio/speech'
    assert nodeproxy.generate_calls[0]['request_payload']['custom_provider_option'] == 'keep-me'
    assert nodeproxy.generate_calls[0]['response_mode'] == 'bytes'
    assert nodeproxy.pre_call_calls[0]['request_action'] == RequestAction.audio_speech
    assert nodeproxy.post_call_calls[0]['request_ctx'].response_data == '<binary 16 bytes>'


@pytest.mark.asyncio
async def test_audio_transcriptions_proxy_multipart_request(runtime_client_factory):
    """Audio transcription route should passthrough multipart bodies and log transcription actions."""
    nodeproxy = DummyAudioNodeProxyService()
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['whisper-1'],
    )

    try:
        response = await client.post(
            '/v1/audio/transcriptions',
            files={
                'file': ('sample.wav', b'audio-bytes', 'audio/wav'),
            },
            data={
                'model': 'whisper-1',
                'prompt': 'transcribe this',
                'response_format': 'json',
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['text'] == 'hello world'

    assert nodeproxy.check_request_calls[0]['model_type'] == ModelType.speech_to_text.value
    assert nodeproxy.get_node_url_calls[0]['model_type'] == ModelType.speech_to_text.value
    assert nodeproxy.generate_calls[0]['endpoint'] == '/v1/audio/transcriptions'
    assert nodeproxy.generate_calls[0]['request_content'] is not None
    assert nodeproxy.generate_calls[0]['extra_headers']['Content-Type'].startswith('multipart/form-data; boundary=')
    assert nodeproxy.pre_call_calls[0]['request_action'] == RequestAction.audio_transcriptions
    assert nodeproxy.post_call_calls[0]['request_ctx'].response_data is not None


@pytest.mark.asyncio
async def test_audio_translations_return_text_response_when_requested(runtime_client_factory):
    """Audio translation route should passthrough multipart requests and support text outputs."""
    nodeproxy = DummyAudioNodeProxyService()
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['whisper-1'],
    )

    try:
        response = await client.post(
            '/v1/audio/translations',
            files={
                'file': ('sample.wav', b'audio-bytes', 'audio/wav'),
            },
            data={
                'model': 'whisper-1',
                'response_format': 'vtt',
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.text.startswith('WEBVTT')
    assert response.headers['content-type'].startswith('text/vtt')

    assert nodeproxy.check_request_calls[0]['model_type'] == ModelType.speech_to_text.value
    assert nodeproxy.generate_calls[0]['endpoint'] == '/v1/audio/translations'
    assert nodeproxy.generate_calls[0]['request_content'] is not None
    assert nodeproxy.pre_call_calls[0]['request_action'] == RequestAction.audio_translations
    assert nodeproxy.post_call_calls[0]['request_ctx'].response_data.startswith('WEBVTT')


@pytest.mark.asyncio
async def test_audio_speech_returns_quota_error_when_node_model_exhausted(runtime_client_factory):
    """Audio speech route should surface node model quota exhaustion consistently."""
    nodeproxy = DummyAudioNodeProxyService(
        get_node_url_exception=NodeModelQuotaExceeded(
            '节点模型配额已耗尽',
            detail='gpt-4o-mini-tts (text-to-speech)',
        )
    )
    client, app = await runtime_client_factory(nodeproxy=nodeproxy)

    try:
        response = await client.post(
            '/v1/audio/speech',
            json={
                'model': 'gpt-4o-mini-tts',
                'input': 'hello world',
                'voice': 'alloy',
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 429
    payload = response.json()
    assert payload['type'] == 'quota_exceeded'
    assert 'gpt-4o-mini-tts' in payload['message']


@pytest.mark.asyncio
async def test_audio_speech_retries_next_node_when_backend_capacity_is_exhausted(runtime_client_factory):
    """Audio speech route should fail over to the next node on backend quota exhaustion."""
    exhausted_payload = {
        'error': {
            'message': 'You exceeded your current quota.',
            'type': 'invalid_request_error',
            'code': 'insufficient_quota',
        }
    }
    nodeproxy = DummyAudioNodeProxyService(
        get_node_url_results=[
            'http://node-a.example.com',
            'http://node-b.example.com',
        ],
        response_payloads=[
            exhausted_payload,
            b'ID3-retry-audio',
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
        effective_allowed_models=['gpt-4o-mini-tts'],
    )

    try:
        response = await client.post(
            '/v1/audio/speech',
            json={
                'model': 'gpt-4o-mini-tts',
                'input': 'hello world',
                'voice': 'alloy',
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.content == b'ID3-retry-audio'
    assert len(nodeproxy.generate_calls) == 2
    assert nodeproxy.generate_calls[0]['node_url'] == 'http://node-a.example.com'
    assert nodeproxy.generate_calls[1]['node_url'] == 'http://node-b.example.com'
    assert nodeproxy.get_node_url_calls[1]['exclude_node_urls'] == {'http://node-a.example.com'}
    assert len(nodeproxy.cleanup_calls) == 1
    assert nodeproxy.cleanup_calls[0]['node_url'] == 'http://node-a.example.com'
    assert len(nodeproxy.post_call_calls) == 1
    assert nodeproxy.post_call_calls[0]['node_url'] == 'http://node-b.example.com'