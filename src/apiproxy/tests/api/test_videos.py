from __future__ import annotations

from datetime import datetime, timedelta
from http import HTTPStatus
from types import SimpleNamespace
from typing import Optional
from uuid import UUID, uuid4

import orjson
import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import delete

from openaiproxy.api.schemas import VideoGenerationRequest
from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.services.database.models.node.model import ModelType, ProtocolType
from openaiproxy.services.database.models.proxy.model import (
    RequestAction,
    VideoGenerationTask,
    VideoTaskStatus,
)
from openaiproxy.services.deps import async_session_scope, get_db_service, get_node_proxy_service
from openaiproxy.services.nodeproxy.exceptions import NodeModelQuotaExceeded
from openaiproxy.services.nodeproxy.service import create_error_response
from openaiproxy.utils.timezone import current_timezone


@pytest.fixture(autouse=True)
async def cleanup_video_tasks() -> None:
    """Reset persisted video task rows around each test."""

    async with async_session_scope() as cleanup_session:
        await cleanup_session.exec(delete(VideoGenerationTask))
    yield
    async with async_session_scope() as cleanup_session:
        await cleanup_session.exec(delete(VideoGenerationTask))


class DummyVideoNodeProxyService:
    """Lightweight video proxy stub used by runtime route tests."""

    def __init__(
        self,
        *,
        check_response=None,
        get_node_url_result: Optional[str] = 'http://node.example.com',
        get_node_url_exception: Optional[BaseException] = None,
        pre_call_exception: Optional[BaseException] = None,
        backend_payload: Optional[dict] = None,
        retrieve_payload: Optional[dict] = None,
        cancel_payload: Optional[dict] = None,
        binary_payload: Optional[bytes] = None,
    ) -> None:
        self.check_response = check_response
        self.get_node_url_result = get_node_url_result
        self.get_node_url_exception = get_node_url_exception
        self.pre_call_exception = pre_call_exception
        self.backend_payload = backend_payload or {
            'id': 'video_123',
            'status': 'succeeded',
            'url': 'https://example.com/generated.mp4',
        }
        self.retrieve_payload = retrieve_payload or {
            'id': 'video_123',
            'status': 'processing',
            'model': 'gpt-video-1',
        }
        self.cancel_payload = cancel_payload or {
            'id': 'video_123',
            'status': 'canceled',
            'model': 'gpt-video-1',
        }
        self.binary_payload = binary_payload or b'video-binary-content'
        self.status = {
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

    def is_model_allowed(self, model_name: str, effective_allowed_models: Optional[list[str]] = None) -> bool:
        if effective_allowed_models is None:
            return True
        return model_name in effective_allowed_models

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
        if self.get_node_url_exception is not None:
            raise self.get_node_url_exception
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
        request,
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
                'request_payload': request,
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
        if endpoint.endswith('/content'):
            return self.binary_payload if response_mode == 'bytes' else self.binary_payload.decode('utf-8')
        if endpoint.endswith('/cancel'):
            return orjson.dumps(self.cancel_payload).decode('utf-8')
        if endpoint.startswith('/v1/videos/') and method == 'GET':
            return orjson.dumps(self.retrieve_payload).decode('utf-8')
        return orjson.dumps(self.backend_payload).decode('utf-8')

    def post_call(self, node_url: str, request_ctx) -> None:
        self.post_call_calls.append(
            {
                'node_url': node_url,
                'request_ctx': request_ctx,
            }
        )


@pytest.fixture
def runtime_client_factory():
    """Create a runtime client with an overridable video proxy service."""
    from openaiproxy.main import setup_app

    async def _build_client(*, nodeproxy: DummyVideoNodeProxyService, effective_allowed_models: Optional[list[str]] = None):
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


def test_video_generation_request_preserves_extra_fields():
    """Video generation schema should preserve provider-specific passthrough fields."""
    request = VideoGenerationRequest(
        model='gpt-video-1',
        prompt='animate a sunset over the ocean',
        duration='5s',
        custom_provider_option='keep-me',
    )

    payload = request.model_dump(exclude_none=True)

    assert payload['custom_provider_option'] == 'keep-me'
    assert payload['duration'] == '5s'


@pytest.mark.asyncio
async def test_videos_generations_proxies_openai_request(runtime_client_factory):
    """Video generation route should forward payloads and record video-specific actions."""
    nodeproxy = DummyVideoNodeProxyService()
    client, app = await runtime_client_factory(
        nodeproxy=nodeproxy,
        effective_allowed_models=['gpt-video-1'],
    )

    try:
        response = await client.post(
            '/v1/videos/generations',
            json={
                'model': 'gpt-video-1',
                'prompt': 'animate a sunset over the ocean',
                'duration': '5s',
                'custom_provider_option': 'keep-me',
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['status'] == 'succeeded'
    assert response.json()['url'] == 'https://example.com/generated.mp4'

    assert nodeproxy.check_request_calls[0]['model_type'] == ModelType.video_generation.value
    assert nodeproxy.get_node_url_calls[0]['model_type'] == ModelType.video_generation.value
    assert nodeproxy.generate_calls[0]['endpoint'] == '/v1/videos/generations'
    assert nodeproxy.generate_calls[0]['request_payload']['custom_provider_option'] == 'keep-me'
    assert nodeproxy.pre_call_calls[0]['request_action'] == RequestAction.videos_generations
    assert nodeproxy.pre_call_calls[0]['request_count'] == 0
    assert nodeproxy.pre_call_calls[0]['estimated_total_tokens'] is None
    assert nodeproxy.post_call_calls[0]['request_ctx'].response_data is not None


@pytest.mark.asyncio
async def test_videos_generations_returns_quota_error_when_node_model_exhausted(runtime_client_factory):
    """Video generation route should surface node model quota exhaustion consistently."""
    nodeproxy = DummyVideoNodeProxyService(
        get_node_url_exception=NodeModelQuotaExceeded(
            '节点模型配额已耗尽',
            detail='gpt-video-1 (video-generation)',
        )
    )
    client, app = await runtime_client_factory(nodeproxy=nodeproxy)

    try:
        response = await client.post(
            '/v1/videos/generations',
            json={
                'model': 'gpt-video-1',
                'prompt': 'animate a sunset over the ocean',
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()

    assert response.status_code == 429
    payload = response.json()
    assert payload['type'] == 'quota_exceeded'
    assert 'gpt-video-1' in payload['message']


@pytest.mark.asyncio
async def test_videos_followup_routes_proxy_retrieve_cancel_and_content(runtime_client_factory):
    """Video follow-up routes should reuse tracked task metadata for retrieve/cancel/content."""
    create_nodeproxy = DummyVideoNodeProxyService()
    create_client, create_app = await runtime_client_factory(
        nodeproxy=create_nodeproxy,
        effective_allowed_models=['gpt-video-1'],
    )

    try:
        create_response = await create_client.post(
            '/v1/videos/generations',
            json={
                'model': 'gpt-video-1',
                'prompt': 'animate a sunset over the ocean',
            },
        )
    finally:
        await create_client.aclose()
        create_app.dependency_overrides.clear()

    followup_nodeproxy = DummyVideoNodeProxyService()
    followup_client, followup_app = await runtime_client_factory(
        nodeproxy=followup_nodeproxy,
        effective_allowed_models=['gpt-video-1'],
    )

    try:
        retrieve_response = await followup_client.get('/v1/videos/video_123')
        cancel_response = await followup_client.post('/v1/videos/video_123/cancel')
        content_response = await followup_client.get('/v1/videos/video_123/content')
    finally:
        await followup_client.aclose()
        followup_app.dependency_overrides.clear()

    assert create_response.status_code == 200
    assert retrieve_response.status_code == 200
    assert retrieve_response.json()['status'] == 'processing'
    assert cancel_response.status_code == 200
    assert cancel_response.json()['status'] == 'canceled'
    assert content_response.status_code == 200
    assert content_response.content == b'video-binary-content'
    assert content_response.headers['content-type'].startswith('video/mp4')
    assert content_response.headers['content-disposition'] == 'attachment; filename="video_123.mp4"'

    assert create_nodeproxy.generate_calls[0]['endpoint'] == '/v1/videos/generations'

    assert followup_nodeproxy.generate_calls[0]['endpoint'] == '/v1/videos/video_123'
    assert followup_nodeproxy.generate_calls[0]['method'] == 'GET'
    assert followup_nodeproxy.pre_call_calls[0]['request_action'] == RequestAction.videos_retrieve

    assert followup_nodeproxy.generate_calls[1]['endpoint'] == '/v1/videos/video_123/cancel'
    assert followup_nodeproxy.generate_calls[1]['method'] == 'POST'
    assert followup_nodeproxy.pre_call_calls[1]['request_action'] == RequestAction.videos_cancel

    assert followup_nodeproxy.generate_calls[2]['endpoint'] == '/v1/videos/video_123/content'
    assert followup_nodeproxy.generate_calls[2]['method'] == 'GET'
    assert followup_nodeproxy.generate_calls[2]['response_mode'] == 'bytes'
    assert followup_nodeproxy.pre_call_calls[2]['request_action'] == RequestAction.videos_content


@pytest.mark.asyncio
async def test_recover_video_generation_tasks_refreshes_processing_rows(session):
    """Recovery task should refresh persisted non-terminal video tasks from backend state."""
    from openaiproxy.api.v1.videos import recover_video_generation_tasks

    task_entry = VideoGenerationTask(
        video_id=f'video_recover_{uuid4().hex}',
        node_url='http://node.example.com',
        backend_api_key='backend-token',
        protocol_type=ProtocolType.openai,
        model_name='gpt-video-1',
        status=VideoTaskStatus.processing,
    )
    session.add(task_entry)
    await session.commit()

    recovery_nodeproxy = DummyVideoNodeProxyService(
        retrieve_payload={
            'id': task_entry.video_id,
            'status': 'succeeded',
            'url': 'https://example.com/final.mp4',
        }
    )
    refreshed_count, failed_count = await recover_video_generation_tasks(
        nodeproxy_service=recovery_nodeproxy,
    )

    assert refreshed_count == 1
    assert failed_count == 0

    db_service = get_db_service()
    async with db_service.with_async_session() as verify_session:
        refreshed_entry = await verify_session.get(VideoGenerationTask, task_entry.id)

    assert refreshed_entry is not None
    assert refreshed_entry.status == VideoTaskStatus.succeeded
    assert refreshed_entry.completed_at is not None
    assert refreshed_entry.latest_response_payload['url'] == 'https://example.com/final.mp4'


@pytest.mark.asyncio
async def test_recover_video_generation_tasks_marks_dispatching_rows_failed(session):
    """Recovery task should mark unrecoverable dispatching rows as failed."""
    from openaiproxy.api.v1.videos import recover_video_generation_tasks

    task_entry = VideoGenerationTask(
        node_url='http://node.example.com',
        protocol_type=ProtocolType.openai,
        model_name='gpt-video-1',
        status=VideoTaskStatus.dispatching,
    )
    session.add(task_entry)
    await session.commit()

    refreshed_count, failed_count = await recover_video_generation_tasks(
        nodeproxy_service=DummyVideoNodeProxyService(),
    )

    assert refreshed_count == 0
    assert failed_count == 1

    db_service = get_db_service()
    async with db_service.with_async_session() as verify_session:
        refreshed_entry = await verify_session.get(VideoGenerationTask, task_entry.id)

    assert refreshed_entry is not None
    assert refreshed_entry.status == VideoTaskStatus.failed
    assert 'video_id' in refreshed_entry.error_message


@pytest.mark.asyncio
async def test_cleanup_video_generation_tasks_removes_old_terminal_rows(session):
    """Cleanup task should remove expired terminal video tasks while keeping active ones."""
    from openaiproxy.api.v1.videos import cleanup_video_generation_tasks

    now = datetime.now(tz=current_timezone())
    old_task = VideoGenerationTask(
        video_id=f'video_old_{uuid4().hex}',
        node_url='http://node.example.com',
        protocol_type=ProtocolType.openai,
        model_name='gpt-video-1',
        status=VideoTaskStatus.failed,
        created_at=now - timedelta(days=120),
        updated_at=now - timedelta(days=120),
        completed_at=now - timedelta(days=120),
        error_message='expired failure',
    )
    active_task = VideoGenerationTask(
        video_id=f'video_active_{uuid4().hex}',
        node_url='http://node.example.com',
        protocol_type=ProtocolType.openai,
        model_name='gpt-video-1',
        status=VideoTaskStatus.processing,
        created_at=now,
        updated_at=now,
    )
    session.add(old_task)
    session.add(active_task)
    await session.commit()

    deleted_count = await cleanup_video_generation_tasks()

    assert deleted_count == 1

    db_service = get_db_service()
    async with db_service.with_async_session() as verify_session:
        deleted_entry = await verify_session.get(VideoGenerationTask, old_task.id)
        active_entry = await verify_session.get(VideoGenerationTask, active_task.id)

    assert deleted_entry is None
    assert active_entry is not None