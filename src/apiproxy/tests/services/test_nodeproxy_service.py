import asyncio

import httpx
import orjson
import pytest
import requests

from openaiproxy.services.nodeproxy.constants import ErrorCodes
from openaiproxy.services.nodeproxy.service import NodeProxyService


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


def _build_service() -> NodeProxyService:
    return object.__new__(NodeProxyService)


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
async def test_generate_cancellation_is_reraised(monkeypatch):
    service = _build_service()

    monkeypatch.setattr(httpx, 'AsyncClient', _CancelledAsyncClient)

    with pytest.raises(asyncio.CancelledError):
        await service.generate(
            request={'stream': False},
            node_url='http://node.example.com',
            endpoint='/v1/chat/completions',
        )