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
from typing import List, Optional, Tuple
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
    instance_id: UUID,
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
                "代理实例已存在一个旧ID，保留使用旧ID={}",
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
    stream: bool = False,
    error: bool = False,
    error_message: Optional[str] = None,
    error_stack: Optional[str] = None,
    request_data: Optional[str] = None,
    response_data: Optional[str] = None,
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
        stream=stream,
        error=error,
        error_message=error_message,
        error_stack=error_stack,
        request_data=request_data,
        response_data=response_data,
    )
    session.add(log_entry)
    await session.flush()

    return log_entry


async def update_proxy_node_status_log_entry(
    *,
    session: AsyncSession,
    log_id: UUID,
    end_at: Optional[datetime] = None,
    latency: Optional[float] = None,
    request_tokens: Optional[int] = None,
    response_tokens: Optional[int] = None,
    error: Optional[bool] = None,
    error_message: Optional[str] = None,
    error_stack: Optional[str] = None,
    request_data: Optional[str] = None,
    response_data: Optional[str] = None,
) -> Optional[ProxyNodeStatusLog]:
    """Update a proxy node status log entry."""

    log_entry = await session.get(ProxyNodeStatusLog, log_id)
    if log_entry is None:
        return None

    if end_at is not None:
        log_entry.end_at = end_at
    if latency is not None:
        log_entry.latency = latency
    if request_tokens is not None:
        log_entry.request_tokens = request_tokens
    if response_tokens is not None:
        log_entry.response_tokens = response_tokens
    if error is not None:
        log_entry.error = error
    if error_message is not None:
        log_entry.error_message = error_message
    if error_stack is not None:
        log_entry.error_stack = error_stack
    if request_data is not None:
        log_entry.request_data = request_data
    if response_data is not None:
        log_entry.response_data = response_data

    session.add(log_entry)
    await session.flush()
    return log_entry


async def fetch_proxy_node_metrics(
    *,
    session: AsyncSession,
    node_id: UUID,
    proxy_id: Optional[UUID],
    history_limit: int,
) -> Tuple[int, Optional[float], Optional[float], List[float]]:
    """Aggregate runtime metrics for a node from status logs."""

    if history_limit <= 0:
        history_limit = 1

    base_filters = [ProxyNodeStatusLog.node_id == node_id]
    if proxy_id is None:
        base_filters.append(ProxyNodeStatusLog.proxy_id.is_(None))
    else:
        base_filters.append(ProxyNodeStatusLog.proxy_id == proxy_id)

    unfinished_stmt = (
        select(func.count(ProxyNodeStatusLog.id))
        .where(*base_filters)
        .where(ProxyNodeStatusLog.end_at.is_(None))
    )
    unfinished_result = await session.exec(unfinished_stmt)
    unfinished_count = unfinished_result.one()

    latency_stmt = (
        select(ProxyNodeStatusLog.latency)
        .where(*base_filters)
        .where(ProxyNodeStatusLog.end_at.is_not(None))
        .where(ProxyNodeStatusLog.latency.is_not(None))
        .order_by(ProxyNodeStatusLog.end_at.desc())
        .limit(history_limit)
    )
    latency_result = await session.exec(latency_stmt)
    latency_desc = [float(item) for item in latency_result.all() if item is not None and item >= 0]
    latency_samples = list(reversed(latency_desc))

    average_latency = None
    if latency_desc:
        average_latency = float(sum(latency_desc) / len(latency_desc))

    speed = None
    if average_latency and average_latency > 0:
        speed = 1.0 / average_latency

    return unfinished_count, average_latency, speed, latency_samples


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
