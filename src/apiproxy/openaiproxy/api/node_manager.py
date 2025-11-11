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

from typing import Annotated, Any, Optional
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from openaiproxy.api.schemas import (
    CreateOpenAINode, OpenAINodeModelUpdate, OpenAINodeReponse, OpenAINodeUpdate, PageResponse
)
from openaiproxy.api.utils import check_api_key
from openaiproxy.services.database.models import (
    Node as OpenAINode, NodeModel as OpenAINodeModel
)
from openaiproxy.services.database.models.node.crud import (
    select_node_by_id, select_node_by_url,
    select_nodes, count_nodes,
    select_node_model_by_id, select_node_model_by_unique,
    select_node_models, count_node_models
)
from openaiproxy.services.database.models.node.model import ModelType

from openaiproxy.services.nodeproxy.service import NodeProxyService
from openaiproxy.services.nodeproxy.schemas import Node, Status
from openaiproxy.services.deps import get_node_proxy_service
from openaiproxy.api.utils import AsyncDbSession
from openaiproxy.logging import logger
from openaiproxy.utils.timezone import current_time_in_timezone
from openaiproxy.utils.apikey import (
    ApiKeyEncryptionError,
    decrypt_api_key,
    encrypt_api_key,
)

MODELS_ENDPOINT = '/v1/models'
MODELS_VERIFY_TIMEOUT = httpx.Timeout(5.0, connect=5.0, read=10.0)

router = APIRouter(tags=["大模型节点管理"])


def _normalize_api_key(api_key: Optional[str]) -> Optional[str]:
    if api_key is None:
        return None
    stripped = api_key.strip()
    return stripped or None


def _encrypt_node_api_key(api_key: Optional[str], *, context: str | None = None) -> Optional[str]:
    normalized = _normalize_api_key(api_key)
    if normalized is None:
        return None
    try:
        return encrypt_api_key(normalized)
    except ApiKeyEncryptionError as exc:  # noqa: BLE001
        label = context or 'unknown'
        logger.exception(f'节点 {label} API密钥加密失败')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='节点API密钥加密失败',
        ) from exc


def _decrypt_node_api_key(
    api_key: Optional[str],
    *,
    context: str | None = None,
    raise_on_error: bool = False,
) -> Optional[str]:
    if api_key is None:
        return None
    try:
        return decrypt_api_key(api_key)
    except ApiKeyEncryptionError as exc:  # noqa: BLE001
        label = context or 'unknown'
        if raise_on_error:
            logger.error(f'节点 {label} API密钥解密失败')
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='节点API密钥不可用',
            ) from exc
        logger.warning(f'节点 {label} API密钥解密失败，已忽略')
        return raise_on_error


def _clone_node_with_plain_api_key(node: OpenAINode) -> OpenAINodeReponse:
    node_payload = node.model_dump()
    return OpenAINodeReponse.model_validate(node_payload)

async def _verify_models_endpoint(node_url: str, api_key: Optional[str]) -> None:
    """Probe the node's `/v1/models` endpoint when verification is requested."""
    await _request_node_models_payload(
        node_url,
        api_key,
        error_status=status.HTTP_400_BAD_REQUEST,
        error_prefix='节点验证失败',
    )


