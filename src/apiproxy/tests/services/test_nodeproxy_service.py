import asyncio
from contextlib import asynccontextmanager
import time
import threading
from datetime import datetime
from uuid import uuid4

import httpx
import orjson
import pytest
import requests

from openaiproxy.services.database.models.node.model import ModelType, Node, NodeModel, ProtocolType
from openaiproxy.services.nodeproxy.constants import ErrorCodes
from openaiproxy.services.nodeproxy.exceptions import NorthboundQuotaProcessingError
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.services.nodeproxy.schemas import Status
from openaiproxy.services.nodeproxy import service as nodeproxy_service_module
from openaiproxy.services.nodeproxy.service import NodeProxyService, _RequestContext
from openaiproxy.utils.timezone import current_timezone


class _FakeStreamingResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def iter_lines(self, decode_unicode=False, delimiter=b'\n'):
        del decode_unicode, delimiter
        yield b'data: {"ok": true}'
        yield b'data: {"ok": false}'


class _TimeoutAsyncClient:
    def __init__(self, *args, **kwargs):
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def post(self, *args, **kwargs):
        del args, kwargs
        raise httpx.TimeoutException('timeout')


class _CancelledAsyncClient:
    def __init__(self, *args, **kwargs):
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def post(self, *args, **kwargs):
        del args, kwargs
        raise asyncio.CancelledError()


class _HttpErrorAsyncClient:
    def __init__(self, *args, **kwargs):
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def post(self, *args, **kwargs):
        del args, kwargs
        raise httpx.ConnectError('connect failed')


class _CaptureAsyncClient:
    last_url: str | None = None

    def __init__(self, *args, **kwargs):
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def post(self, url, *args, **kwargs):
        del args, kwargs
        _CaptureAsyncClient.last_url = url

        class _Response:
            text = '{"ok": true}'

        return _Response()


def _build_service() -> NodeProxyService:
    return object.__new__(NodeProxyService)


def _build_refresh_service() -> NodeProxyService:
    service = _build_service()
    service._lock = threading.Lock()
    service.nodes = {}
    service.snode = {}
    service._offline_nodes = {}
    service._node_metadata = {}
    service._node_model_quota_state = {}
    service._quota_exhausted_models = {}
    service._quota_exhaustion_ttl = 300
    service.proxy_instance_id = None
    service._set_config = lambda node_url, status: service.nodes.__setitem__(node_url, status)
    service._delete_config = lambda node_url: service.nodes.pop(node_url, None)
    service._clear_node_model_quota_mark = lambda node_url, model_name, model_type: service._node_model_quota_state.pop((node_url, model_name, model_type), None)
    return service


class _FakeAsyncSession:
    def __init__(self):
        self.commit_calls = 0
        self.rollback_calls = 0

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1


def test_stream_generate_close_does_not_raise_runtime_error(monkeypatch):
    service = _build_service()

    def fake_post(*args, **kwargs):
        del args, kwargs
        return _FakeStreamingResponse()

    monkeypatch.setattr(requests, 'post', fake_post)

    generator = service.stream_generate(
        request={'stream': True},
        node_url='http://node.example.com',
        endpoint='/v1/chat/completions',
    )

    first_chunk = next(generator)
    assert first_chunk == b'data: {"ok": true}\n\n'

    generator.close()


def test_build_backend_request_url_avoids_duplicate_v1_prefix():
    assert NodeProxyService._build_backend_request_url(
        'http://node.example.com/v1',
        '/v1/chat/completions',
    ) == 'http://node.example.com/v1/chat/completions'
    assert NodeProxyService._build_backend_request_url(
        'http://node.example.com',
        '/v1/chat/completions',
    ) == 'http://node.example.com/v1/chat/completions'
    assert NodeProxyService._build_models_url(
        'http://node.example.com/v1',
    ) == 'http://node.example.com/v1/models'


def test_stream_generate_uses_single_v1_prefix(monkeypatch):
    service = _build_service()
    captured: dict[str, str] = {}

    def fake_post(url, *args, **kwargs):
        del args, kwargs
        captured['url'] = url
        return _FakeStreamingResponse()

    monkeypatch.setattr(requests, 'post', fake_post)

    generator = service.stream_generate(
        request={'stream': True},
        node_url='http://node.example.com/v1',
        endpoint='/v1/chat/completions',
    )

    first_chunk = next(generator)
    assert first_chunk == b'data: {"ok": true}\n\n'
    assert captured['url'] == 'http://node.example.com/v1/chat/completions'
    generator.close()


