# /*********************************************
#                    _ooOoo_
#                   o8888888o
#                   88" . "88
#                   (| -_- |)
#                   O\  =  /O
#                ____/`---'\____
#              .'  \\|     |//  `.
#             /  \\|||  :  |||//  \
#            /  _||||| -:- |||||-  \
#            |   | \\\  -  /// |   |
#            | \_|  ''\---/''  |   |
#            \  .-\__  `-`  ___/-. /
#          ___`. .'  /--.--\  `. . __
#       ."" '<  `.___\_<|>_/___.'  >'"".
#      | | :  `- \`.;`\ _ /`;.`/ - ` : | |
#      \  \ `-.   \_ __\ /__ _/   .-` /  /
# ======`-.____`-.___\_____/___.-`____.-'======
#                    `=---='

# ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#            佛祖保佑       永无BUG
#            心外无法       法外无心
#            三宝弟子       三德子宏愿
# *********************************************/

from http import HTTPStatus
from typing import Any, Optional
import orjson
import traceback

from fastapi import APIRouter, Depends, Request
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.formparsers import MultiPartException

from openaiproxy.api.schemas import ImageGenerationRequest
from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.api.v1.completions import _build_backend_json_response
from openaiproxy.api.v1.embeddings import _apply_backend_error_info, _extract_backend_error
from openaiproxy.logging import logger
from openaiproxy.services.database.models.node.model import ModelType, ProtocolType
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.services.deps import get_node_proxy_service
from openaiproxy.services.nodeproxy.exceptions import (
    ApiKeyQuotaExceeded,
    AppQuotaExceeded,
    NodeModelQuotaExceeded,
    NorthboundQuotaProcessingError,
)
from openaiproxy.services.nodeproxy.service import NodeProxyService, create_error_response
from openaiproxy.utils.viagateway import get_client_real_ip_via_gateway

router = APIRouter(tags=["OpenAI兼容接口"])


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


async def _parse_image_multipart_request(
    raw_request: Request,
) -> tuple[bytes, dict[str, Any], list[dict[str, Optional[str]]]]:
    """Parse multipart image requests while preserving the original body bytes."""
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


