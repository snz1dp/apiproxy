"""Anthropic-compatible northbound routes."""

from __future__ import annotations

import asyncio
import threading
import time
import traceback
from http import HTTPStatus
from typing import Any, Dict, Optional

import httpx
import orjson
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from openaiproxy.api.schemas import DisconnectHandlerStreamingResponse
from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.api.v1.completions import (
    _apply_backend_error_info,
    _apply_usage_to_context,
    _build_backend_json_response,
    _extract_backend_error,
    _finalize_token_counts,
    _merge_error_info,
    _try_loads_json,
)
from openaiproxy.api.v1.protocol_adapters import (
    anthropic_messages_to_openai_request,
    build_anthropic_count_tokens_payload,
    estimate_anthropic_input_tokens,
    iter_anthropic_sse_from_openai,
    openai_response_to_anthropic_payload,
)
from openaiproxy.logging import logger
from openaiproxy.services.database.models.node.model import ModelType, ProtocolType
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.services.deps import get_node_proxy_service
from openaiproxy.services.nodeproxy.exceptions import (
    ApiKeyQuotaExceeded,
    AppQuotaExceeded,
    NorthboundQuotaProcessingError,
    NodeModelQuotaExceeded,
)
from openaiproxy.services.nodeproxy.service import NodeProxyService
from openaiproxy.utils.viagateway import get_client_real_ip_via_gateway

router = APIRouter(tags=['Anthropic兼容接口'])

_BATCH_STORE: dict[str, dict[str, Any]] = {}
_BATCH_STORE_LOCK = threading.RLock()
_NATIVE_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=30.0)


def _anthropic_error_response(
    status_code: int,
    message: str,
    error_type: str = 'invalid_request_error',
) -> JSONResponse:
    """Build Anthropic-compatible error responses."""
    return JSONResponse(
        {
            'type': 'error',
            'error': {
                'type': error_type,
                'message': message,
            },
        },
        status_code=status_code,
    )


def _build_anthropic_headers(api_key: Optional[str]) -> dict[str, str]:
    """Build Anthropic-compatible backend headers."""
    headers = {'anthropic-version': '2023-06-01'}
    if api_key:
        headers['x-api-key'] = api_key
    return headers


def _extract_anthropic_text(payload: Dict[str, Any]) -> str:
    """Extract plain text from Anthropic message payload."""
    content = payload.get('content') if isinstance(payload.get('content'), list) else []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get('type') == 'text':
            text = block.get('text')
            if isinstance(text, str):
                parts.append(text)
    return ''.join(parts)


def _build_anthropic_response(payload: Dict[str, Any]) -> JSONResponse:
    """Build HTTP response for Anthropic payloads."""
    if payload.get('type') == 'error' or isinstance(payload.get('error'), dict):
        return JSONResponse(payload, status_code=int(HTTPStatus.BAD_GATEWAY))
    return JSONResponse(payload, status_code=int(HTTPStatus.OK))


def _resolve_target_protocol(node_status: Any) -> ProtocolType:
    """Resolve backend protocol used for an Anthropic northbound request."""
    protocol_type = getattr(node_status, 'protocol_type', ProtocolType.openai)
    if protocol_type in {ProtocolType.anthropic, ProtocolType.both}:
        return ProtocolType.anthropic
    return ProtocolType.openai


def _get_node_runtime_config(
    nodeproxy_service: NodeProxyService,
    node_url: str,
) -> tuple[Any, Optional[str], ProtocolType, Optional[str]]:
    """Resolve node runtime status and protocol config."""
    status_snapshot = nodeproxy_service.status
    node_status = status_snapshot.get(node_url) if isinstance(status_snapshot, dict) else None
    api_key = getattr(node_status, 'api_key', None) if node_status is not None else None
    request_proxy_url = getattr(node_status, 'request_proxy_url', None) if node_status is not None else None
    target_protocol = _resolve_target_protocol(node_status)
    return node_status, api_key, target_protocol, request_proxy_url