def test_stream_generate_timeout_returns_timeout_payload(monkeypatch):
    service = _build_service()

    def fake_post(*args, **kwargs):
        del args, kwargs
        raise requests.Timeout('timeout')

    monkeypatch.setattr(requests, 'post', fake_post)

    chunks = list(
        service.stream_generate(
            request={'stream': True},
            node_url='http://node.example.com',
            endpoint='/v1/chat/completions',
        )
    )

    assert len(chunks) == 1
    payload = orjson.loads(chunks[0].strip())
    assert payload['error_code'] == ErrorCodes.API_TIMEOUT.value


def test_stream_generate_request_failure_returns_service_unavailable(monkeypatch):
    service = _build_service()

    def fake_post(*args, **kwargs):
        del args, kwargs
        raise requests.ConnectionError('connection failed')

    monkeypatch.setattr(requests, 'post', fake_post)

    chunks = list(
        service.stream_generate(
            request={'stream': True},
            node_url='http://node.example.com',
            endpoint='/v1/chat/completions',
        )
    )

    assert len(chunks) == 1
    payload = orjson.loads(chunks[0].strip())
    assert payload['error_code'] == ErrorCodes.SERVICE_UNAVAILABLE.value


@pytest.mark.asyncio
async def test_generate_timeout_returns_timeout_payload(monkeypatch):
    service = _build_service()

    monkeypatch.setattr(httpx, 'AsyncClient', _TimeoutAsyncClient)

    payload = await service.generate(
        request={'stream': False},
        node_url='http://node.example.com',
        endpoint='/v1/chat/completions',
    )

    body = orjson.loads(payload.strip())
    assert body['error_code'] == ErrorCodes.API_TIMEOUT.value


@pytest.mark.asyncio
async def test_generate_request_failure_returns_service_unavailable(monkeypatch):
    service = _build_service()

    monkeypatch.setattr(httpx, 'AsyncClient', _HttpErrorAsyncClient)

    payload = await service.generate(
        request={'stream': False},
        node_url='http://node.example.com',
        endpoint='/v1/chat/completions',
    )

    body = orjson.loads(payload.strip())
    assert body['error_code'] == ErrorCodes.SERVICE_UNAVAILABLE.value


@pytest.mark.asyncio
async def test_generate_cancellation_is_reraised(monkeypatch):
    service = _build_service()

    monkeypatch.setattr(httpx, 'AsyncClient', _CancelledAsyncClient)

    with pytest.raises(asyncio.CancelledError):
        await service.generate(
            request={'stream': False},
            node_url='http://node.example.com',
            endpoint='/v1/chat/completions',
        )


@pytest.mark.asyncio
async def test_generate_uses_single_v1_prefix(monkeypatch):
    service = _build_service()
    _CaptureAsyncClient.last_url = None

    monkeypatch.setattr(httpx, 'AsyncClient', _CaptureAsyncClient)

    payload = await service.generate(
        request={'stream': False},
        node_url='http://node.example.com/v1',
        endpoint='/v1/chat/completions',
    )

    assert payload == '{"ok": true}'
    assert _CaptureAsyncClient.last_url == 'http://node.example.com/v1/chat/completions'


def test_pre_call_propagates_northbound_quota_processing_error(monkeypatch):
    service = _build_service()

    monkeypatch.setattr(service, '_normalize_model_type', lambda model_type: model_type)
    monkeypatch.setattr(
        service,
        '_reserve_northbound_quota',
        lambda **kwargs: (_ for _ in ()).throw(NorthboundQuotaProcessingError('quota reserve failed')),
    )

    with pytest.raises(NorthboundQuotaProcessingError):
        service.pre_call(
            'http://node.example.com',
            RequestAction.completions,
            model_name='gpt-4',
            ownerapp_id='app-test',
            request_count=10,
            estimated_total_tokens=20,
            api_key_id='00000000-0000-0000-0000-000000000001',
        )


