from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Optional

import orjson
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.formparsers import MultiPartException

from openaiproxy.api.schemas import AudioSpeechRequest
from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.api.v1.completions import (
    _build_backend_json_response,
    _build_openai_quota_exceeded_response,
    _build_openai_service_unavailable_response,
    _prepare_proxy_attempt,
    _resolve_default_target_protocol,
    _retry_proxy_attempt_after_capacity_exhausted,
)
from openaiproxy.api.v1.embeddings import _apply_backend_error_info, _extract_backend_error
from openaiproxy.logging import logger
from openaiproxy.services.database.models.node.model import ModelType, ProtocolType
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.services.deps import get_node_proxy_service
from openaiproxy.services.nodeproxy.exceptions import (
    NodeModelQuotaExceeded,
)
from openaiproxy.services.nodeproxy.service import NodeProxyService, create_error_response
from openaiproxy.utils.viagateway import get_client_real_ip_via_gateway

router = APIRouter(tags=["OpenAI兼容接口"])

_SPEECH_RESPONSE_FORMATS: dict[str, tuple[str, str]] = {
    'aac': ('audio/aac', 'speech.aac'),
    'flac': ('audio/flac', 'speech.flac'),
    'mp3': ('audio/mpeg', 'speech.mp3'),
    'opus': ('audio/ogg', 'speech.opus'),
    'pcm': ('audio/pcm', 'speech.pcm'),
    'wav': ('audio/wav', 'speech.wav'),
}

_TRANSCRIPT_RESPONSE_FORMATS: dict[str, str] = {
    'json': 'application/json',
    'srt': 'application/x-subrip',
    'text': 'text/plain; charset=utf-8',
    'txt': 'text/plain; charset=utf-8',
    'verbose_json': 'application/json',
    'vtt': 'text/vtt; charset=utf-8',
}


@dataclass(slots=True)
class AudioProxyContext:
    """Resolved backend context for an audio proxy request."""

    node_url: str
    api_key: Optional[str]
    request_ctx: Any
    request_proxy_url: Optional[str]
    target_protocol: ProtocolType


def _append_form_value(form_fields: dict[str, Any], field_name: str, field_value: Any) -> None:
    """Append a multipart form field while preserving repeated keys."""
    normalized_value = field_value if isinstance(field_value, str) else str(field_value)
    existing_value = form_fields.get(field_name)
    if existing_value is None:
        form_fields[field_name] = normalized_value
        return
    if isinstance(existing_value, list):
        existing_value.append(normalized_value)
        return
    form_fields[field_name] = [existing_value, normalized_value]


def _extract_first_form_value(form_fields: dict[str, Any], field_name: str) -> Optional[str]:
    """Extract the first non-empty string value from a parsed multipart field."""
    field_value = form_fields.get(field_name)
    if isinstance(field_value, list):
        for item in field_value:
            if isinstance(item, str) and item.strip():
                return item.strip()
        return None
    if isinstance(field_value, str) and field_value.strip():
        return field_value.strip()
    return None


def _has_uploaded_file(file_summaries: list[dict[str, Optional[str]]], field_name: str) -> bool:
    """Check whether a multipart request contains a file for the given field."""
    return any(file_item.get('field') == field_name for file_item in file_summaries)


async def _parse_audio_multipart_request(
    raw_request: Request,
) -> tuple[bytes, dict[str, Any], list[dict[str, Optional[str]]]]:
    """Parse multipart audio requests while preserving the original body bytes."""
    request_body = await raw_request.body()
    form_data = await raw_request.form()
    form_fields: dict[str, Any] = {}
    file_summaries: list[dict[str, Optional[str]]] = []

    for field_name, field_value in form_data.multi_items():
        if isinstance(field_value, StarletteUploadFile):
            file_summaries.append(
                {
                    'field': field_name,
                    'filename': field_value.filename,
                    'content_type': field_value.content_type,
                }
            )
            continue
        _append_form_value(form_fields, field_name, field_value)

    return request_body, form_fields, file_summaries


