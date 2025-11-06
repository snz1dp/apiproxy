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
from openaiproxy.services.database.models.proxy.model import (
    ProxyInstance, ProxyNodeStatus, ProxyNodeStatusLog
)
from openaiproxy.utils.sqlalchemy import parse_orderby_column
from sqlmodel import func, select, or_
from sqlmodel.ext.asyncio.session import AsyncSession

async def select_proxy_instances(
    filter: str | None = None,
    orderby: str | None = None,
    offset: int = 0,
    limit: int = 100,
    instance_ids: List[UUID] | None = None,
    *,
    session: AsyncSession,
) -> List[ProxyInstance]:
    """查询代理实例"""
    smts = select(ProxyInstance)
    if filter:
        smts = smts.where(or_(
            ProxyInstance.instance_name.ilike(f"%{filter}%"),
            ProxyInstance.instance_ip.ilike(f"%{filter}%")
        ))

    if instance_ids:
        smts = smts.where(ProxyInstance.id.in_(instance_ids))

    smts = smts.order_by(parse_orderby_column(
        ProxyInstance, orderby, ProxyInstance.created_at.asc()
    ))
    smts = smts.offset(offset).limit(limit)
    result = await session.exec(smts)
    return result.all()

async def count_proxy_instances(
    filter: str | None = None,
    instance_ids: List[UUID] | None = None,
    *,
    session: AsyncSession,
) -> int:
    """统计代理实例数量"""
    smts = select(func.count(ProxyInstance.id))
    if filter:
        smts = smts.where(or_(
            ProxyInstance.instance_name.ilike(f"%{filter}%"),
            ProxyInstance.instance_ip.ilike(f"%{filter}%")
        ))

    if instance_ids:
        smts = smts.where(ProxyInstance.id.in_(instance_ids))

    result = await session.exec(smts)
    return result.one()

async def count_proxy_node_status(
    status_ids: List[UUID] | None = None,
    proxy_instance_ids: List[UUID] | None = None,
    node_ids: List[UUID] | None = None,
    avaiaible: bool | None = None,
    *,
    session: AsyncSession,
) -> int:
    """统计代理实例关联的节点数量"""
    smts = select(func.count(ProxyNodeStatus.id))
    if status_ids:
        smts = smts.where(ProxyNodeStatus.id.in_(status_ids))
    if proxy_instance_ids:
        smts = smts.where(ProxyNodeStatus.proxy_id.in_(proxy_instance_ids))
    if node_ids:
        smts = smts.where(ProxyNodeStatus.node_id.in_(node_ids))
    if avaiaible is not None:
        smts = smts.where(ProxyNodeStatus.available == avaiaible)
    result = await session.exec(smts)
    return result.one()

async def select_proxy_node_status(
    status_ids: List[UUID] | None = None,
    proxy_instance_ids: List[UUID] | None = None,
    node_ids: List[UUID] | None = None,
    avaiaible: bool | None = None,
    orderby: str | None = None,
    offset: int = 0,
    limit: int = 100,
    *,
    session: AsyncSession,
) -> List[ProxyNodeStatus]:
    """查询代理实例关联的节点状态"""
    smts = select(ProxyNodeStatus)
    if status_ids:
        smts = smts.where(ProxyNodeStatus.id.in_(status_ids))
    if proxy_instance_ids:
        smts = smts.where(ProxyNodeStatus.proxy_id.in_(proxy_instance_ids))
    if node_ids:
        smts = smts.where(ProxyNodeStatus.node_id.in_(node_ids))
    if avaiaible is not None:
        smts = smts.where(ProxyNodeStatus.available == avaiaible)

    smts = smts.order_by(parse_orderby_column(
        ProxyNodeStatus, orderby, ProxyNodeStatus.created_at.asc()
    ))
    smts = smts.offset(offset).limit(limit)
    result = await session.exec(smts)
    return result.all()