async def _request_node_models_payload(
    node_url: str,
    api_key: Optional[str],
    *,
    error_status: int,
    error_prefix: str,
) -> dict[str, Any]:
    if not node_url:
        raise HTTPException(
            status_code=error_status,
            detail=f'{error_prefix}，节点地址不能为空',
        )

    headers = {'Authorization': f'Bearer {api_key}'} if api_key else None
    models_url = f"{node_url.rstrip('/')}{MODELS_ENDPOINT}"

    try:
        async with httpx.AsyncClient(timeout=MODELS_VERIFY_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(models_url, headers=headers)
    except httpx.HTTPError as exc:  # noqa: BLE001 - propagated as HTTPException
        logger.warning('节点 {} 请求/v1/models失败: {}', node_url, exc)
        raise HTTPException(
            status_code=error_status,
            detail=f'{error_prefix}，无法访问/v1/models接口',
        ) from exc

    if response.status_code != status.HTTP_200_OK:
        logger.warning(
            '节点 {} 请求/v1/models失败，状态码: {}',
            node_url,
            response.status_code,
        )
        raise HTTPException(
            status_code=error_status,
            detail=f'{error_prefix}，/v1/models返回状态码{response.status_code}',
        )

    try:
        payload = response.json()
    except ValueError as exc:  # noqa: BLE001 - propagated as HTTPException
        logger.warning('节点 {} 请求/v1/models失败，响应不是有效JSON', node_url)
        raise HTTPException(
            status_code=error_status,
            detail=f'{error_prefix}，/v1/models返回非JSON响应',
        ) from exc
    if not isinstance(payload, dict):
        logger.warning('节点 {} 请求/v1/models失败，响应格式不是对象', node_url)
        raise HTTPException(
            status_code=error_status,
            detail=f'{error_prefix}，/v1/models返回格式异常',
        )
    return payload

# 以下部分为遗留接口先不动
@router.get('/nodes/status', dependencies=[Depends(check_api_key)], deprecated=True)
def node_status(nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service)):
    """Show nodes status."""
    try:
        raw_status = nodeproxy_service.status
    except:  # noqa
        return False
    sanitized_status: dict[str, Any] = {}
    for node_url, status in raw_status.items():
        if isinstance(status, Status):
            sanitized_status[node_url] = status.model_copy(
                deep=True,
                update={'api_key': None},
            )
        elif isinstance(status, dict):
            status_copy = dict(status)
            status_copy.pop('api_key', None)
            sanitized_status[node_url] = status_copy
        else:
            sanitized_status[node_url] = status
    return sanitized_status

@router.post('/nodes/add', dependencies=[Depends(check_api_key)], deprecated=True)
async def add_node(
    node: Node,
    session: AsyncDbSession,
    raw_request: Request = None,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
):
    """Add a node to the manager.

    - url (str): A http url. Can be the url generated by
        `lmdeploy serve api_server`.
    - status (Dict): The description of the node. An example:
        {models: ['internlm-chat-7b],  speed: 1}. The speed here can be
        RPM or other metric. All the values of nodes should be the same metric.
    """
    status_payload = node.status or Status()

    def resolve_model_type(value: str | None) -> ModelType:
        try:
            return ModelType(value) if value is not None else ModelType.chat
        except ValueError:
            return ModelType.chat

    try:
        if nodeproxy_service is not None:
            res = await run_in_threadpool(nodeproxy_service.add, node.url, node.status)
            if res is not None:
                logger.error(f'节点 {node.url} 添加失败，原因: {res}')
                return res
    except Exception:  # noqa: BLE001
        logger.exception('通过节点代理服务添加节点时发生异常')
        return 'Failed to add, please check the input url.'

    now = current_time_in_timezone()
    try:
        db_node = await select_node_by_url(node.url, session=session)
        try:
            encrypted_api_key = _encrypt_node_api_key(status_payload.api_key, context=node.url)
        except HTTPException:
            return 'Failed to add, please check the input url.'
        if db_node:
            if status_payload.api_key is not None:
                db_node.api_key = encrypted_api_key
            if status_payload.health_check is not None:
                db_node.health_check = status_payload.health_check
            if status_payload.avaiaible is not None:
                db_node.enabled = bool(status_payload.avaiaible)
            if status_payload.type and not db_node.name:
                db_node.name = status_payload.type
            db_node.updated_at = now
            session.add(db_node)
        else:
            db_node = OpenAINode(
                url=node.url,
                name=status_payload.type,
                api_key=encrypted_api_key,
                health_check=status_payload.health_check if status_payload.health_check is not None else True,
                enabled=bool(status_payload.avaiaible) if status_payload.avaiaible is not None else True,
                created_at=now,
                updated_at=now,
            )
            session.add(db_node)
            await session.flush()

        models = status_payload.models or []
        if models:
            model_type = resolve_model_type(status_payload.type)
            seen_models: set[str] = set()
            for model_name in models:
                if not model_name or model_name in seen_models:
                    continue
                seen_models.add(model_name)
                existed_model = await select_node_model_by_unique(
                    node_id=db_node.id,
                    model_name=model_name,
                    model_type=model_type,
                    session=session,
                )
                if existed_model:
                    if status_payload.avaiaible is not None:
                        existed_model.enabled = bool(status_payload.avaiaible)
                        session.add(existed_model)
                    continue
                session.add(OpenAINodeModel(
                    node_id=db_node.id,
                    model_name=model_name,
                    model_type=model_type,
                    enabled=bool(status_payload.avaiaible) if status_payload.avaiaible is not None else True,
                ))

        await session.commit()
        logger.info(f'节点 {node.url} 添加成功')
        return 'Added successfully'
    except Exception:  # noqa: BLE001
        await session.rollback()
        logger.exception('保存节点信息到数据库失败')
        return 'Failed to add, please check the input url.'