def _resolve_speech_response_metadata(request_payload: dict[str, Any]) -> tuple[str, str]:
    """Resolve the media type and download filename for a speech response."""
    response_format = str(
        request_payload.get('response_format')
        or request_payload.get('format')
        or 'mp3'
    ).strip().lower()
    return _SPEECH_RESPONSE_FORMATS.get(response_format, _SPEECH_RESPONSE_FORMATS['mp3'])


def _resolve_transcript_media_type(form_fields: dict[str, Any]) -> str:
    """Resolve the media type for transcription and translation responses."""
    response_format = str(form_fields.get('response_format') or 'json').strip().lower()
    return _TRANSCRIPT_RESPONSE_FORMATS.get(response_format, _TRANSCRIPT_RESPONSE_FORMATS['json'])


def _try_parse_structured_payload(response_payload: bytes | str) -> Optional[Any]:
    """Parse JSON payloads while ignoring plain text and binary content."""
    try:
        payload = orjson.loads(response_payload)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(payload, (dict, list)):
        return payload
    return None


async def _prepare_audio_proxy_context(
    *,
    raw_request: Request,
    nodeproxy_service: NodeProxyService,
    access_ctx: AccessKeyContext,
    model_name: str,
    model_type: str,
    request_action: RequestAction,
    request_log_payload: dict[str, Any],
) -> AudioProxyContext | Response:
    """Validate model access, select a node and create request bookkeeping context."""
    check_response = await nodeproxy_service.check_request_model(
        model_name,
        model_type,
        request_protocol=ProtocolType.openai,
        allow_cross_protocol=False,
        effective_allowed_models=access_ctx.effective_allowed_models,
    )
    if check_response is not None:
        return check_response

    try:
        node_url = nodeproxy_service.get_node_url(
            model_name,
            model_type,
            request_protocol=ProtocolType.openai,
            allow_cross_protocol=False,
        )
    except NodeModelQuotaExceeded as exc:
        message = exc.detail or str(exc) or '模型配额已耗尽'
        logger.warning('节点模型配额不足: {}', message)
        return create_error_response(HTTPStatus.TOO_MANY_REQUESTS, message, error_type='quota_exceeded')
    if not node_url:
        return nodeproxy_service.handle_unavailable_model(model_name, model_type)

    logger.debug('应用 {} 将音频请求转发到节点 {}', access_ctx.ownerapp_id, node_url)

    request_payload = orjson.dumps(request_log_payload).decode('utf-8', errors='ignore')
    client_ip = get_client_real_ip_via_gateway(raw_request)
    error_response, attempt = _prepare_proxy_attempt(
        nodeproxy_service=nodeproxy_service,
        node_url=node_url,
        model_name=model_name,
        model_type=model_type,
        request_protocol=ProtocolType.openai,
        ownerapp_id=access_ctx.ownerapp_id,
        request_action=request_action,
        request_count=0,
        estimated_total_tokens=None,
        stream=False,
        request_data=request_payload,
        client_ip=client_ip,
        api_key_id=access_ctx.api_key_id,
        protocol_resolver=_resolve_default_target_protocol,
        quota_error_builder=_build_openai_quota_exceeded_response,
        service_unavailable_builder=_build_openai_service_unavailable_response,
    )
    if error_response is not None:
        return error_response
    assert attempt is not None

    return AudioProxyContext(
        node_url=attempt.node_url,
        api_key=attempt.api_key,
        request_ctx=attempt.request_ctx,
        request_proxy_url=attempt.request_proxy_url,
        target_protocol=attempt.target_protocol,
    )