async def _proxy_image_request(
    *,
    raw_request: Request,
    nodeproxy_service: NodeProxyService,
    access_ctx: AccessKeyContext,
    model_name: str,
    backend_endpoint: str,
    request_action: RequestAction,
    request_log_payload: dict[str, Any],
    backend_request_json: Optional[dict[str, Any]] = None,
    request_content: Optional[bytes] = None,
    extra_headers: Optional[dict[str, str]] = None,
):
    """Proxy an image request to the selected backend node and finalize bookkeeping."""
    model_type = ModelType.image_generation.value
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

    logger.debug('应用 {} 将图片请求转发到节点 {}: {}', access_ctx.ownerapp_id, node_url, backend_endpoint)

    request_payload = orjson.dumps(request_log_payload).decode('utf-8', errors='ignore')
    client_ip = get_client_real_ip_via_gateway(raw_request)
    try:
        request_ctx = nodeproxy_service.pre_call(
            node_url,
            model_name=model_name,
            model_type=model_type,
            request_protocol=ProtocolType.openai,
            ownerapp_id=access_ctx.ownerapp_id,
            request_action=request_action,
            request_count=0,
            estimated_total_tokens=None,
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
    target_protocol = getattr(node_status, 'protocol_type', ProtocolType.openai) if node_status is not None else ProtocolType.openai
    request_proxy_url = getattr(node_status, 'request_proxy_url', None) if node_status is not None else None

    response = await nodeproxy_service.generate(
        backend_request_json,
        node_url,
        backend_endpoint,
        api_key,
        protocol_type=target_protocol,
        request_proxy_url=request_proxy_url,
        request_content=request_content,
        extra_headers=extra_headers,
    )
    request_ctx.response_data = response

    try:
        payload = orjson.loads(response)
    except Exception:  # noqa: BLE001
        error_message = f'Failed to decode backend image response: {response!r}'
        stack = traceback.format_exc()
        _apply_backend_error_info(request_ctx, error_message, stack)
        nodeproxy_service.post_call(node_url, request_ctx)
        raise

    message, stack = _extract_backend_error(payload)
    _apply_backend_error_info(request_ctx, message, stack)

    nodeproxy_service.post_call(node_url, request_ctx)
    return _build_backend_json_response(payload)


@router.post('/images/generations')
async def image_generations_v1(
    request: ImageGenerationRequest,
    raw_request: Request = None,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Image generation API compatible with OpenAI's specification."""
    request_dict = request.model_dump(exclude_none=True)
    return await _proxy_image_request(
        raw_request=raw_request,
        nodeproxy_service=nodeproxy_service,
        access_ctx=access_ctx,
        model_name=request.model,
        backend_endpoint='/v1/images/generations',
        request_action=RequestAction.images_generations,
        request_log_payload=request_dict,
        backend_request_json=request_dict,
    )


@router.post('/images/edits')
async def image_edits_v1(
    raw_request: Request,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Image edit API compatible with OpenAI's multipart specification."""
    try:
        request_body, form_fields, file_summaries = await _parse_image_multipart_request(raw_request)
    except (ValueError, MultiPartException) as exc:
        logger.warning('解析图片编辑请求失败: {}', exc)
        return create_error_response(HTTPStatus.BAD_REQUEST, 'Invalid multipart image edit request.', error_type='invalid_request_error')

    model_name = _extract_first_form_value(form_fields, 'model')
    if model_name is None:
        return create_error_response(HTTPStatus.BAD_REQUEST, 'model is required', error_type='invalid_request_error')
    prompt = _extract_first_form_value(form_fields, 'prompt')
    if prompt is None:
        return create_error_response(HTTPStatus.BAD_REQUEST, 'prompt is required', error_type='invalid_request_error')
    if not _has_uploaded_file(file_summaries, 'image'):
        return create_error_response(HTTPStatus.BAD_REQUEST, 'image is required', error_type='invalid_request_error')

    content_type = raw_request.headers.get('content-type')
    extra_headers = {'Content-Type': content_type} if content_type else None
    request_log_payload = {
        'fields': form_fields,
        'files': file_summaries,
    }
    return await _proxy_image_request(
        raw_request=raw_request,
        nodeproxy_service=nodeproxy_service,
        access_ctx=access_ctx,
        model_name=model_name,
        backend_endpoint='/v1/images/edits',
        request_action=RequestAction.images_edits,
        request_log_payload=request_log_payload,
        request_content=request_body,
        extra_headers=extra_headers,
    )


@router.post('/images/variations')
async def image_variations_v1(
    raw_request: Request,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Image variation API compatible with OpenAI's multipart specification."""
    try:
        request_body, form_fields, file_summaries = await _parse_image_multipart_request(raw_request)
    except (ValueError, MultiPartException) as exc:
        logger.warning('解析图片变体请求失败: {}', exc)
        return create_error_response(HTTPStatus.BAD_REQUEST, 'Invalid multipart image variation request.', error_type='invalid_request_error')

    model_name = _extract_first_form_value(form_fields, 'model')
    if model_name is None:
        return create_error_response(HTTPStatus.BAD_REQUEST, 'model is required', error_type='invalid_request_error')
    if not _has_uploaded_file(file_summaries, 'image'):
        return create_error_response(HTTPStatus.BAD_REQUEST, 'image is required', error_type='invalid_request_error')

    content_type = raw_request.headers.get('content-type')
    extra_headers = {'Content-Type': content_type} if content_type else None
    request_log_payload = {
        'fields': form_fields,
        'files': file_summaries,
    }
    return await _proxy_image_request(
        raw_request=raw_request,
        nodeproxy_service=nodeproxy_service,
        access_ctx=access_ctx,
        model_name=model_name,
        backend_endpoint='/v1/images/variations',
        request_action=RequestAction.images_variations,
        request_log_payload=request_log_payload,
        request_content=request_body,
        extra_headers=extra_headers,
    )