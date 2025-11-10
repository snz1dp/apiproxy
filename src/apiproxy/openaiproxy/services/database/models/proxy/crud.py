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


from datetime import datetime
from typing import List, Optional
from uuid import UUID, uuid4

from openaiproxy.logging import logger
from openaiproxy.services.database.models.proxy.model import (
    ProxyInstance, ProxyNodeStatus, ProxyNodeStatusLog, RequestAction
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


async def upsert_proxy_instance(
    *,
    session: AsyncSession,
    instance_name: str,
    instance_ip: str,
    instance_id: Optional[UUID] = None,
    process_id: Optional[str] = None,
) -> ProxyInstance:
    """Create or update a proxy instance record."""

    proxy_row: Optional[ProxyInstance] = None

    if instance_id is not None:
        proxy_row = await session.get(ProxyInstance, instance_id)

    if proxy_row is None:
        stmt = select(ProxyInstance).where(
            ProxyInstance.instance_name == instance_name,
            ProxyInstance.instance_ip == instance_ip,
        )
        result = await session.exec(stmt)
        proxy_row = result.first()

    if proxy_row is None:
        proxy_row = ProxyInstance(
            id=instance_id or uuid4(),
            instance_name=instance_name,
            instance_ip=instance_ip,
            process_id=process_id,
        )
        session.add(proxy_row)
        await session.flush()
    else:
        if instance_id is not None and proxy_row.id != instance_id:
            logger.warning(
                "Proxy instance found with different identifier; keeping existing id %s",
                proxy_row.id,
            )
        proxy_row.instance_name = instance_name
        proxy_row.instance_ip = instance_ip
        proxy_row.process_id = process_id
        session.add(proxy_row)

    return proxy_row


async def get_or_create_proxy_node_status(
    *,
    session: AsyncSession,
    node_id: UUID,
    proxy_id: Optional[UUID],
    status_id: Optional[UUID] = None,
) -> ProxyNodeStatus:
    """Fetch an existing proxy node status or create one if absent."""

    status_row: Optional[ProxyNodeStatus] = None

    if status_id is not None:
        status_row = await session.get(ProxyNodeStatus, status_id)

    if status_row is None:
        smts = select(ProxyNodeStatus).where(ProxyNodeStatus.node_id == node_id)
        if proxy_id is None:
            smts = smts.where(ProxyNodeStatus.proxy_id.is_(None))
        else:
            smts = smts.where(ProxyNodeStatus.proxy_id == proxy_id)
        result = await session.exec(smts)
        status_row = result.first()

    if status_row is None:
        status_row = ProxyNodeStatus(node_id=node_id, proxy_id=proxy_id)
        session.add(status_row)
        await session.flush()

    return status_row


async def upsert_proxy_node_status(
    *,
    session: AsyncSession,
    node_id: UUID,
    proxy_id: Optional[UUID],
    status_id: Optional[UUID],
    unfinished: int,
    latency: float,
    speed: float,
    avaiaible: bool,
) -> ProxyNodeStatus:
    """Ensure and update a proxy node status record."""

    status_row = await get_or_create_proxy_node_status(
        session=session,
        node_id=node_id,
        proxy_id=proxy_id,
        status_id=status_id,
    )

    status_row.unfinished = unfinished
    status_row.latency = latency
    status_row.speed = speed
    status_row.avaiaible = avaiaible
    session.add(status_row)

    return status_row


async def create_proxy_node_status_log_entry(
    *,
    session: AsyncSession,
    node_id: UUID,
    proxy_id: UUID,
    status_id: UUID,
    ownerapp_id: Optional[str],
    model_name: Optional[str],
    action: RequestAction,
    start_at: datetime,
    end_at: Optional[datetime],
    latency: float,
    request_tokens: int,
    response_tokens: int,
) -> ProxyNodeStatusLog:
    """Create a proxy node status log entry."""

    log_entry = ProxyNodeStatusLog(
        node_id=node_id,
        proxy_id=proxy_id,
        status_id=status_id,
        ownerapp_id=ownerapp_id,
        action=action,
        model_name=model_name,
        start_at=start_at,
        end_at=end_at,
        latency=latency,
        request_tokens=request_tokens,
        response_tokens=response_tokens,
    )
    session.add(log_entry)

    return log_entry


async def delete_stale_proxy_node_status(
    *,
    session: AsyncSession,
    before: datetime,
    exclude_proxy_id: Optional[UUID] = None,
) -> int:
    """Delete proxy node status rows updated before a given time."""

    smts = select(ProxyNodeStatus).where(ProxyNodeStatus.updated_at < before)
    if exclude_proxy_id is None:
        smts = smts.where(ProxyNodeStatus.proxy_id.is_not(None))
    else:
        smts = smts.where(or_(
            ProxyNodeStatus.proxy_id.is_(None),
            ProxyNodeStatus.proxy_id != exclude_proxy_id,
        ))

    result = await session.exec(smts)
    rows = result.all()
    removed = 0

    for row in rows:
        await session.delete(row)
        removed += 1

    return removed