async def _proxy_audio_request(
    *,
    raw_request: Request,
    nodeproxy_service: NodeProxyService,
    access_ctx: AccessKeyContext,
    model_name: str,
    model_type: str,
    backend_endpoint: str,
    request_action: RequestAction,
    request_log_payload: dict[str, Any],
    response_media_type: str,
    response_filename: Optional[str] = None,
    backend_request_json: Optional[dict[str, Any]] = None,
    request_content: Optional[bytes] = None,
    extra_headers: Optional[dict[str, str]] = None,
) -> Response:
    """Proxy an audio request and return either structured JSON or raw content."""
    request_payload = orjson.dumps(request_log_payload).decode('utf-8', errors='ignore')
    proxy_context = await _prepare_audio_proxy_context(
        raw_request=raw_request,
        nodeproxy_service=nodeproxy_service,
        access_ctx=access_ctx,
        model_name=model_name,
        model_type=model_type,
        request_action=request_action,
        request_log_payload=request_log_payload,
    )
    if isinstance(proxy_context, Response):
        return proxy_context

    attempted_node_urls: set[str] = set()
    while True:
        response_payload = await nodeproxy_service.generate(
            backend_request_json,
            proxy_context.node_url,
            backend_endpoint,
            proxy_context.api_key,
            protocol_type=proxy_context.target_protocol,
            request_proxy_url=proxy_context.request_proxy_url,
            request_content=request_content,
            extra_headers=extra_headers,
            response_mode='bytes',
        )

        structured_payload = _try_parse_structured_payload(response_payload)
        if structured_payload is not None and NodeProxyService.is_backend_capacity_exhausted_error(structured_payload):
            error_response, next_attempt = _retry_proxy_attempt_after_capacity_exhausted(
                nodeproxy_service=nodeproxy_service,
                current_attempt=proxy_context,
                payload=structured_payload,
                attempted_node_urls=attempted_node_urls,
                model_name=model_name,
                model_type=model_type,
                request_protocol=ProtocolType.openai,
                allow_cross_protocol=False,
                ownerapp_id=access_ctx.ownerapp_id,
                request_action=request_action,
                request_count=0,
                estimated_total_tokens=None,
                stream=False,
                request_data=request_payload,
                client_ip=get_client_real_ip_via_gateway(raw_request),
                api_key_id=access_ctx.api_key_id,
                protocol_resolver=_resolve_default_target_protocol,
                quota_error_builder=_build_openai_quota_exceeded_response,
                service_unavailable_builder=_build_openai_service_unavailable_response,
                request_label='音频请求',
            )
            if error_response is not None:
                return error_response
            assert next_attempt is not None
            proxy_context = AudioProxyContext(
                node_url=next_attempt.node_url,
                api_key=next_attempt.api_key,
                request_ctx=next_attempt.request_ctx,
                request_proxy_url=next_attempt.request_proxy_url,
                target_protocol=next_attempt.target_protocol,
            )
            continue

        if structured_payload is not None:
            proxy_context.request_ctx.response_data = orjson.dumps(structured_payload).decode('utf-8', errors='ignore')
            message, stack = _extract_backend_error(structured_payload)
            _apply_backend_error_info(proxy_context.request_ctx, message, stack)
            nodeproxy_service.post_call(proxy_context.node_url, proxy_context.request_ctx)
            return _build_backend_json_response(structured_payload)

        response_headers: dict[str, str] | None = None
        response_content: bytes
        if isinstance(response_payload, (bytes, bytearray)):
            response_content = bytes(response_payload)
            if response_filename is not None:
                proxy_context.request_ctx.response_data = f'<binary {len(response_content)} bytes>'
                response_headers = {
                    'Content-Disposition': f'attachment; filename="{response_filename}"',
                }
            else:
                decoded_text = response_content.decode('utf-8', errors='ignore')
                proxy_context.request_ctx.response_data = decoded_text
        else:
            response_content = str(response_payload).encode('utf-8', errors='ignore')
            proxy_context.request_ctx.response_data = str(response_payload)

        nodeproxy_service.post_call(proxy_context.node_url, proxy_context.request_ctx)
        return Response(
            content=response_content,
            media_type=response_media_type,
            headers=response_headers,
        )


