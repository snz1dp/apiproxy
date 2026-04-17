"""数据库辅助工具。"""

from datetime import datetime
from typing import Iterable
from uuid import UUID

from sqlalchemy import or_
from sqlmodel import delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from openaiproxy.services.database.models.proxy.model import ProxyNodeStatus


async def select_stale_proxy_node_status(
    *,
    session: AsyncSession,
    expiration_cutoff: datetime,
    exclude_proxy_id: UUID | None,
) -> list[ProxyNodeStatus]:
    """查询超过过期时间且不属于当前代理实例的节点状态记录。

    Args:
        session (AsyncSession): 当前异步数据库会话。
        expiration_cutoff (datetime): 过期时间阈值。
        exclude_proxy_id (UUID | None): 需要排除的代理实例 ID。

    Returns:
        list[ProxyNodeStatus]: 命中的过期节点状态记录列表。
    """

    stmt = select(ProxyNodeStatus).where(
        ProxyNodeStatus.updated_at < expiration_cutoff
    )
    if exclude_proxy_id is not None:
        stmt = stmt.where(
            or_(
                ProxyNodeStatus.proxy_id.is_(None),
                ProxyNodeStatus.proxy_id != exclude_proxy_id,
            )
        )

    result = await session.exec(stmt)
    return list(result.all())


async def delete_proxy_node_status_by_ids(
    *,
    session: AsyncSession,
    status_ids: Iterable[UUID],
) -> int:
    """按状态 ID 批量删除节点状态记录。

    Args:
        session (AsyncSession): 当前异步数据库会话。
        status_ids (Iterable[UUID]): 待删除的状态 ID 集合。

    Returns:
        int: 实际删除的记录数。
    """

    normalized_status_ids = list(status_ids)
    if not normalized_status_ids:
        return 0

    stmt = (
        delete(ProxyNodeStatus)
        .where(ProxyNodeStatus.id.in_(normalized_status_ids))
        .execution_options(synchronize_session=False)
    )
    result = await session.exec(stmt)
    rowcount = result.rowcount
    if rowcount is None or rowcount < 0:
        return 0
    return int(rowcount)