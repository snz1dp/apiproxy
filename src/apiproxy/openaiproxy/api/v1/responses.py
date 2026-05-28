from __future__ import annotations

import asyncio
from http import HTTPStatus
import time
import traceback
from typing import Any, Dict, List, Optional

import orjson
from fastapi import APIRouter, Depends, Request

from openaiproxy.api.schemas import DisconnectHandlerStreamingResponse, ResponsesRequest
from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.api.v1.completions import (
    _apply_backend_error_info,
    _apply_usage_to_context,
    _build_backend_json_response,
    _estimate_tokens,
    _extract_backend_error,
    _finalize_token_counts,
    _merge_error_info,
    _normalize_content_to_text,
    _safe_int,
    _to_error_text,
    _try_loads_json,
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
from openaiproxy.services.nodeproxy.service import NodeProxyService, create_error_response
from openaiproxy.utils.viagateway import get_client_real_ip_via_gateway


def _estimate_responses_prompt_tokens(request: ResponsesRequest) -> int:
    """Estimate prompt tokens for Responses requests."""
    parts = [
        _normalize_content_to_text(request.instructions),
        _normalize_content_to_text(request.input),
    ]
    prompt_text = ''.join(part for part in parts if part)
    return _estimate_tokens(prompt_text, request.model)


def _estimate_responses_total_tokens(request: ResponsesRequest) -> int:
    """Estimate the upper bound of tokens consumed by a Responses request."""
    prompt_tokens = _estimate_responses_prompt_tokens(request)
    extra_fields = getattr(request, 'model_extra', None) or {}
    completion_limit = _safe_int(request.max_output_tokens)
    if completion_limit is None:
        completion_limit = _safe_int(extra_fields.get('max_completion_tokens'))
    if completion_limit is None:
        completion_limit = _safe_int(extra_fields.get('max_tokens'))
    return prompt_tokens + max(completion_limit or 0, 0)


def _append_responses_text(
    payload: Any,
    acc: List[str],
    *,
    include_response: bool,
) -> None:
    """Collect textual assistant output from Responses payloads."""
    if payload is None:
        return
    if isinstance(payload, list):
        for item in payload:
            _append_responses_text(item, acc, include_response=include_response)
        return
    if not isinstance(payload, dict):
        return

    payload_type = payload.get('type')
    if payload_type == 'response.output_text.delta':
        text = _normalize_content_to_text(payload.get('delta'))
        if text:
            acc.append(text)
        return

    if payload_type in {'response.output_text.done', 'output_text'}:
        text = _normalize_content_to_text(payload.get('text'))
        if text:
            acc.append(text)
        return

    if include_response and 'response' in payload:
        _append_responses_text(payload.get('response'), acc, include_response=include_response)

    for key in ('output', 'item', 'part', 'content'):
        if key in payload:
            _append_responses_text(payload.get(key), acc, include_response=include_response)


def _apply_responses_usage_to_context(request_ctx: Any, payload: Any) -> None:
    """Apply usage blocks from Responses payloads to the request context."""
    if not isinstance(payload, dict):
        return

    usage = payload.get('usage')
    if isinstance(usage, dict):
        _apply_usage_to_context(request_ctx, usage)

    response_obj = payload.get('response')
    if isinstance(response_obj, dict):
        response_usage = response_obj.get('usage')
        if isinstance(response_usage, dict):
            _apply_usage_to_context(request_ctx, response_usage)


def _extract_responses_error(payload: Any) -> tuple[Optional[str], Optional[str]]:
    """Extract backend error details from Responses payloads."""
    message, stack = _extract_backend_error(payload)
    if message or stack:
        return message, stack

    if isinstance(payload, dict) and isinstance(payload.get('response'), dict):
        return _extract_backend_error(payload['response'])
    return None, None


router = APIRouter(tags=['OpenAI兼容接口'])


@router.post('/responses')
async def responses_v1(
    request: ResponsesRequest,
    raw_request: Request = None,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Proxy OpenAI Responses requests to OpenAI-compatible backend nodes."""
    model_type = ModelType.chat.value
    check_response = await nodeproxy_service.check_request_model(
        request.model,
        model_type,
        request_protocol=ProtocolType.openai,
        allow_cross_protocol=False,
        effective_allowed_models=access_ctx.effective_allowed_models,
    )
    if check_response is not None:
        return check_response

    try:
        node_url = nodeproxy_service.get_node_url(
            request.model,
            model_type,
            request_protocol=ProtocolType.openai,
            allow_cross_protocol=False,
        )
    except NodeModelQuotaExceeded as exc:
        message = exc.detail or str(exc) or '模型配额已耗尽'
        logger.warning('节点模型配额不足: {}', message)
        return create_error_response(HTTPStatus.TOO_MANY_REQUESTS, message, error_type='quota_exceeded')

    if not node_url:
        return nodeproxy_service.handle_unavailable_model(request.model, model_type)

    logger.debug('应用 {} 将 Responses 请求转发到节点 {}', access_ctx.ownerapp_id, node_url)
    request_dict = request.model_dump(exclude_none=True)
    request_payload = orjson.dumps(request_dict).decode('utf-8', errors='ignore')
    prompt_token_estimate = _estimate_responses_prompt_tokens(request)
    total_token_estimate = _estimate_responses_total_tokens(request)
    client_ip = get_client_real_ip_via_gateway(raw_request)
    try:
        request_ctx = nodeproxy_service.pre_call(
            node_url,
            model_name=request.model,
            model_type=model_type,
            request_protocol=ProtocolType.openai,
            ownerapp_id=access_ctx.ownerapp_id,
            request_action=RequestAction.responses,
            request_count=prompt_token_estimate,
            estimated_total_tokens=total_token_estimate,
            stream=request.stream,
            request_data=request_payload,
            client_ip=client_ip,
            api_key_id=access_ctx.api_key_id,
        )
    except (NodeModelQuotaExceeded, ApiKeyQuotaExceeded, AppQuotaExceeded) as exc:
        message = str(exc) or '配额已耗尽'
        logger.warning('配额不足: {}', message)
        return create_error_response(HTTPStatus.TOO_MANY_REQUESTS, message, error_type='quota_exceeded')
    except NorthboundQuotaProcessingError as exc:
        message = exc.detail or str(exc) or '北向配额处理失败'
        logger.warning('北向配额处理异常: {}', message)
        return create_error_response(HTTPStatus.SERVICE_UNAVAILABLE, message, error_type='service_unavailable_error')

    status_snapshot = nodeproxy_service.status
    node_status = status_snapshot.get(node_url) if isinstance(status_snapshot, dict) else None
    api_key = getattr(node_status, 'api_key', None) if node_status is not None else None
    request_proxy_url = getattr(node_status, 'request_proxy_url', None) if node_status is not None else None
    backend_endpoint = '/v1/responses'

    if request.stream is True:
        raw_stream = nodeproxy_service.stream_generate(
            request_dict,
            node_url,
            backend_endpoint,
            api_key,
            protocol_type=ProtocolType.openai,
            request_proxy_url=request_proxy_url,
        )

        completion_segments: List[str] = []
        raw_response_chunks: List[str] = []
        backend_error: Dict[str, Optional[str]] = {'message': None, 'stack': None}
        client_disconnected = False
        stream_completed = False
        first_token_recorded = False

        def _mark_client_disconnect() -> None:
            nonlocal client_disconnected, stream_completed
            if stream_completed or client_disconnected:
                return
            client_disconnected = True
            request_ctx.abort = True
            _merge_error_info(backend_error, 'Client disconnected during streaming', None)

        def stream_with_usage_logging():
            nonlocal first_token_recorded, stream_completed
            try:
                for chunk in raw_stream:
                    logger.debug('Responses 流式数据片段: {}', chunk)
                    if isinstance(chunk, (bytes, bytearray)):
                        try:
                            text = chunk.decode('utf-8', errors='ignore')
                        except Exception:  # noqa: BLE001
                            text = ''
                    elif isinstance(chunk, str):
                        text = chunk
                    else:
                        text = str(chunk)

                    if text:
                        raw_response_chunks.append(text)
                        for line in text.splitlines():
                            stripped = line.strip()
                            if not stripped:
                                continue
                            payload_obj: Optional[Any] = None
                            is_data_line = stripped.startswith('data:')
                            if is_data_line:
                                payload = stripped[5:].strip()
                                if not payload or payload == '[DONE]':
                                    continue
                                if request_ctx.first_response_time is None and not first_token_recorded:
                                    request_ctx.first_response_time = time.time()
                                    first_token_recorded = True
                                payload_obj = _try_loads_json(payload)
                                if isinstance(payload_obj, dict):
                                    _append_responses_text(payload_obj, completion_segments, include_response=False)
                            elif stripped.startswith('event:') or stripped.startswith(':'):
                                continue
                            else:
                                payload_obj = _try_loads_json(stripped)

                            if payload_obj is not None:
                                _apply_responses_usage_to_context(request_ctx, payload_obj)
                                message, stack = _extract_responses_error(payload_obj)
                                _merge_error_info(backend_error, message, stack)
                            elif not is_data_line:
                                fallback_msg = _to_error_text(stripped)
                                if fallback_msg:
                                    _merge_error_info(backend_error, fallback_msg, None)
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
                    model_name=request.model,
                )

        background_task = nodeproxy_service.create_background_tasks(node_url, request_ctx)
        return DisconnectHandlerStreamingResponse(
            stream_with_usage_logging(),
            background=background_task,
            media_type='text/event-stream',
            on_disconnect=_mark_client_disconnect,
        )

    response = await nodeproxy_service.generate(
        request_dict,
        node_url,
        backend_endpoint,
        api_key,
        protocol_type=ProtocolType.openai,
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

    request_ctx.response_data = response
    message, stack = _extract_responses_error(payload)
    _apply_backend_error_info(request_ctx, message, stack)
    _apply_responses_usage_to_context(request_ctx, payload)
    completion_segments: List[str] = []
    _append_responses_text(payload, completion_segments, include_response=True)
    _finalize_token_counts(
        request_ctx=request_ctx,
        prompt_estimate=prompt_token_estimate,
        completion_segments=completion_segments,
        model_name=request.model,
    )
    nodeproxy_service.post_call(node_url, request_ctx)
    return _build_backend_json_response(payload)