@router.post('/audio/speech')
async def audio_speech_v1(
    request: AudioSpeechRequest,
    raw_request: Request = None,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Text-to-speech API compatible with OpenAI's specification."""
    request_dict = request.model_dump(exclude_none=True)
    response_media_type, response_filename = _resolve_speech_response_metadata(request_dict)
    return await _proxy_audio_request(
        raw_request=raw_request,
        nodeproxy_service=nodeproxy_service,
        access_ctx=access_ctx,
        model_name=request.model,
        model_type=ModelType.text_to_speech.value,
        backend_endpoint='/v1/audio/speech',
        request_action=RequestAction.audio_speech,
        request_log_payload=request_dict,
        response_media_type=response_media_type,
        response_filename=response_filename,
        backend_request_json=request_dict,
    )


@router.post('/audio/transcriptions')
async def audio_transcriptions_v1(
    raw_request: Request,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Speech-to-text transcription API compatible with OpenAI's multipart specification."""
    try:
        request_body, form_fields, file_summaries = await _parse_audio_multipart_request(raw_request)
    except (ValueError, MultiPartException) as exc:
        logger.warning('解析音频转写请求失败: {}', exc)
        return create_error_response(HTTPStatus.BAD_REQUEST, 'Invalid multipart audio transcription request.', error_type='invalid_request_error')

    model_name = _extract_first_form_value(form_fields, 'model')
    if model_name is None:
        return create_error_response(HTTPStatus.BAD_REQUEST, 'model is required', error_type='invalid_request_error')
    if not _has_uploaded_file(file_summaries, 'file'):
        return create_error_response(HTTPStatus.BAD_REQUEST, 'file is required', error_type='invalid_request_error')

    content_type = raw_request.headers.get('content-type')
    extra_headers = {'Content-Type': content_type} if content_type else None
    request_log_payload = {
        'fields': form_fields,
        'files': file_summaries,
    }
    return await _proxy_audio_request(
        raw_request=raw_request,
        nodeproxy_service=nodeproxy_service,
        access_ctx=access_ctx,
        model_name=model_name,
        model_type=ModelType.speech_to_text.value,
        backend_endpoint='/v1/audio/transcriptions',
        request_action=RequestAction.audio_transcriptions,
        request_log_payload=request_log_payload,
        response_media_type=_resolve_transcript_media_type(form_fields),
        request_content=request_body,
        extra_headers=extra_headers,
    )


@router.post('/audio/translations')
async def audio_translations_v1(
    raw_request: Request,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Speech translation API compatible with OpenAI's multipart specification."""
    try:
        request_body, form_fields, file_summaries = await _parse_audio_multipart_request(raw_request)
    except (ValueError, MultiPartException) as exc:
        logger.warning('解析音频翻译请求失败: {}', exc)
        return create_error_response(HTTPStatus.BAD_REQUEST, 'Invalid multipart audio translation request.', error_type='invalid_request_error')

    model_name = _extract_first_form_value(form_fields, 'model')
    if model_name is None:
        return create_error_response(HTTPStatus.BAD_REQUEST, 'model is required', error_type='invalid_request_error')
    if not _has_uploaded_file(file_summaries, 'file'):
        return create_error_response(HTTPStatus.BAD_REQUEST, 'file is required', error_type='invalid_request_error')

    content_type = raw_request.headers.get('content-type')
    extra_headers = {'Content-Type': content_type} if content_type else None
    request_log_payload = {
        'fields': form_fields,
        'files': file_summaries,
    }
    return await _proxy_audio_request(
        raw_request=raw_request,
        nodeproxy_service=nodeproxy_service,
        access_ctx=access_ctx,
        model_name=model_name,
        model_type=ModelType.speech_to_text.value,
        backend_endpoint='/v1/audio/translations',
        request_action=RequestAction.audio_translations,
        request_log_payload=request_log_payload,
        response_media_type=_resolve_transcript_media_type(form_fields),
        request_content=request_body,
        extra_headers=extra_headers,
    )