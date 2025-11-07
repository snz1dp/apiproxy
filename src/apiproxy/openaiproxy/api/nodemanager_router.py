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
from openaiproxy.api.schemas import OpenAINodeUpdate, PageResponse
from openaiproxy.api.utils import check_api_key
from openaiproxy.services.database.models import Node as OpenAINode
from openaiproxy.services.database.models.node.crud import (
    select_node_by_id, select_node_by_url,
    select_nodes, count_nodes
)

from openaiproxy.services.nodemanager.service import NodeManager
from openaiproxy.services.nodemanager.schemas import Node
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
    '/nodes/query_by_url',
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

# 以下部分为遗留接口先不动
@router.get('/nodes/status', dependencies=[Depends(check_api_key)], deprecated=True)
def node_status(node_manager: NodeManager = Depends(get_node_manager)):
    """Show nodes status."""
    try:
        return node_manager.status
    except:  # noqa
        return False

@router.post('/nodes/add', dependencies=[Depends(check_api_key)], deprecated=True)
def add_node(
    node: Node,
    raw_request: Request = None,
    node_manager: NodeManager = Depends(get_node_manager)
):
    """Add a node to the manager.

    - url (str): A http url. Can be the url generated by
        `lmdeploy serve api_server`.
    - status (Dict): The description of the node. An example:
        {models: ['internlm-chat-7b],  speed: 1}. The speed here can be
        RPM or other metric. All the values of nodes should be the same metric.
    """
    try:
        res = node_manager.add(node.url, node.status)
        if res is not None:
            logger.error(f'add node {node.url} failed, {res}')
            return res
        logger.info(f'add node {node.url} successfully')
        return 'Added successfully'
    except:  # noqa
        return 'Failed to add, please check the input url.'


@router.post('/nodes/remove', dependencies=[Depends(check_api_key)], deprecated=True)
def remove_node(node_url: str, node_manager: NodeManager = Depends(get_node_manager)):
    """Show available models."""
    try:
        node_manager.remove(node_url)
        logger.info(f'delete node {node_url} successfully')
        return 'Deleted successfully'
    except:  # noqa
        logger.error(f'delete node {node_url} failed.')
        return 'Failed to delete, please check the input url.'