@router.post('/nodes/remove', dependencies=[Depends(check_api_key)], deprecated=True)
async def remove_node(
    node_url: str,
    session: AsyncDbSession,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
):
    """Show available models."""
    try:
        if nodeproxy_service is not None:
            await run_in_threadpool(nodeproxy_service.remove, node_url)
    except Exception:  # noqa: BLE001
        logger.exception('通过节点代理服务移除节点时发生异常')
        return 'Failed to delete, please check the input url.'

    try:
        db_node = await select_node_by_url(node_url, session=session)
        if db_node:
            db_node.enabled = False
            db_node.updated_at = current_time_in_timezone()
            session.add(db_node)

            models = await select_node_models(node_ids=db_node.id, session=session)
            for model in models:
                if model.enabled:
                    model.enabled = False
                    session.add(model)

        await session.commit()
        logger.info(f'节点 {node_url} 删除成功')
        return 'Deleted successfully'
    except Exception:  # noqa: BLE001
        await session.rollback()
        logger.exception('保存节点删除信息失败')
        return 'Failed to delete, please check the input url.'

# 以下部分为新的节点管理接口

@router.get(
    '/nodes',
    dependencies=[Depends(check_api_key)],
    summary="获取OpenAI兼容服务节点"
)
async def get_openaiapi_nodes(
    enabled: Optional[bool] = None,
    orderby: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[OpenAINodeReponse]:
    """获取OpenAI兼容服务节点"""
    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    nodes = await select_nodes(
        enabled=enabled,
        orderby=orderby,
        offset=safe_offset,
        limit=safe_limit,
        session=session,
    )
    raw_total = await count_nodes(enabled=enabled, session=session)
    total = raw_total if isinstance(raw_total, int) else raw_total[0]

    response_nodes = [_clone_node_with_plain_api_key(node) for node in nodes]

    return PageResponse[OpenAINode](
        offset=safe_offset,
        total=int(total),
        data=response_nodes,
    )

@router.post(
    '/nodes', dependencies=[Depends(check_api_key)],
    summary="创建OpenAI兼容服务节点"
)
async def create_openaiapi_node(
    input: CreateOpenAINode,
    *,
    session: AsyncDbSession,
) -> OpenAINodeReponse:
    existed = await select_node_by_url(input.url, session=session)
    if existed:
        return OpenAINodeReponse.model_validate(existed, from_attributes=True)
    if input.id:
        existed = await select_node_by_id(input.id, session=session)
        if existed:
            return OpenAINodeReponse.model_validate(existed, from_attributes=True)
    else:
        input.id = uuid4()

    if input.verify is not False:
        await _verify_models_endpoint(input.url, input.api_key)

    current_time = current_time_in_timezone()
    input.created_at = current_time
    input.updated_at = current_time
    input.enabled = True
    node_payload = input.model_dump(exclude={'verify'})
    node_payload['api_key'] = _encrypt_node_api_key(
        input.api_key,
        context=input.url,
    )
    node = OpenAINode.model_validate(node_payload)
    session.add(node)
    await session.commit()
    await session.refresh(node)
    return OpenAINodeReponse.model_validate(node, from_attributes=True)

@router.post(
    '/nodes/query',
    dependencies=[Depends(check_api_key)],
    summary="通过URL查询OpenAI兼容服务节点"
)
async def query_openaiapi_node_by_url(
    url: str = Form(None),
    *,
    session: AsyncDbSession,
) -> Optional[OpenAINodeReponse]:
    existed = await select_node_by_url(url, session=session)
    if not existed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="节点不存在"
        )
    return _clone_node_with_plain_api_key(existed)