async def _request_native_anthropic_json(
    *,
    node_url: str,
    endpoint: str,
    api_key: Optional[str],
    request_proxy_url: Optional[str],
    method: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Issue direct Anthropic-compatible JSON requests for batch management."""
    target_url = f"{node_url.rstrip('/')}{endpoint}"
    headers = _build_anthropic_headers(api_key)
    async with httpx.AsyncClient(proxy=request_proxy_url, timeout=_NATIVE_REQUEST_TIMEOUT) as client:
        response = await client.request(method=method, url=target_url, headers=headers, json=payload)
    try:
        return response.json()
    except ValueError as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=int(HTTPStatus.BAD_GATEWAY),
            detail='Anthropic后端返回非JSON响应',
        ) from exc


def _store_batch(batch_id: str, batch_payload: Dict[str, Any]) -> None:
    """Store batch metadata in local runtime memory."""
    with _BATCH_STORE_LOCK:
        _BATCH_STORE[batch_id] = batch_payload


def _get_stored_batch(batch_id: str) -> Optional[Dict[str, Any]]:
    """Fetch stored batch metadata from local runtime memory."""
    with _BATCH_STORE_LOCK:
        return _BATCH_STORE.get(batch_id)


@router.post('/messages')
async def anthropic_messages(
    request_payload: Dict[str, Any],
    raw_request: Request,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Anthropic-compatible messages endpoint."""
    model_name = request_payload.get('model')
    if not isinstance(model_name, str) or not model_name.strip():
        return _anthropic_error_response(int(HTTPStatus.BAD_REQUEST), 'model is required')

    model_type = ModelType.chat.value
    if not nodeproxy_service.supports_model(
        model_name,
        model_type,
        request_protocol=ProtocolType.anthropic,
        allow_cross_protocol=True,
    ):
        return _anthropic_error_response(int(HTTPStatus.NOT_FOUND), f'Model {model_name} is not available')

    try:
        node_url = nodeproxy_service.get_node_url(
            model_name,
            model_type,
            request_protocol=ProtocolType.anthropic,
            allow_cross_protocol=True,
        )
    except NodeModelQuotaExceeded as exc:
        message = exc.detail or str(exc) or '模型配额已耗尽'
        logger.warning('节点模型配额不足: {}', message)
        return _anthropic_error_response(int(HTTPStatus.TOO_MANY_REQUESTS), message, 'rate_limit_error')

    if not node_url:
        return _anthropic_error_response(int(HTTPStatus.NOT_FOUND), f'Model {model_name} is not available')

    request_payload_json = orjson.dumps(request_payload).decode('utf-8', errors='ignore')
    prompt_token_estimate = estimate_anthropic_input_tokens(request_payload)
    total_token_estimate = prompt_token_estimate + max(int(request_payload.get('max_tokens') or 0), 0)
    client_ip = get_client_real_ip_via_gateway(raw_request)

    try:
        request_ctx = nodeproxy_service.pre_call(
            node_url,
            model_name=model_name,
            model_type=model_type,
            request_protocol=ProtocolType.anthropic,
            ownerapp_id=access_ctx.ownerapp_id,
            request_action=RequestAction.completions,
            request_count=prompt_token_estimate,
            estimated_total_tokens=total_token_estimate,
            stream=bool(request_payload.get('stream')),
            request_data=request_payload_json,
            client_ip=client_ip,
            api_key_id=access_ctx.api_key_id,
        )
    except (NodeModelQuotaExceeded, ApiKeyQuotaExceeded, AppQuotaExceeded) as exc:
        message = str(exc) or '配额已耗尽'
        return _anthropic_error_response(int(HTTPStatus.TOO_MANY_REQUESTS), message, 'rate_limit_error')
    except NorthboundQuotaProcessingError as exc:
        message = exc.detail or str(exc) or '北向配额处理失败'
        return _anthropic_error_response(int(HTTPStatus.SERVICE_UNAVAILABLE), message, 'api_error')

    _, api_key, target_protocol, request_proxy_url = _get_node_runtime_config(nodeproxy_service, node_url)
    backend_request = (
        anthropic_messages_to_openai_request(request_payload)
        if target_protocol == ProtocolType.openai
        else request_payload
    )
    backend_endpoint = '/v1/chat/completions' if target_protocol == ProtocolType.openai else '/v1/messages'

    if request_payload.get('stream'):
        raw_stream = nodeproxy_service.stream_generate(
            backend_request,
            node_url,
            backend_endpoint,
            api_key,
            protocol_type=target_protocol,
            request_proxy_url=request_proxy_url,
        )
        if target_protocol == ProtocolType.openai:
            raw_stream = iter_anthropic_sse_from_openai(raw_stream, model_name=model_name)

        completion_segments: list[str] = []
        raw_response_chunks: list[str] = []
        backend_error: Dict[str, Optional[str]] = {'message': None, 'stack': None}
        client_disconnected = False
        stream_completed = False
        current_event = 'message'

        def _mark_client_disconnect() -> None:
            nonlocal client_disconnected, stream_completed
            if stream_completed or client_disconnected:
                return
            client_disconnected = True
            request_ctx.abort = True
            _merge_error_info(backend_error, 'Client disconnected during streaming', None)

        def stream_with_usage_logging():
            nonlocal stream_completed, current_event
            try:
                for chunk in raw_stream:
                    if isinstance(chunk, (bytes, bytearray)):
                        text = chunk.decode('utf-8', errors='ignore')
                    else:
                        text = str(chunk)
                    if text:
                        raw_response_chunks.append(text)
                        for line in text.splitlines():
                            stripped = line.strip()
                            if not stripped:
                                continue
                            if stripped.startswith('event:'):
                                current_event = stripped[6:].strip() or 'message'
                                continue
                            if not stripped.startswith('data:'):
                                continue
                            payload_text = stripped[5:].strip()
                            if not payload_text:
                                continue
                            payload_obj = _try_loads_json(payload_text)
                            if not isinstance(payload_obj, dict):
                                continue
                            if request_ctx.first_response_time is None and current_event in {'content_block_start', 'content_block_delta'}:
                                request_ctx.first_response_time = time.time()
                            if current_event == 'content_block_start':
                                content_block = payload_obj.get('content_block') if isinstance(payload_obj.get('content_block'), dict) else {}
                                text_block = content_block.get('text')
                                if isinstance(text_block, str) and text_block:
                                    completion_segments.append(text_block)
                            elif current_event == 'content_block_delta':
                                delta = payload_obj.get('delta') if isinstance(payload_obj.get('delta'), dict) else {}
                                text_delta = delta.get('text')
                                if isinstance(text_delta, str) and text_delta:
                                    completion_segments.append(text_delta)
                            usage_payload = payload_obj.get('usage') if isinstance(payload_obj.get('usage'), dict) else None
                            if isinstance(usage_payload, dict):
                                _apply_usage_to_context(request_ctx, usage_payload)
                            message, stack = _extract_backend_error(payload_obj)
                            _merge_error_info(backend_error, message, stack)
                    yield chunk
                stream_completed = True
            except GeneratorExit:
                _mark_client_disconnect()
                raise
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, asyncio.CancelledError) or exc.__class__.__name__ in {'ClientDisconnect', 'ClientDisconnectError'}:
                    _mark_client_disconnect()
                raise
            finally:
                if backend_error['message'] or backend_error['stack']:
                    _apply_backend_error_info(
                        request_ctx,
                        backend_error.get('message'),
                        backend_error.get('stack'),
                    )
                if raw_response_chunks:
                    request_ctx.response_data = ''.join(raw_response_chunks)
                _finalize_token_counts(
                    request_ctx=request_ctx,
                    prompt_estimate=prompt_token_estimate,
                    completion_segments=completion_segments,
                    model_name=model_name,
                )

        background_task = nodeproxy_service.create_background_tasks(node_url, request_ctx)
        return DisconnectHandlerStreamingResponse(
            stream_with_usage_logging(),
            media_type='text/event-stream',
            background=background_task,
            on_disconnect=_mark_client_disconnect,
        )

    response = await nodeproxy_service.generate(
        backend_request,
        node_url,
        backend_endpoint,
        api_key,
        protocol_type=target_protocol,
        request_proxy_url=request_proxy_url,
    )
    try:
        payload = orjson.loads(response)
    except Exception:  # noqa: BLE001
        error_message = f'Failed to decode backend response: {response!r}'
        stack = traceback.format_exc()
        _apply_backend_error_info(request_ctx, error_message, stack)
        nodeproxy_service.post_call(node_url, request_ctx)
        raise

    if target_protocol == ProtocolType.openai:
        payload = openai_response_to_anthropic_payload(payload, model_name)
        response = orjson.dumps(payload).decode('utf-8', errors='ignore')
    request_ctx.response_data = response
    message, stack = _extract_backend_error(payload)
    _apply_backend_error_info(request_ctx, message, stack)
    usage = payload.get('usage') if isinstance(payload, dict) else None
    if isinstance(usage, dict):
        _apply_usage_to_context(request_ctx, usage)
    _finalize_token_counts(
        request_ctx=request_ctx,
        prompt_estimate=prompt_token_estimate,
        completion_segments=[_extract_anthropic_text(payload)],
        model_name=model_name,
    )
    nodeproxy_service.post_call(node_url, request_ctx)
    return _build_anthropic_response(payload)


