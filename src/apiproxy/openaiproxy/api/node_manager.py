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

from typing import Annotated, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from openaiproxy.api.schemas import (
    OpenAINodeModelUpdate, OpenAINodeUpdate, PageResponse
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

from openaiproxy.services.nodemanager.service import NodeManager
from openaiproxy.services.nodemanager.schemas import Node, Status
from openaiproxy.services.deps import get_node_manager
from openaiproxy.api.utils import AsyncDbSession
from openaiproxy.logging import logger
from openaiproxy.utils.timezone import current_time_in_timezone

router = APIRouter(tags=["大模型节点管理"])

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
) -> PageResponse[OpenAINode]:
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

    return PageResponse[OpenAINode](
        offset=safe_offset,
        total=int(total),
        data=nodes,
    )

@router.post(
    '/nodes', dependencies=[Depends(check_api_key)],
    summary="创建OpenAI兼容服务节点"
)
async def create_openaiapi_node(
    input: OpenAINode,
    *,
    session: AsyncDbSession,
) -> OpenAINode:
    existed = await select_node_by_url(input.url, session=session)
    if existed:
        return existed
    if input.id:
        existed = await select_node_by_id(input.id, session=session)
        if existed:
            return existed
    else:
        input.id = uuid4()

    current_time = current_time_in_timezone()
    input.created_at = current_time
    input.updated_at = current_time
    input.enabled = True
    session.add(input)
    await session.commit()
    await session.refresh(input)
    return input

@router.post(
    '/nodes/query',
    dependencies=[Depends(check_api_key)],
    summary="通过URL查询OpenAI兼容服务节点"
)
async def query_openaiapi_node_by_url(
    url: str,
    *,
    session: AsyncDbSession,
) -> Optional[OpenAINode]:
    existed = await select_node_by_url(url, session=session)
    if not existed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="节点不存在"
        )
    return existed

@router.get(
    '/nodes/{node_id}',
    dependencies=[Depends(check_api_key)],
    summary="获取指定ID的OpenAI兼容服务节点"
)
async def get_openaiapi_node(
    node_id: UUID,
    *,
    session: AsyncDbSession,
) -> Optional[OpenAINode]:
    existed = await select_node_by_id(node_id, session=session)
    if not existed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="节点不存在"
        )
    return existed

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
) -> OpenAINode:
    existed = await select_node_by_id(node_id, session=session)
    if not existed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点不存在",
        )

    update_payload = update.model_dump(exclude_unset=True)
    if not update_payload:
        return existed

    for field, value in update_payload.items():
        setattr(existed, field, value)

    existed.updated_at = current_time_in_timezone()
    session.add(existed)
    await session.commit()
    await session.refresh(existed)
    return existed

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
        node_id=node_id,
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

# 以下部分为遗留接口先不动
@router.get('/nodes/status', dependencies=[Depends(check_api_key)], deprecated=True)
def node_status(node_manager: NodeManager = Depends(get_node_manager)):
    """Show nodes status."""
    try:
        return node_manager.status
    except:  # noqa
        return False

@router.post('/nodes/add', dependencies=[Depends(check_api_key)], deprecated=True)
async def add_node(
    node: Node,
    session: AsyncDbSession,
    raw_request: Request = None,
    node_manager: NodeManager = Depends(get_node_manager),
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
        if node_manager is not None:
            res = await run_in_threadpool(node_manager.add, node.url, node.status)
            if res is not None:
                logger.error(f'add node {node.url} failed, {res}')
                return res
    except Exception:  # noqa: BLE001
        logger.exception('Failed to add node via NodeManager')
        return 'Failed to add, please check the input url.'

    now = current_time_in_timezone()
    try:
        db_node = await select_node_by_url(node.url, session=session)
        if db_node:
            if status_payload.api_key is not None:
                db_node.api_key = status_payload.api_key
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
                api_key=status_payload.api_key,
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
        logger.info(f'add node {node.url} successfully')
        return 'Added successfully'
    except Exception:  # noqa: BLE001
        await session.rollback()
        logger.exception('Failed to persist node to database')
        return 'Failed to add, please check the input url.'

@router.post('/nodes/remove', dependencies=[Depends(check_api_key)], deprecated=True)
async def remove_node(
    node_url: str,
    session: AsyncDbSession,
    node_manager: NodeManager = Depends(get_node_manager),
):
    """Show available models."""
    try:
        if node_manager is not None:
            await run_in_threadpool(node_manager.remove, node_url)
    except Exception:  # noqa: BLE001
        logger.exception('Failed to remove node via NodeManager')
        return 'Failed to delete, please check the input url.'

    try:
        db_node = await select_node_by_url(node_url, session=session)
        if db_node:
            db_node.enabled = False
            db_node.updated_at = current_time_in_timezone()
            session.add(db_node)

            models = await select_node_models(node_id=db_node.id, session=session)
            for model in models:
                if model.enabled:
                    model.enabled = False
                    session.add(model)

        await session.commit()
        logger.info(f'delete node {node_url} successfully')
        return 'Deleted successfully'
    except Exception:  # noqa: BLE001
        await session.rollback()
        logger.exception('Failed to persist node removal')
        return 'Failed to delete, please check the input url.'