@router.post(
    '/nodes/models',
    dependencies=[Depends(check_api_key)],
    summary="查询OpenAI兼容节点的模型"
)
async def fetch_openaiapi_node_models(
    node_id: UUID | None = Form(None),
    url: Optional[str] = Form(None),
    api_key: Optional[str] = Form(None),
    *,
    session: AsyncDbSession,
):
    node_url = url
    node_api_key = api_key

    if node_id is not None:
        node = await select_node_by_id(node_id, session=session)
        if not node:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="节点不存在",
            )
        node_url = node.url
        if node_api_key is None:
            node_api_key = _decrypt_node_api_key(
                node.api_key,
                context=node.url or str(node_id),
                raise_on_error=False,
            )

    if not node_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请提供节点ID或节点URL",
        )

    payload = await _request_node_models_payload(
        node_url,
        node_api_key,
        error_status=status.HTTP_502_BAD_GATEWAY,
        error_prefix='获取节点模型失败',
    )

    models = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(models, list):
        logger.warning('节点 {} /v1/models 响应缺少有效的 data 列表', node_url)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='获取节点模型失败，返回数据格式异常',
        )

    return payload

@router.get(
    '/nodes/{node_id}',
    dependencies=[Depends(check_api_key)],
    summary="获取指定ID的OpenAI兼容服务节点"
)
async def get_openaiapi_node(
    node_id: UUID,
    *,
    session: AsyncDbSession,
) -> Optional[OpenAINodeReponse]:
    existed = await select_node_by_id(node_id, session=session)
    if not existed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="节点不存在"
        )
    return _clone_node_with_plain_api_key(existed)

@router.post(
    '/nodes/{node_id}',
    dependencies=[Depends(check_api_key)],
    summary="更新OpenAI兼容服务节点"
)
async def update_openaiapi_node(
    node_id: UUID,
    update: OpenAINodeUpdate,
    *,
    session: AsyncDbSession,
) -> OpenAINodeReponse:
    existed = await select_node_by_id(node_id, session=session)
    if not existed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点不存在",
        )

    update_payload = update.model_dump(exclude_unset=True)
    verify_flag = update_payload.pop('verify', update.verify)
    if not update_payload:
        return _clone_node_with_plain_api_key(existed)

    if 'api_key' in update_payload:
        normalized_api_key = _normalize_api_key(update_payload['api_key'])
        if normalized_api_key is not None and verify_flag is not False:
            await _verify_models_endpoint(existed.url, normalized_api_key)
        update_payload['api_key'] = _encrypt_node_api_key(
            normalized_api_key,
            context=existed.url or str(node_id),
        )

    for field, value in update_payload.items():
        setattr(existed, field, value)

    existed.updated_at = current_time_in_timezone()
    session.add(existed)
    await session.commit()
    await session.refresh(existed)
    return _clone_node_with_plain_api_key(existed)

@router.delete(
    '/nodes/{node_id}',
    dependencies=[Depends(check_api_key)],
    summary="删除OpenAI兼容服务节点"
)
async def delete_openaiapi_node(
    node_id: UUID,
    *,
    session: AsyncDbSession,
):
    existed = await select_node_by_id(node_id, session=session)
    if existed:
        if existed.enabled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="请先禁用节点后再删除",
            )
        await session.delete(existed)
        await session.commit()
    return {
        "code": 0,
        "message": "删除成功",
    }


@router.get(
    '/nodes/{node_id}/models',
    dependencies=[Depends(check_api_key)],
    summary="获取OpenAI兼容服务节点模型"
)
async def get_openaiapi_node_models(
    node_id: UUID,
    model_type: Optional[ModelType] = None,
    enabled: Optional[bool] = None,
    orderby: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[OpenAINodeModel]:
    """获取节点模型列表"""
    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    node = await select_node_by_id(node_id, session=session)
    if not node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点不存在",
        )

    models = await select_node_models(
        node_ids=node_id,
        model_type=model_type,
        enabled=enabled,
        orderby=orderby,
        offset=safe_offset,
        limit=safe_limit,
        session=session,
    )
    raw_total = await count_node_models(
        node_id=node_id,
        model_type=model_type,
        enabled=enabled,
        session=session,
    )
    total = raw_total if isinstance(raw_total, int) else raw_total[0]

    return PageResponse[OpenAINodeModel](
        offset=safe_offset,
        total=int(total),
        data=models,
    )


