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

from typing import List
from uuid import UUID
from sqlmodel import func, select
from openaiproxy.utils.sqlalchemy import parse_orderby_column
from sqlmodel.ext.asyncio.session import AsyncSession
from openaiproxy.services.database.models.node.model import ModelType, Node, NodeModel


def _coerce_model_type(model_type: ModelType | str) -> str:
    """转换模型类型为数据库存储值"""
    return model_type.value if isinstance(model_type, ModelType) else str(model_type)

async def select_node_by_url(
    url: str,
    *,
    session: AsyncSession
) -> Node | None:
    """通过 URL 查询节点"""
    smts = select(Node).where(Node.url == url)
    result = await session.exec(smts)
    return result.first()

async def select_node_by_id(
    id: str | UUID,
    *,
    session: AsyncSession
) -> Node | None:
    """通过 ID 查询节点"""
    id = UUID(str(id)) if not isinstance(id, UUID) else id
    smts = select(Node).where(Node.id == id)
    result = await session.exec(smts)
    return result.first()

async def select_nodes(
    enabled: bool | None = None,
    orderby: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    *,
    session: AsyncSession
) -> List[Node]:
    """查询所有节点"""
    smts = select(Node)
    if enabled is not None:
        smts = smts.where(Node.enabled == True)  # noqa: E712

    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)

    smts = smts.order_by(parse_orderby_column(
        Node, orderby, Node.created_at.asc()
    ))
    result = await session.exec(smts)
    return result.all()

async def count_nodes(
    enabled: bool | None = None,
    *,
    session: AsyncSession
) -> int:
    """统计节点数量"""
    smts = select(func.count(Node.id))
    if enabled is not None:
        smts = smts.where(Node.enabled == True)  # noqa: E712
    result = await session.exec(smts)
    return result.one()


async def select_node_model_by_id(
    id: str | UUID,
    *,
    session: AsyncSession
) -> NodeModel | None:
    """通过 ID 查询节点模型"""
    id = UUID(str(id)) if not isinstance(id, UUID) else id
    smts = select(NodeModel).where(NodeModel.id == id)
    result = await session.exec(smts)
    return result.first()


async def select_node_model_by_unique(
    node_id: str | UUID,
    model_name: str,
    model_type: ModelType | str,
    *,
    session: AsyncSession
) -> NodeModel | None:
    """通过节点与模型唯一键查询节点模型"""
    node_uuid = UUID(str(node_id)) if not isinstance(node_id, UUID) else node_id
    model_type_value = _coerce_model_type(model_type)
    smts = select(NodeModel).where(
        NodeModel.node_id == node_uuid,
        NodeModel.model_name == model_name,
        NodeModel.model_type == model_type_value,
    )
    result = await session.exec(smts)
    return result.first()


async def select_node_models(
    node_id: str | UUID | None = None,
    model_type: ModelType | str | None = None,
    enabled: bool | None = None,
    orderby: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    *,
    session: AsyncSession
) -> List[NodeModel]:
    """查询节点模型列表"""
    smts = select(NodeModel)
    if node_id is not None:
        node_uuid = UUID(str(node_id)) if not isinstance(node_id, UUID) else node_id
        smts = smts.where(NodeModel.node_id == node_uuid)

    if model_type is not None:
        model_type_value = _coerce_model_type(model_type)
        smts = smts.where(NodeModel.model_type == model_type_value)

    if enabled is not None:
        smts = smts.where(NodeModel.enabled == True)  # noqa: E712

    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)

    smts = smts.order_by(parse_orderby_column(
        NodeModel, orderby, NodeModel.model_name.asc()
    ))
    result = await session.exec(smts)
    return result.all()


async def count_node_models(
    node_id: str | UUID | None = None,
    model_type: ModelType | str | None = None,
    enabled: bool | None = None,
    *,
    session: AsyncSession
) -> int:
    """统计节点模型数量"""
    smts = select(func.count(NodeModel.id))

    if node_id is not None:
        node_uuid = UUID(str(node_id)) if not isinstance(node_id, UUID) else node_id
        smts = smts.where(NodeModel.node_id == node_uuid)

    if model_type is not None:
        model_type_value = _coerce_model_type(model_type)
        smts = smts.where(NodeModel.model_type == model_type_value)

    if enabled is not None:
        smts = smts.where(NodeModel.enabled == True)  # noqa: E712

    result = await session.exec(smts)
    return result.one()