def test_post_call_updates_log_when_northbound_quota_finalize_fails(monkeypatch):
    service = _build_service()
    context = _RequestContext(
        start_time=0.0,
        ownerapp_id='app-test',
        request_action=RequestAction.completions,
        request_tokens=10,
        response_tokens=5,
        total_tokens=15,
    )

    finalize_calls: list[tuple[str, bool, str | None]] = []

    monkeypatch.setattr(service, '_finalize_request_log', lambda node_url, ctx, elapsed: finalize_calls.append((node_url, ctx.error, ctx.error_message)))
    monkeypatch.setattr(service, '_apply_node_model_quota', lambda node_url, ctx: None)
    monkeypatch.setattr(service, '_apply_northbound_quota', lambda ctx: (_ for _ in ()).throw(NorthboundQuotaProcessingError('quota finalize failed')))
    monkeypatch.setattr(service, '_refresh_node_metrics', lambda node_url: None)
    monkeypatch.setattr(time, 'time', lambda: 1.0)

    service.post_call('http://node.example.com', context)

    assert len(finalize_calls) == 2
    assert finalize_calls[0][1] is False
    assert finalize_calls[1][1] is True
    assert finalize_calls[1][2] == 'quota finalize failed'


def test_resolve_node_availability_keeps_trusted_node_available():
    assert NodeProxyService._resolve_node_availability(
        enabled_flag=True,
        persisted_available=False,
        trusted_without_models_endpoint=True,
    ) is True


def test_perform_node_health_checks_skips_trusted_nodes(monkeypatch):
    service = _build_service()
    service._lock = threading.Lock()
    service.snode = {
        'http://trusted-node.example.com': Status(
            models=['gpt-4'],
            avaiaible=True,
            trusted_without_models_endpoint=True,
        ),
        'http://normal-node.example.com': Status(
            models=['gpt-4'],
            avaiaible=True,
            trusted_without_models_endpoint=False,
        ),
    }

    checked_nodes: list[str] = []
    monkeypatch.setattr(
        service,
        '_check_single_node',
        lambda node_url, api_key, protocol_type=None, request_proxy_url=None: checked_nodes.append(node_url),
    )

    service.perform_node_health_checks()

    assert checked_nodes == ['http://normal-node.example.com']


@pytest.mark.asyncio
async def test_refresh_nodes_from_database_loads_configured_nodes_and_models(monkeypatch):
    service = _build_refresh_service()
    now = datetime.now(current_timezone())
    node_id = uuid4()
    model_id = uuid4()
    db_node = Node(
        id=node_id,
        url='http://configured-node.example.com',
        name='configured-node',
        enabled=True,
        health_check=False,
        trusted_without_models_endpoint=False,
        updated_at=now,
    )
    db_model = NodeModel(
        id=model_id,
        node_id=node_id,
        model_name='gpt-4o-mini',
        model_type=ModelType.chat,
        enabled=True,
    )

    @asynccontextmanager
    async def fake_async_session_scope():
        yield object()

    async def fake_select_nodes(*, enabled, expired, session):
        del enabled, expired, session
        return [db_node]

    async def fake_select_node_models(*, node_ids, session):
        del session
        assert node_ids == [node_id]
        return [db_model]

    async def fake_select_node_model_quotas(*, node_model_ids, session):
        del node_model_ids, session
        return []

    async def fake_select_proxy_node_status(*, proxy_instance_ids, node_ids, session):
        del proxy_instance_ids, node_ids, session
        return []

    async def fake_fetch_proxy_node_metrics(*, session, node_id, proxy_id, history_limit):
        del session, node_id, proxy_id, history_limit
        return 0, None, None, []

    monkeypatch.setattr(nodeproxy_service_module, 'async_session_scope', fake_async_session_scope)
    monkeypatch.setattr(nodeproxy_service_module, 'select_nodes', fake_select_nodes)
    monkeypatch.setattr(nodeproxy_service_module, 'select_node_models', fake_select_node_models)
    monkeypatch.setattr(nodeproxy_service_module, 'select_node_model_quotas', fake_select_node_model_quotas)
    monkeypatch.setattr(nodeproxy_service_module, 'select_proxy_node_status', fake_select_proxy_node_status)
    monkeypatch.setattr(nodeproxy_service_module, 'fetch_proxy_node_metrics', fake_fetch_proxy_node_metrics)

    await service._refresh_nodes_from_database(initial_load=True)

    assert list(service.snode.keys()) == ['http://configured-node.example.com']
    status = service.snode['http://configured-node.example.com']
    assert isinstance(status, Status)
    assert status.models == ['gpt-4o-mini']
    assert status.health_check is False
    assert status.types == ['chat']


