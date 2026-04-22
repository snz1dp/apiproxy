import asyncio
from contextlib import asynccontextmanager
import time
import threading

import httpx
import orjson
import pytest
import requests

from openaiproxy.services.nodeproxy.constants import ErrorCodes
from openaiproxy.services.nodeproxy.exceptions import NorthboundQuotaProcessingError
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.services.nodeproxy.schemas import Status
from openaiproxy.services.nodeproxy import service as nodeproxy_service_module
from openaiproxy.services.nodeproxy.service import NodeProxyService, _RequestContext


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
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def post(self, *args, **kwargs):
        del args, kwargs
        raise httpx.TimeoutException('timeout')


class _CancelledAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def post(self, *args, **kwargs):
        del args, kwargs
        raise asyncio.CancelledError()


class _HttpErrorAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def post(self, *args, **kwargs):
        del args, kwargs
        raise httpx.ConnectError('connect failed')


def _build_service() -> NodeProxyService:
    return object.__new__(NodeProxyService)


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
        lambda node_url, api_key: checked_nodes.append(node_url),
    )

    service.perform_node_health_checks()

    assert checked_nodes == ['http://normal-node.example.com']


@pytest.mark.asyncio
async def test_acquire_rollup_task_lock_logs_skip_when_locked(monkeypatch):
    service = _build_service()
    fake_session = _FakeAsyncSession()
    info_messages: list[str] = []

    @asynccontextmanager
    async def fake_async_session_scope():
        yield fake_session

    async def fake_acquire_database_task_lock(**kwargs):
        del kwargs
        return False

    monkeypatch.setattr(service, '_build_rollup_task_owner_token', lambda: 'worker-a')
    monkeypatch.setattr(nodeproxy_service_module, 'async_session_scope', fake_async_session_scope)
    monkeypatch.setattr(nodeproxy_service_module, 'acquire_database_task_lock', fake_acquire_database_task_lock)
    monkeypatch.setattr(nodeproxy_service_module.logger, 'info', lambda message, *args: info_messages.append(message.format(*args) if args else message))

    owner_token = await NodeProxyService._acquire_rollup_task_lock(
        service,
        task_name='daily_usage_rollup',
        task_label='昨日应用模型用量汇总',
    )

    assert owner_token is None
    assert fake_session.rollback_calls == 1
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