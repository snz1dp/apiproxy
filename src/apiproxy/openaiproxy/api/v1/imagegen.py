"""独立的文生图模型转发接口。

该接口仅做模型请求转发，使用 Provider Adapter 将请求转换为
下游原生格式，响应直接透传模型原始返回。与 OpenAI 兼容接口（/v1/images/*）完全独立。
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
from openaiproxy.services.imagegen.capability import ImageModelCapability
from openaiproxy.services.imagegen.registry import image_adapter_registry
import openaiproxy.services.imagegen.adapters  # noqa: F401 触发内置适配器注册
from openaiproxy.services.nodeproxy.exceptions import (
    NodeModelQuotaExceeded,
)
from openaiproxy.services.nodeproxy.service import NodeProxyService, create_error_response
from openaiproxy.utils.viagateway import get_client_real_ip_via_gateway

router = APIRouter(tags=["文生图转发接口"])


class ImageGenRequest(BaseModel):
    """文生图转发请求体。

    接收 Provider 原生格式的请求参数，由 Adapter 负责组装和转发。
    model 为必填字段，其余字段透传给 Adapter。
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

    接收原始请求体，通过 Adapter 组装为下游原生格式并转发，
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

    # 查找节点的 image_provider，获取对应 Adapter
    image_provider = nodeproxy_service.get_node_image_provider(node_url)
    adapter = image_adapter_registry.get(image_provider) if image_provider else None

    if adapter is not None:
        # 使用 Adapter 组装下游请求
        provider_request = adapter.build_request(
            request_dict,
            api_key='',  # 会在 generate 中由 attempt.api_key 覆盖
            node_url=node_url,
        )
        forward_endpoint = provider_request.endpoint
        forward_body = provider_request.json_body
        forward_headers = provider_request.headers or None
    else:
        # 无适配器时直接原样转发到节点地址，不添加额外路径后缀
        forward_endpoint = ''
        forward_body = request_dict
        forward_headers = None

    logger.debug('应用 {} 通过 imagegen 转发到节点 {}: {}', access_ctx.ownerapp_id, node_url, forward_endpoint or '(原样转发)')

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
        response = await nodeproxy_service.generate(
            forward_body,
            attempt.node_url,
            forward_endpoint,
            attempt.api_key,
            protocol_type=attempt.target_protocol,
            request_proxy_url=attempt.request_proxy_url,
            request_content=None,
            extra_headers=forward_headers,
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


@router.get('/imagegen/models')
async def list_imagegen_model_capabilities(
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """查询当前可用的文生图模型及其能力描述。

    返回所有当前用户可访问的 image_generation 类型模型的能力信息，
    包括支持的尺寸、是否支持参考图、风格列表等。
    """
    available_models = nodeproxy_service.get_available_image_models(
        effective_allowed_models=access_ctx.effective_allowed_models,
    )

    capabilities = []
    seen_models: set[str] = set()

    for model_info in available_models:
        model_name = model_info['model_name']
        if model_name in seen_models:
            continue
        seen_models.add(model_name)

        image_provider = model_info.get('image_provider')
        adapter = image_adapter_registry.get(image_provider) if image_provider else None

        if adapter is not None:
            capability = adapter.get_model_capability(model_name)
            if capability is not None:
                capabilities.append(capability.to_openai_dict())
                continue

        # 未知 Provider，返回通用能力描述
        generic_capability = ImageModelCapability(
            model_name=model_name,
            provider=image_provider or 'openai',
            supported_sizes=["1024x1024", "1792x1024", "1024x1792"],
            default_size="1024x1024",
            supports_reference_image=True,
            max_reference_images=1,
            n_range=(1, 1),
        )
        capabilities.append(generic_capability.to_openai_dict())

    return {"object": "list", "data": capabilities}
