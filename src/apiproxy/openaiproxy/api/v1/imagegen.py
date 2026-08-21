"""独立的文生图模型转发接口。

该接口仅做模型请求原样转发，响应直接透传模型原始返回。
与 OpenAI 兼容接口（/v1/images/*）完全独立。
"""

from http import HTTPStatus
import orjson
import traceback

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.api.v1.completions import (
    _build_backend_json_response,
    _build_openai_quota_exceeded_response,
    _build_openai_service_unavailable_response,
    _prepare_proxy_attempt,
    _resolve_default_target_protocol,
    _retry_proxy_attempt_after_capacity_exhausted,
)
from openaiproxy.api.v1.embeddings import _apply_backend_error_info
from openaiproxy.logging import logger
from openaiproxy.services.database.models.node.model import ModelType, ProtocolType
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.services.deps import get_node_proxy_service
from openaiproxy.services.nodeproxy.exceptions import (
    NodeModelQuotaExceeded,
)
from openaiproxy.services.nodeproxy.service import NodeProxyService, create_error_response
from openaiproxy.utils.viagateway import get_client_real_ip_via_gateway

router = APIRouter(tags=["文生图转发接口"])


class ImageGenRequest(BaseModel):
    """文生图转发请求体。

    接收任意格式的请求参数，原样转发到下游节点。
    model 为必填字段，其余字段透传。
    """

    model_config = ConfigDict(extra='allow')

    model: str
    """模型名称，用于路由到对应节点"""


@router.post('/imagegen/generate')
async def imagegen_generate(
    request: ImageGenRequest,
    raw_request: Request,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """文生图模型转发接口。

    接收原始请求体，原样转发到下游节点，
    响应直接透传模型原始返回，不做格式转换。
    """
    request_dict = request.model_dump(exclude_none=True)
    model_name = request.model

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

    logger.debug('应用 {} 通过 imagegen 原样转发到节点 {}', access_ctx.ownerapp_id, node_url)

    request_payload = orjson.dumps(request_dict).decode('utf-8', errors='ignore')
    client_ip = get_client_real_ip_via_gateway(raw_request)

    error_response, attempt = _prepare_proxy_attempt(
        nodeproxy_service=nodeproxy_service,
        node_url=node_url,
        model_name=model_name,
        model_type=model_type,
        request_protocol=ProtocolType.openai,
        ownerapp_id=access_ctx.ownerapp_id,
        request_action=RequestAction.images_generations,
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

    attempted_node_urls: set[str] = set()
    while True:
        # 原样转发：不添加额外路径后缀，请求体不变
        response = await nodeproxy_service.generate(
            request_dict,
            attempt.node_url,
            '',  # 空 endpoint，直接请求节点地址
            attempt.api_key,
            protocol_type=attempt.target_protocol,
            request_proxy_url=attempt.request_proxy_url,
            request_content=None,
            extra_headers=None,
        )

        try:
            payload = orjson.loads(response)
        except Exception:  # noqa: BLE001
            error_message = f'Failed to decode backend imagegen response: {response!r}'
            stack = traceback.format_exc()
            _apply_backend_error_info(attempt.request_ctx, error_message, stack)
            nodeproxy_service.post_call(attempt.node_url, attempt.request_ctx)
            raise

        if NodeProxyService.is_backend_capacity_exhausted_error(payload):
            error_response, next_attempt = _retry_proxy_attempt_after_capacity_exhausted(
                nodeproxy_service=nodeproxy_service,
                current_attempt=attempt,
                payload=payload,
                attempted_node_urls=attempted_node_urls,
                model_name=model_name,
                model_type=model_type,
                request_protocol=ProtocolType.openai,
                allow_cross_protocol=False,
                ownerapp_id=access_ctx.ownerapp_id,
                request_action=RequestAction.images_generations,
                request_count=0,
                estimated_total_tokens=None,
                stream=False,
                request_data=request_payload,
                client_ip=client_ip,
                api_key_id=access_ctx.api_key_id,
                protocol_resolver=_resolve_default_target_protocol,
                quota_error_builder=_build_openai_quota_exceeded_response,
                service_unavailable_builder=_build_openai_service_unavailable_response,
                request_label='文生图转发请求',
            )
            if error_response is not None:
                return error_response
            assert next_attempt is not None
            attempt = next_attempt
            continue

        attempt.request_ctx.response_data = response

        # 检查后端响应是否为错误（含 error 字段）
        if isinstance(payload, dict) and 'error' in payload:
            error_info = payload['error']
            message = error_info.get('message', 'Backend request failed') if isinstance(error_info, dict) else str(error_info)
            _apply_backend_error_info(attempt.request_ctx, message, None)
            nodeproxy_service.post_call(attempt.node_url, attempt.request_ctx)
            return _build_backend_json_response(payload)

        # 直接透传模型原始响应，不做格式转换
        _apply_backend_error_info(attempt.request_ctx, None, None)
        nodeproxy_service.post_call(attempt.node_url, attempt.request_ctx)
        return _build_backend_json_response(payload)