@pytest.mark.asyncio
async def test_persist_health_check_result_skips_when_status_upsert_returns_none(monkeypatch):
    service = _build_service()
    service.proxy_instance_id = uuid4()

    @asynccontextmanager
    async def fake_async_session_scope():
        yield object()

    async def fake_upsert_proxy_node_status(**kwargs):
        del kwargs
        return None

    async def fake_create_proxy_node_status_log_entry(**kwargs):
        raise AssertionError('status row missing 时不应继续写节点日志')

    monkeypatch.setattr(nodeproxy_service_module, 'async_session_scope', fake_async_session_scope)
    monkeypatch.setattr(nodeproxy_service_module, 'upsert_proxy_node_status', fake_upsert_proxy_node_status)
    monkeypatch.setattr(nodeproxy_service_module, 'create_proxy_node_status_log_entry', fake_create_proxy_node_status_log_entry)

    result = await NodeProxyService._persist_health_check_result_async(
        service,
        node_id=uuid4(),
        status_id=None,
        available=False,
        latency=0.5,
        started_at=0.0,
        previous_available=True,
        error_message='node missing',
    )

    assert result is None


@pytest.mark.asyncio
async def test_persist_health_check_result_logs_with_openai_request_protocol(monkeypatch):
    service = _build_service()
    service.proxy_instance_id = uuid4()
    status_id = uuid4()
    captured_kwargs: dict[str, object] = {}

    @asynccontextmanager
    async def fake_async_session_scope():
        yield object()

    class _StatusRow:
        id = status_id

    async def fake_upsert_proxy_node_status(**kwargs):
        del kwargs
        return _StatusRow()

    async def fake_create_proxy_node_status_log_entry(**kwargs):
        captured_kwargs.update(kwargs)
        return None

    monkeypatch.setattr(nodeproxy_service_module, 'async_session_scope', fake_async_session_scope)
    monkeypatch.setattr(nodeproxy_service_module, 'upsert_proxy_node_status', fake_upsert_proxy_node_status)
    monkeypatch.setattr(nodeproxy_service_module, 'create_proxy_node_status_log_entry', fake_create_proxy_node_status_log_entry)

    result = await NodeProxyService._persist_health_check_result_async(
        service,
        node_id=uuid4(),
        status_id=None,
        available=False,
        latency=0.5,
        started_at=0.0,
        previous_available=True,
        error_message='health check failed',
    )

    assert result == status_id
    assert captured_kwargs['request_protocol'] == ProtocolType.openai
    assert captured_kwargs['action'] == RequestAction.healthcheck


@pytest.mark.asyncio
async def test_acquire_rollup_task_lock_logs_skip_when_locked(monkeypatch):
    service = _build_service()
    info_messages: list[str] = []

    async def fake_acquire_database_task_lock_transactionally(**kwargs):
        del kwargs
        return False

    monkeypatch.setattr(service, '_build_rollup_task_owner_token', lambda: 'worker-a')
    monkeypatch.setattr(
        nodeproxy_service_module,
        'acquire_database_task_lock_transactionally',
        fake_acquire_database_task_lock_transactionally,
    )
    monkeypatch.setattr(nodeproxy_service_module.logger, 'info', lambda message, *args: info_messages.append(message.format(*args) if args else message))

    owner_token = await NodeProxyService._acquire_rollup_task_lock(
        service,
        task_name='daily_usage_rollup',
        task_label='昨日应用模型用量汇总',
    )

    assert owner_token is None
    assert info_messages == ['昨日应用模型用量汇总已有任务在执行，忽略本次调度']


@pytest.mark.asyncio
async def test_rollup_previous_day_usage_returns_none_when_lock_not_acquired(monkeypatch):
    service = _build_service()

    async def fake_acquire_rollup_task_lock(**kwargs):
        del kwargs
        return None

    monkeypatch.setattr(service, '_acquire_rollup_task_lock', fake_acquire_rollup_task_lock)

    result = await NodeProxyService._rollup_previous_day_usage(service)

    assert result is None


def test_status_preserves_alive_state_for_unroutable_trusted_node():
    service = _build_service()
    service._lock = threading.Lock()
    service.snode = {
        'http://trusted-node.example.com': Status(
            models=[],
            avaiaible=True,
            trusted_without_models_endpoint=True,
        )
    }
    service.nodes = {}

    snapshot = service.status

    assert snapshot['http://trusted-node.example.com'].avaiaible is True