@router.post('/messages/count_tokens')
async def anthropic_count_tokens(
    request_payload: Dict[str, Any],
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Anthropic-compatible count_tokens endpoint."""
    del access_ctx
    model_name = request_payload.get('model')
    if not isinstance(model_name, str) or not model_name.strip():
        return _anthropic_error_response(int(HTTPStatus.BAD_REQUEST), 'model is required')
    if not nodeproxy_service.supports_model(
        model_name,
        ModelType.chat.value,
        request_protocol=ProtocolType.anthropic,
        allow_cross_protocol=True,
    ):
        return _anthropic_error_response(int(HTTPStatus.NOT_FOUND), f'Model {model_name} is not available')
    try:
        node_url = nodeproxy_service.get_node_url(
            model_name,
            ModelType.chat.value,
            request_protocol=ProtocolType.anthropic,
            allow_cross_protocol=True,
        )
    except NodeModelQuotaExceeded as exc:
        message = exc.detail or str(exc) or '模型配额已耗尽'
        return _anthropic_error_response(int(HTTPStatus.TOO_MANY_REQUESTS), message, 'rate_limit_error')

    if not node_url:
        return _anthropic_error_response(int(HTTPStatus.NOT_FOUND), f'Model {model_name} is not available')

    _, api_key, target_protocol, request_proxy_url = _get_node_runtime_config(nodeproxy_service, node_url)
    if target_protocol == ProtocolType.openai:
        return build_anthropic_count_tokens_payload(request_payload)

    payload = await _request_native_anthropic_json(
        node_url=node_url,
        endpoint='/v1/messages/count_tokens',
        api_key=api_key,
        request_proxy_url=request_proxy_url,
        method='POST',
        payload=request_payload,
    )
    return _build_anthropic_response(payload)


@router.post('/messages/batches')
async def anthropic_create_message_batch(
    request_payload: Dict[str, Any],
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Create Anthropic-compatible message batches."""
    del access_ctx
    requests_payload = request_payload.get('requests')
    if not isinstance(requests_payload, list) or not requests_payload:
        return _anthropic_error_response(int(HTTPStatus.BAD_REQUEST), 'requests is required')

    first_request = requests_payload[0] if isinstance(requests_payload[0], dict) else {}
    params = first_request.get('params') if isinstance(first_request.get('params'), dict) else {}
    model_name = params.get('model')
    if not isinstance(model_name, str) or not model_name.strip():
        return _anthropic_error_response(int(HTTPStatus.BAD_REQUEST), 'batch request params.model is required')

    if not nodeproxy_service.supports_model(
        model_name,
        ModelType.chat.value,
        request_protocol=ProtocolType.anthropic,
        allow_cross_protocol=True,
    ):
        return _anthropic_error_response(int(HTTPStatus.NOT_FOUND), f'Model {model_name} is not available')

    try:
        node_url = nodeproxy_service.get_node_url(
            model_name,
            ModelType.chat.value,
            request_protocol=ProtocolType.anthropic,
            allow_cross_protocol=True,
        )
    except NodeModelQuotaExceeded as exc:
        message = exc.detail or str(exc) or '模型配额已耗尽'
        return _anthropic_error_response(int(HTTPStatus.TOO_MANY_REQUESTS), message, 'rate_limit_error')

    if not node_url:
        return _anthropic_error_response(int(HTTPStatus.NOT_FOUND), f'Model {model_name} is not available')

    _, api_key, target_protocol, request_proxy_url = _get_node_runtime_config(nodeproxy_service, node_url)
    created_at = int(time.time())
    batch_id = f'msgbatch_{created_at}_{int(time.time() * 1000)}'

    if target_protocol == ProtocolType.anthropic:
        payload = await _request_native_anthropic_json(
            node_url=node_url,
            endpoint='/v1/messages/batches',
            api_key=api_key,
            request_proxy_url=request_proxy_url,
            method='POST',
            payload=request_payload,
        )
        native_batch_id = payload.get('id') if isinstance(payload, dict) else None
        _store_batch(
            native_batch_id or batch_id,
            {
                'native': True,
                'node_url': node_url,
                'api_key': api_key,
                'request_proxy_url': request_proxy_url,
                'last_payload': payload,
                'results': None,
            },
        )
        return _build_anthropic_response(payload)

    results: list[dict[str, Any]] = []
    succeeded = 0
    errored = 0
    for item in requests_payload:
        if not isinstance(item, dict):
            continue
        custom_id = item.get('custom_id')
        params = item.get('params') if isinstance(item.get('params'), dict) else {}
        openai_request = anthropic_messages_to_openai_request(params)
        openai_request['stream'] = False
        response_text = await nodeproxy_service.generate(
            openai_request,
            node_url,
            '/v1/chat/completions',
            api_key,
            protocol_type=ProtocolType.openai,
            request_proxy_url=request_proxy_url,
        )
        try:
            openai_payload = orjson.loads(response_text)
        except Exception:  # noqa: BLE001
            openai_payload = {'error': {'message': response_text}}
        anthropic_payload = openai_response_to_anthropic_payload(openai_payload, params.get('model'))
        is_error = anthropic_payload.get('type') == 'error' or isinstance(anthropic_payload.get('error'), dict)
        if is_error:
            errored += 1
        else:
            succeeded += 1
        results.append(
            {
                'custom_id': custom_id,
                'result': {
                    'type': 'succeeded' if not is_error else 'errored',
                    'message': anthropic_payload,
                },
            }
        )

    batch_payload = {
        'id': batch_id,
        'type': 'message_batch',
        'processing_status': 'ended',
        'request_counts': {
            'processing': 0,
            'succeeded': succeeded,
            'errored': errored,
            'canceled': 0,
            'expired': 0,
        },
        'created_at': created_at,
        'ended_at': created_at,
        'results_url': f'/v1/messages/batches/{batch_id}/results',
    }
    _store_batch(
        batch_id,
        {
            'native': False,
            'node_url': node_url,
            'api_key': api_key,
            'request_proxy_url': request_proxy_url,
            'last_payload': batch_payload,
            'results': results,
        },
    )
    return batch_payload


@router.get('/messages/batches')
async def anthropic_list_message_batches(
    limit: int = Query(20, ge=1, le=100),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """List locally tracked Anthropic-compatible message batches."""
    del access_ctx
    with _BATCH_STORE_LOCK:
        payloads = [
            item.get('last_payload')
            for item in _BATCH_STORE.values()
            if isinstance(item.get('last_payload'), dict)
        ]
    payloads = payloads[:limit]
    return {
        'data': payloads,
        'first_id': payloads[0].get('id') if payloads else None,
        'last_id': payloads[-1].get('id') if payloads else None,
        'has_more': False,
    }


@router.get('/messages/batches/{batch_id}')
async def anthropic_get_message_batch(
    batch_id: str,
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Get batch status for locally tracked or native Anthropic batches."""
    del access_ctx
    batch_entry = _get_stored_batch(batch_id)
    if batch_entry is None:
        return _anthropic_error_response(int(HTTPStatus.NOT_FOUND), 'Message batch not found')
    if batch_entry.get('native'):
        payload = await _request_native_anthropic_json(
            node_url=batch_entry['node_url'],
            endpoint=f'/v1/messages/batches/{batch_id}',
            api_key=batch_entry.get('api_key'),
            request_proxy_url=batch_entry.get('request_proxy_url'),
            method='GET',
        )
        batch_entry['last_payload'] = payload
        return _build_anthropic_response(payload)
    return batch_entry['last_payload']


@router.post('/messages/batches/{batch_id}/cancel')
async def anthropic_cancel_message_batch(
    batch_id: str,
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Cancel a locally tracked or native Anthropic message batch."""
    del access_ctx
    batch_entry = _get_stored_batch(batch_id)
    if batch_entry is None:
        return _anthropic_error_response(int(HTTPStatus.NOT_FOUND), 'Message batch not found')
    if batch_entry.get('native'):
        payload = await _request_native_anthropic_json(
            node_url=batch_entry['node_url'],
            endpoint=f'/v1/messages/batches/{batch_id}/cancel',
            api_key=batch_entry.get('api_key'),
            request_proxy_url=batch_entry.get('request_proxy_url'),
            method='POST',
        )
        batch_entry['last_payload'] = payload
        return _build_anthropic_response(payload)

    payload = dict(batch_entry['last_payload'])
    payload['processing_status'] = 'canceled'
    request_counts = dict(payload.get('request_counts') or {})
    request_counts['processing'] = 0
    payload['request_counts'] = request_counts
    batch_entry['last_payload'] = payload
    return payload


@router.get('/messages/batches/{batch_id}/results')
async def anthropic_get_message_batch_results(
    batch_id: str,
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Get stored synthetic results or proxy native batch results."""
    del access_ctx
    batch_entry = _get_stored_batch(batch_id)
    if batch_entry is None:
        return _anthropic_error_response(int(HTTPStatus.NOT_FOUND), 'Message batch not found')
    if batch_entry.get('native'):
        payload = await _request_native_anthropic_json(
            node_url=batch_entry['node_url'],
            endpoint=f'/v1/messages/batches/{batch_id}/results',
            api_key=batch_entry.get('api_key'),
            request_proxy_url=batch_entry.get('request_proxy_url'),
            method='GET',
        )
        return _build_backend_json_response(payload)
    return {'data': batch_entry.get('results') or []}