@router.post(
    '/nodes/{node_id}/models', dependencies=[Depends(check_api_key)],
    summary="创建OpenAI兼容服务节点模型"
)
async def create_openaiapi_node_model(
    node_id: UUID,
    input: OpenAINodeModel,
    *,
    session: AsyncDbSession,
) -> OpenAINodeModel:
    if input.node_id and input.node_id != node_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="节点ID与路径参数不一致",
        )

    if not input.model_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="模型名称不能为空",
        )

    if input.model_type is None:
        input.model_type = ModelType.chat

    existed_node = await select_node_by_id(node_id, session=session)
    if not existed_node:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="节点不存在",
        )

    input.node_id = node_id

    existed_model = await select_node_model_by_unique(
        node_id=node_id,
        model_name=input.model_name,
        model_type=input.model_type,
        session=session,
    )
    if existed_model:
        return existed_model

    if input.id:
        existed = await select_node_model_by_id(input.id, session=session)
        if existed:
            return existed
    else:
        input.id = uuid4()

    session.add(input)
    await session.commit()
    await session.refresh(input)
    return input


@router.post(
    '/nodes/{node_id}/models/query',
    dependencies=[Depends(check_api_key)],
    summary="通过节点与名称查询OpenAI兼容服务节点模型"
)
async def query_openaiapi_node_model(
    node_id: UUID,
    model_name: str,
    model_type: Optional[ModelType] = ModelType.chat,
    *,
    session: AsyncDbSession,
) -> OpenAINodeModel:
    node = await select_node_by_id(node_id, session=session)
    if not node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点不存在",
        )

    if model_type is None:
        model_type = ModelType.chat

    existed = await select_node_model_by_unique(
        node_id=node_id,
        model_name=model_name,
        model_type=model_type,
        session=session,
    )
    if not existed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点模型不存在",
        )
    return existed


@router.get(
    '/nodes/{node_id}/models/{model_id}',
    dependencies=[Depends(check_api_key)],
    summary="获取指定ID的OpenAI兼容服务节点模型"
)
async def get_openaiapi_node_model(
    node_id: UUID,
    model_id: UUID,
    *,
    session: AsyncDbSession,
) -> Optional[OpenAINodeModel]:
    node = await select_node_by_id(node_id, session=session)
    if not node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点不存在",
        )

    existed = await select_node_model_by_id(model_id, session=session)
    if not existed or existed.node_id != node_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点模型不存在",
        )
    return existed


@router.post(
    '/nodes/{node_id}/models/{model_id}',
    dependencies=[Depends(check_api_key)],
    summary="更新OpenAI兼容服务节点模型"
)
async def update_openaiapi_node_model(
    node_id: UUID,
    model_id: UUID,
    update: OpenAINodeModelUpdate,
    *,
    session: AsyncDbSession,
) -> OpenAINodeModel:
    existed_node = await select_node_by_id(node_id, session=session)
    if not existed_node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点不存在",
        )

    existed = await select_node_model_by_id(model_id, session=session)
    if not existed or existed.node_id != node_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点模型不存在",
        )

    update_payload = update.model_dump(exclude_unset=True)
    if not update_payload:
        return existed

    for field, value in update_payload.items():
        setattr(existed, field, value)

    session.add(existed)
    await session.commit()
    await session.refresh(existed)
    return existed

@router.delete(
    '/nodes/{node_id}/models/{model_id}',
    dependencies=[Depends(check_api_key)],
    summary="删除OpenAI兼容服务节点模型"
)
async def delete_openaiapi_node_model(
    node_id: UUID,
    model_id: UUID,
    *,
    session: AsyncDbSession,
):
    node = await select_node_by_id(node_id, session=session)
    if not node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点不存在",
        )

    existed = await select_node_model_by_id(model_id, session=session)
    if existed and existed.node_id == node_id:
        if existed.enabled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="请先禁用节点模型后再删除",
            )
        await session.delete(existed)
        await session.commit()
    return {
        "code": 0,
        "message": "删除成功",
    }
