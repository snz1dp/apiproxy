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

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence
from uuid import UUID, uuid4
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import func, select
from openaiproxy.utils.sqlalchemy import parse_orderby_column
from sqlmodel.ext.asyncio.session import AsyncSession
from openaiproxy.services.database.models.node.model import (
    AppDailyModelUsage,
    AppMonthlyModelUsage,
    AppWeeklyModelUsage,
    ModelType,
    Node,
    NodeModel,
    NodeModelQuota,
    NodeModelQuotaUsage,
)
from openaiproxy.services.database.models.proxy.model import ProxyNodeStatusLog
from openaiproxy.utils.timezone import current_time_in_timezone


def _build_insert_statement(session: AsyncSession, model):
    """根据当前数据库方言构建插入语句。"""

    dialect_name = session.bind.dialect.name if session.bind is not None else ""
    if dialect_name == "sqlite":
        return sqlite_insert(model)
    return postgresql_insert(model)


async def _upsert_periodic_usage_record(
    *,
    model,
    period_field: str,
    period_value: datetime,
    constraint_name: str,
    index_elements: list[str],
    usage,
    session: AsyncSession,
):
    """按周期唯一键原子写入报表聚合记录。"""

    now = current_time_in_timezone()
    values = {
        "id": uuid4(),
        "ownerapp_id": usage.ownerapp_id,
        "model_name": usage.model_name,
        period_field: period_value,
        "call_count": usage.call_count,
        "request_tokens": usage.request_tokens,
        "response_tokens": usage.response_tokens,
        "total_tokens": usage.total_tokens,
        "created_at": now,
        "updated_at": now,
    }
    insert_stmt = _build_insert_statement(session, model).values(**values)
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=index_elements,
            set_={
                "call_count": usage.call_count,
                "request_tokens": usage.request_tokens,
                "response_tokens": usage.response_tokens,
                "total_tokens": usage.total_tokens,
                "updated_at": now,
            },
        )
    else:
        upsert_stmt = insert_stmt.on_conflict_do_update(
            constraint=constraint_name,
            set_={
                "call_count": usage.call_count,
                "request_tokens": usage.request_tokens,
                "response_tokens": usage.response_tokens,
                "total_tokens": usage.total_tokens,
                "updated_at": now,
            },
        )

    result = await session.exec(upsert_stmt.returning(model.id))
    resolved_id = result.one()
    await session.flush()

    row = await session.get(model, resolved_id, populate_existing=True)
    if row is not None:
        return row

    lookup_stmt = select(model).where(
        getattr(model, "ownerapp_id") == usage.ownerapp_id,
        getattr(model, "model_name") == usage.model_name,
        getattr(model, period_field) == period_value,
    )
    return (await session.exec(lookup_stmt)).first()


@dataclass(slots=True)
class MonthlyUsageAggregate:
    """月度模型用量聚合结果。"""

    ownerapp_id: str
    model_name: str
    call_count: int
    request_tokens: int
    response_tokens: int
    total_tokens: int


@dataclass(slots=True)
class DailyUsageAggregate:
    """日度模型用量聚合结果。"""

    ownerapp_id: str
    model_name: str
    call_count: int
    request_tokens: int
    response_tokens: int
    total_tokens: int


@dataclass(slots=True)
class WeeklyUsageAggregate:
    """周度模型用量聚合结果。"""

    ownerapp_id: str
    model_name: str
    call_count: int
    request_tokens: int
    response_tokens: int
    total_tokens: int


@dataclass(slots=True)
class YearlyUsageAggregate:
    """年度模型用量聚合结果。"""

    ownerapp_id: str
    model_name: str
    call_count: int
    request_tokens: int
    response_tokens: int
    total_tokens: int


@dataclass(slots=True)
class YearlyUsageTotalAggregate:
    """年度模型用量总计聚合结果（按应用，不分模型）。"""

    ownerapp_id: str
    call_count: int
    request_tokens: int
    response_tokens: int
    total_tokens: int


@dataclass(slots=True)
class MonthlyUsageTotalAggregate:
    """月度模型用量总计聚合结果（按应用，不分模型）。"""

    ownerapp_id: str
    call_count: int
    request_tokens: int
    response_tokens: int
    total_tokens: int


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
    expired: bool | None = None,
    orderby: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    *,
    session: AsyncSession
) -> List[Node]:
    """查询所有节点"""
    smts = select(Node)
    if enabled is not None:
        smts = smts.where(Node.enabled == enabled)  # noqa: E712

    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)

    if expired is not None:
        now = datetime.now().astimezone()
        if expired:
            smts = smts.where(
                Node.expired_at != None,
                Node.expired_at <= now
            )
        else:
            smts = smts.where(
                (Node.expired_at == None) | (Node.expired_at > now)
            )

    smts = smts.order_by(parse_orderby_column(
        Node, orderby, Node.created_at.asc()
    ))
    result = await session.exec(smts)
    return result.all()

async def count_nodes(
    enabled: bool | None = None,
    expired: bool | None = None,
    *,
    session: AsyncSession
) -> int:
    """统计节点数量"""
    smts = select(func.count(Node.id))
    if enabled is not None:
        smts = smts.where(Node.enabled == enabled)  # noqa: E712

    if expired is not None:
        now = datetime.now().astimezone()
        if expired:
            smts = smts.where(
                Node.expired_at != None,
                Node.expired_at <= now
            )
        else:
            smts = smts.where(
                (Node.expired_at == None) | (Node.expired_at > now)
            )

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
    node_ids: list[str] | list[UUID] | UUID | str | None = None,
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
    if node_ids and not isinstance(node_ids, list):
        node_ids = [node_ids]

    if node_ids is not None and node_ids:
        node_ids = _ensure_uuid_list(node_ids)
        smts = smts.where(NodeModel.node_id.in_(node_ids))

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
    node_ids: list[str] | list[UUID] | UUID | str | None = None,
    model_type: ModelType | str | None = None,
    enabled: bool | None = None,
    *,
    session: AsyncSession
) -> int:
    """统计节点模型数量"""
    smts = select(func.count(NodeModel.id))

    if node_ids is not None and not isinstance(node_ids, list):
        node_ids = [node_ids]

    if node_ids is not None and node_ids:
        node_ids = _ensure_uuid_list(node_ids)
        smts = smts.where(NodeModel.node_id.in_(node_ids))

    if model_type is not None:
        model_type_value = _coerce_model_type(model_type)
        smts = smts.where(NodeModel.model_type == model_type_value)

    if enabled is not None:
        smts = smts.where(NodeModel.enabled == True)  # noqa: E712

    result = await session.exec(smts)
    return result.one()


def _ensure_uuid_list(values: Sequence[str | UUID]) -> list[UUID]:
    return [UUID(str(val)) if not isinstance(val, UUID) else val for val in values]


async def select_node_model_quota_by_id(
    id: str | UUID,
    *,
    session: AsyncSession,
) -> NodeModelQuota | None:
    """通过ID查询节点模型配额"""
    identifier = UUID(str(id)) if not isinstance(id, UUID) else id
    smts = select(NodeModelQuota).where(NodeModelQuota.id == identifier)
    result = await session.exec(smts)
    return result.first()


async def select_node_model_quota_by_unique(
    *,
    node_model_id: str | UUID,
    order_id: Optional[str],
    session: AsyncSession,
) -> NodeModelQuota | None:
    """根据节点模型与订单ID查询节点模型配额"""
    node_model_uuid = UUID(str(node_model_id)) if not isinstance(node_model_id, UUID) else node_model_id
    smts = select(NodeModelQuota).where(NodeModelQuota.node_model_id == node_model_uuid)
    if order_id:
        smts = smts.where(NodeModelQuota.order_id == order_id)
    else:
        smts = smts.where(NodeModelQuota.order_id.is_(None))
    result = await session.exec(smts)
    return result.first()


async def select_node_model_quotas(
    node_ids: list[str] | list[UUID] | UUID | str | None = None,
    node_model_ids: list[str] | list[UUID] | UUID | str | None = None,
    order_id: Optional[str] = None,
    expired: Optional[bool] = None,
    orderby: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    *,
    session: AsyncSession,
) -> List[NodeModelQuota]:
    """查询节点模型配额列表"""
    smts = select(NodeModelQuota)

    if node_ids is not None and not isinstance(node_ids, list):
        node_ids = [node_ids]

    if node_ids is not None and node_ids:
        node_id_values = _ensure_uuid_list(node_ids)
        smts = smts.join(NodeModel, NodeModel.id == NodeModelQuota.node_model_id)
        smts = smts.where(NodeModel.node_id.in_(node_id_values))

    if node_model_ids is not None and not isinstance(node_model_ids, list):
        node_model_ids = [node_model_ids]

    if node_model_ids is not None and node_model_ids:
        node_model_values = _ensure_uuid_list(node_model_ids)
        smts = smts.where(NodeModelQuota.node_model_id.in_(node_model_values))

    if order_id is not None:
        if order_id:
            smts = smts.where(NodeModelQuota.order_id == order_id)
        else:
            smts = smts.where(NodeModelQuota.order_id.is_(None))

    if expired is not None:
        now = datetime.now().astimezone()
        if expired:
            smts = smts.where(
                NodeModelQuota.expired_at != None,
                NodeModelQuota.expired_at <= now,
            )
        else:
            smts = smts.where(
                (NodeModelQuota.expired_at == None) | (NodeModelQuota.expired_at > now)
            )

    order_clause = parse_orderby_column(NodeModelQuota, orderby, NodeModelQuota.created_at.desc())
    if order_clause is not None:
        smts = smts.order_by(order_clause)

    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)

    result = await session.exec(smts)
    return result.all()


async def count_node_model_quotas(
    node_ids: list[str] | list[UUID] | UUID | str | None = None,
    node_model_ids: list[str] | list[UUID] | UUID | str | None = None,
    order_id: Optional[str] = None,
    expired: Optional[bool] = None,
    *,
    session: AsyncSession,
) -> int:
    """统计节点模型配额数量"""
    smts = select(func.count(NodeModelQuota.id))

    if node_ids is not None and not isinstance(node_ids, list):
        node_ids = [node_ids]

    if node_ids is not None and node_ids:
        node_id_values = _ensure_uuid_list(node_ids)
        smts = smts.join(NodeModel, NodeModel.id == NodeModelQuota.node_model_id)
        smts = smts.where(NodeModel.node_id.in_(node_id_values))

    if node_model_ids is not None and not isinstance(node_model_ids, list):
        node_model_ids = [node_model_ids]

    if node_model_ids is not None and node_model_ids:
        node_model_values = _ensure_uuid_list(node_model_ids)
        smts = smts.where(NodeModelQuota.node_model_id.in_(node_model_values))

    if order_id is not None:
        if order_id:
            smts = smts.where(NodeModelQuota.order_id == order_id)
        else:
            smts = smts.where(NodeModelQuota.order_id.is_(None))

    if expired is not None:
        now = datetime.now().astimezone()
        if expired:
            smts = smts.where(
                NodeModelQuota.expired_at != None,
                NodeModelQuota.expired_at <= now,
            )
        else:
            smts = smts.where(
                (NodeModelQuota.expired_at == None) | (NodeModelQuota.expired_at > now)
            )

    result = await session.exec(smts)
    return result.one()


async def select_node_model_quota_usages(
    quota_ids: list[str] | list[UUID] | UUID | str | None = None,
    node_ids: list[str] | list[UUID] | UUID | str | None = None,
    node_model_ids: list[str] | list[UUID] | UUID | str | None = None,
    ownerapp_id: Optional[str] = None,
    request_action: Optional[str] = None,
    orderby: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    *,
    session: AsyncSession,
) -> List[NodeModelQuotaUsage]:
    """查询节点模型配额使用记录"""
    smts = select(NodeModelQuotaUsage)

    if quota_ids is not None and not isinstance(quota_ids, list):
        quota_ids = [quota_ids]

    if quota_ids is not None and quota_ids:
        quota_values = _ensure_uuid_list(quota_ids)
        smts = smts.where(NodeModelQuotaUsage.quota_id.in_(quota_values))

    if node_ids is not None and not isinstance(node_ids, list):
        node_ids = [node_ids]

    if node_ids is not None and node_ids:
        node_values = _ensure_uuid_list(node_ids)
        smts = smts.where(NodeModelQuotaUsage.node_id.in_(node_values))

    if node_model_ids is not None and not isinstance(node_model_ids, list):
        node_model_ids = [node_model_ids]

    if node_model_ids is not None and node_model_ids:
        node_model_values = _ensure_uuid_list(node_model_ids)
        smts = smts.where(NodeModelQuotaUsage.node_model_id.in_(node_model_values))

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(NodeModelQuotaUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(NodeModelQuotaUsage.ownerapp_id.is_(None))

    if request_action is not None:
        if request_action:
            smts = smts.where(NodeModelQuotaUsage.request_action == request_action)
        else:
            smts = smts.where(NodeModelQuotaUsage.request_action.is_(None))

    order_clause = parse_orderby_column(NodeModelQuotaUsage, orderby, NodeModelQuotaUsage.created_at.desc())
    if order_clause is not None:
        smts = smts.order_by(order_clause)

    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)

    result = await session.exec(smts)
    return result.all()


async def count_node_model_quota_usages(
    quota_ids: list[str] | list[UUID] | UUID | str | None = None,
    node_ids: list[str] | list[UUID] | UUID | str | None = None,
    node_model_ids: list[str] | list[UUID] | UUID | str | None = None,
    ownerapp_id: Optional[str] = None,
    request_action: Optional[str] = None,
    *,
    session: AsyncSession,
) -> int:
    """统计节点模型配额使用记录数量"""
    smts = select(func.count(NodeModelQuotaUsage.id))

    if quota_ids is not None and not isinstance(quota_ids, list):
        quota_ids = [quota_ids]

    if quota_ids is not None and quota_ids:
        quota_values = _ensure_uuid_list(quota_ids)
        smts = smts.where(NodeModelQuotaUsage.quota_id.in_(quota_values))

    if node_ids is not None and not isinstance(node_ids, list):
        node_ids = [node_ids]

    if node_ids is not None and node_ids:
        node_values = _ensure_uuid_list(node_ids)
        smts = smts.where(NodeModelQuotaUsage.node_id.in_(node_values))

    if node_model_ids is not None and not isinstance(node_model_ids, list):
        node_model_ids = [node_model_ids]

    if node_model_ids is not None and node_model_ids:
        node_model_values = _ensure_uuid_list(node_model_ids)
        smts = smts.where(NodeModelQuotaUsage.node_model_id.in_(node_model_values))

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(NodeModelQuotaUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(NodeModelQuotaUsage.ownerapp_id.is_(None))

    if request_action is not None:
        if request_action:
            smts = smts.where(NodeModelQuotaUsage.request_action == request_action)
        else:
            smts = smts.where(NodeModelQuotaUsage.request_action.is_(None))

    result = await session.exec(smts)
    return result.one()


async def aggregate_monthly_model_usage(
    *,
    month_start: datetime,
    month_end: datetime,
    session: AsyncSession,
) -> list[MonthlyUsageAggregate]:
    """聚合指定月份区间内的应用模型用量。"""

    smts = (
        select(
            ProxyNodeStatusLog.ownerapp_id,
            ProxyNodeStatusLog.model_name,
            func.count(ProxyNodeStatusLog.id),
            func.coalesce(func.sum(ProxyNodeStatusLog.request_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.response_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.total_tokens), 0),
        )
        .where(
            ProxyNodeStatusLog.end_at.is_not(None),
            ProxyNodeStatusLog.start_at >= month_start,
            ProxyNodeStatusLog.start_at < month_end,
            ProxyNodeStatusLog.ownerapp_id.is_not(None),
            ProxyNodeStatusLog.model_name.is_not(None),
        )
        .group_by(ProxyNodeStatusLog.ownerapp_id, ProxyNodeStatusLog.model_name)
    )

    result = await session.exec(smts)
    rows = result.all()
    aggregated: list[MonthlyUsageAggregate] = []
    for ownerapp_id, model_name, call_count, request_tokens, response_tokens, total_tokens in rows:
        if not ownerapp_id or not model_name:
            continue
        aggregated.append(
            MonthlyUsageAggregate(
                ownerapp_id=str(ownerapp_id),
                model_name=str(model_name),
                call_count=int(call_count or 0),
                request_tokens=int(request_tokens or 0),
                response_tokens=int(response_tokens or 0),
                total_tokens=int(total_tokens or 0),
            )
        )
    return aggregated


async def aggregate_daily_model_usage(
    *,
    day_start: datetime,
    day_end: datetime,
    session: AsyncSession,
) -> list[DailyUsageAggregate]:
    """聚合指定日期区间内的应用模型用量。"""

    smts = (
        select(
            ProxyNodeStatusLog.ownerapp_id,
            ProxyNodeStatusLog.model_name,
            func.count(ProxyNodeStatusLog.id),
            func.coalesce(func.sum(ProxyNodeStatusLog.request_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.response_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.total_tokens), 0),
        )
        .where(
            ProxyNodeStatusLog.end_at.is_not(None),
            ProxyNodeStatusLog.start_at >= day_start,
            ProxyNodeStatusLog.start_at < day_end,
            ProxyNodeStatusLog.ownerapp_id.is_not(None),
            ProxyNodeStatusLog.model_name.is_not(None),
        )
        .group_by(ProxyNodeStatusLog.ownerapp_id, ProxyNodeStatusLog.model_name)
    )

    rows = (await session.exec(smts)).all()
    return [
        DailyUsageAggregate(
            ownerapp_id=str(ownerapp_id),
            model_name=str(model_name),
            call_count=int(call_count or 0),
            request_tokens=int(request_tokens or 0),
            response_tokens=int(response_tokens or 0),
            total_tokens=int(total_tokens or 0),
        )
        for ownerapp_id, model_name, call_count, request_tokens, response_tokens, total_tokens in rows
        if ownerapp_id and model_name
    ]


async def aggregate_weekly_model_usage(
    *,
    week_start: datetime,
    week_end: datetime,
    session: AsyncSession,
) -> list[WeeklyUsageAggregate]:
    """聚合指定周区间内的应用模型用量。"""

    smts = (
        select(
            ProxyNodeStatusLog.ownerapp_id,
            ProxyNodeStatusLog.model_name,
            func.count(ProxyNodeStatusLog.id),
            func.coalesce(func.sum(ProxyNodeStatusLog.request_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.response_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.total_tokens), 0),
        )
        .where(
            ProxyNodeStatusLog.end_at.is_not(None),
            ProxyNodeStatusLog.start_at >= week_start,
            ProxyNodeStatusLog.start_at < week_end,
            ProxyNodeStatusLog.ownerapp_id.is_not(None),
            ProxyNodeStatusLog.model_name.is_not(None),
        )
        .group_by(ProxyNodeStatusLog.ownerapp_id, ProxyNodeStatusLog.model_name)
    )

    rows = (await session.exec(smts)).all()
    return [
        WeeklyUsageAggregate(
            ownerapp_id=str(ownerapp_id),
            model_name=str(model_name),
            call_count=int(call_count or 0),
            request_tokens=int(request_tokens or 0),
            response_tokens=int(response_tokens or 0),
            total_tokens=int(total_tokens or 0),
        )
        for ownerapp_id, model_name, call_count, request_tokens, response_tokens, total_tokens in rows
        if ownerapp_id and model_name
    ]


async def upsert_app_daily_model_usage(
    *,
    day_start: datetime,
    usage: DailyUsageAggregate,
    session: AsyncSession,
) -> AppDailyModelUsage:
    """按唯一键幂等写入应用日度模型用量。"""

    return await _upsert_periodic_usage_record(
        model=AppDailyModelUsage,
        period_field="day_start",
        period_value=day_start,
        constraint_name="uix_openaiapi_app_daily_usage_unique",
        index_elements=["ownerapp_id", "model_name", "day_start"],
        usage=usage,
        session=session,
    )


async def upsert_app_weekly_model_usage(
    *,
    week_start: datetime,
    usage: WeeklyUsageAggregate,
    session: AsyncSession,
) -> AppWeeklyModelUsage:
    """按唯一键幂等写入应用周度模型用量。"""

    return await _upsert_periodic_usage_record(
        model=AppWeeklyModelUsage,
        period_field="week_start",
        period_value=week_start,
        constraint_name="uix_openaiapi_app_weekly_usage_unique",
        index_elements=["ownerapp_id", "model_name", "week_start"],
        usage=usage,
        session=session,
    )


async def upsert_app_monthly_model_usage(
    *,
    month_start: datetime,
    usage: MonthlyUsageAggregate,
    session: AsyncSession,
) -> AppMonthlyModelUsage:
    """按唯一键幂等写入应用月度模型用量。"""

    return await _upsert_periodic_usage_record(
        model=AppMonthlyModelUsage,
        period_field="month_start",
        period_value=month_start,
        constraint_name="uix_openaiapi_app_monthly_usage_unique",
        index_elements=["ownerapp_id", "model_name", "month_start"],
        usage=usage,
        session=session,
    )


async def select_app_monthly_model_usages(
    ownerapp_id: Optional[str] = None,
    month_start: Optional[datetime] = None,
    model_names: Optional[list[str]] = None,
    orderby: Optional[str] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    *,
    session: AsyncSession,
) -> list[AppMonthlyModelUsage]:
    """查询应用月度模型用量分页数据。"""

    smts = select(AppMonthlyModelUsage)

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id.is_(None))

    if month_start is not None:
        smts = smts.where(AppMonthlyModelUsage.month_start == month_start)

    if model_names:
        smts = smts.where(AppMonthlyModelUsage.model_name.in_(model_names))

    order_clause = parse_orderby_column(
        AppMonthlyModelUsage,
        orderby,
        AppMonthlyModelUsage.month_start.desc(),
    )
    if order_clause is not None:
        smts = smts.order_by(order_clause)

    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)

    result = await session.exec(smts)
    return result.all()


async def select_app_daily_model_usages(
    ownerapp_id: Optional[str] = None,
    day_start: Optional[datetime] = None,
    model_names: Optional[list[str]] = None,
    orderby: Optional[str] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    *,
    session: AsyncSession,
) -> list[AppDailyModelUsage]:
    """查询应用日度模型用量分页数据。"""

    smts = select(AppDailyModelUsage)

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppDailyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppDailyModelUsage.ownerapp_id.is_(None))

    if day_start is not None:
        smts = smts.where(AppDailyModelUsage.day_start == day_start)

    if model_names:
        smts = smts.where(AppDailyModelUsage.model_name.in_(model_names))

    order_clause = parse_orderby_column(
        AppDailyModelUsage,
        orderby,
        AppDailyModelUsage.day_start.desc(),
    )
    if order_clause is not None:
        smts = smts.order_by(order_clause)

    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)

    return (await session.exec(smts)).all()


async def count_app_daily_model_usages(
    ownerapp_id: Optional[str] = None,
    day_start: Optional[datetime] = None,
    model_names: Optional[list[str]] = None,
    *,
    session: AsyncSession,
) -> int:
    """统计应用日度模型用量记录数量。"""

    smts = select(func.count(AppDailyModelUsage.id))

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppDailyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppDailyModelUsage.ownerapp_id.is_(None))

    if day_start is not None:
        smts = smts.where(AppDailyModelUsage.day_start == day_start)

    if model_names:
        smts = smts.where(AppDailyModelUsage.model_name.in_(model_names))

    return int((await session.exec(smts)).one() or 0)


async def select_app_weekly_model_usages(
    ownerapp_id: Optional[str] = None,
    week_start: Optional[datetime] = None,
    model_names: Optional[list[str]] = None,
    orderby: Optional[str] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    *,
    session: AsyncSession,
) -> list[AppWeeklyModelUsage]:
    """查询应用周度模型用量分页数据。"""

    smts = select(AppWeeklyModelUsage)

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppWeeklyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppWeeklyModelUsage.ownerapp_id.is_(None))

    if week_start is not None:
        smts = smts.where(AppWeeklyModelUsage.week_start == week_start)

    if model_names:
        smts = smts.where(AppWeeklyModelUsage.model_name.in_(model_names))

    order_clause = parse_orderby_column(
        AppWeeklyModelUsage,
        orderby,
        AppWeeklyModelUsage.week_start.desc(),
    )
    if order_clause is not None:
        smts = smts.order_by(order_clause)

    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)

    return (await session.exec(smts)).all()


async def count_app_weekly_model_usages(
    ownerapp_id: Optional[str] = None,
    week_start: Optional[datetime] = None,
    model_names: Optional[list[str]] = None,
    *,
    session: AsyncSession,
) -> int:
    """统计应用周度模型用量记录数量。"""

    smts = select(func.count(AppWeeklyModelUsage.id))

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppWeeklyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppWeeklyModelUsage.ownerapp_id.is_(None))

    if week_start is not None:
        smts = smts.where(AppWeeklyModelUsage.week_start == week_start)

    if model_names:
        smts = smts.where(AppWeeklyModelUsage.model_name.in_(model_names))

    return int((await session.exec(smts)).one() or 0)


async def count_app_monthly_model_usages(
    ownerapp_id: Optional[str] = None,
    month_start: Optional[datetime] = None,
    model_names: Optional[list[str]] = None,
    *,
    session: AsyncSession,
) -> int:
    """统计应用月度模型用量记录数量。"""

    smts = select(func.count(AppMonthlyModelUsage.id))

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id.is_(None))

    if month_start is not None:
        smts = smts.where(AppMonthlyModelUsage.month_start == month_start)

    if model_names:
        smts = smts.where(AppMonthlyModelUsage.model_name.in_(model_names))

    result = await session.exec(smts)
    return result.one()


async def select_app_yearly_model_usages(
    *,
    year_start: datetime,
    year_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    session: AsyncSession,
) -> list[YearlyUsageAggregate]:
    """查询应用年度模型用量聚合分页数据。"""

    smts = (
        select(
            AppMonthlyModelUsage.ownerapp_id,
            AppMonthlyModelUsage.model_name,
            func.coalesce(func.sum(AppMonthlyModelUsage.call_count), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.request_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.response_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.total_tokens), 0),
        )
        .where(
            AppMonthlyModelUsage.month_start >= year_start,
            AppMonthlyModelUsage.month_start < year_end,
        )
        .group_by(
            AppMonthlyModelUsage.ownerapp_id,
            AppMonthlyModelUsage.model_name,
        )
        .order_by(
            AppMonthlyModelUsage.ownerapp_id.asc(),
            AppMonthlyModelUsage.model_name.asc(),
        )
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(AppMonthlyModelUsage.model_name.in_(model_names))

    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)

    rows = (await session.exec(smts)).all()
    return [
        YearlyUsageAggregate(
            ownerapp_id=str(row_ownerapp_id),
            model_name=str(row_model_name),
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_model_name,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
        ) in rows
        if row_ownerapp_id and row_model_name
    ]


async def count_app_yearly_model_usages(
    *,
    year_start: datetime,
    year_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> int:
    """统计应用年度模型用量聚合记录数量。"""

    grouped_smts = (
        select(
            AppMonthlyModelUsage.ownerapp_id,
            AppMonthlyModelUsage.model_name,
        )
        .where(
            AppMonthlyModelUsage.month_start >= year_start,
            AppMonthlyModelUsage.month_start < year_end,
        )
        .group_by(
            AppMonthlyModelUsage.ownerapp_id,
            AppMonthlyModelUsage.model_name,
        )
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            grouped_smts = grouped_smts.where(AppMonthlyModelUsage.ownerapp_id == ownerapp_id)
        else:
            grouped_smts = grouped_smts.where(AppMonthlyModelUsage.ownerapp_id.is_(None))

    if model_names:
        grouped_smts = grouped_smts.where(AppMonthlyModelUsage.model_name.in_(model_names))

    count_smts = select(func.count()).select_from(grouped_smts.subquery())
    result = await session.exec(count_smts)
    return int(result.one() or 0)


async def select_app_yearly_total_usages(
    *,
    year_start: datetime,
    year_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    session: AsyncSession,
) -> list[YearlyUsageTotalAggregate]:
    """查询应用年度模型用量总计分页数据。"""

    smts = (
        select(
            AppMonthlyModelUsage.ownerapp_id,
            func.coalesce(func.sum(AppMonthlyModelUsage.call_count), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.request_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.response_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.total_tokens), 0),
        )
        .where(
            AppMonthlyModelUsage.month_start >= year_start,
            AppMonthlyModelUsage.month_start < year_end,
        )
        .group_by(AppMonthlyModelUsage.ownerapp_id)
        .order_by(AppMonthlyModelUsage.ownerapp_id.asc())
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(AppMonthlyModelUsage.model_name.in_(model_names))

    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)

    rows = (await session.exec(smts)).all()
    return [
        YearlyUsageTotalAggregate(
            ownerapp_id=str(row_ownerapp_id),
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
        ) in rows
        if row_ownerapp_id
    ]


async def count_app_yearly_total_usages(
    *,
    year_start: datetime,
    year_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> int:
    """统计应用年度模型用量总计聚合记录数量。"""

    grouped_smts = (
        select(AppMonthlyModelUsage.ownerapp_id)
        .where(
            AppMonthlyModelUsage.month_start >= year_start,
            AppMonthlyModelUsage.month_start < year_end,
        )
        .group_by(AppMonthlyModelUsage.ownerapp_id)
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            grouped_smts = grouped_smts.where(AppMonthlyModelUsage.ownerapp_id == ownerapp_id)
        else:
            grouped_smts = grouped_smts.where(AppMonthlyModelUsage.ownerapp_id.is_(None))

    if model_names:
        grouped_smts = grouped_smts.where(AppMonthlyModelUsage.model_name.in_(model_names))

    count_smts = select(func.count()).select_from(grouped_smts.subquery())
    result = await session.exec(count_smts)
    return int(result.one() or 0)


async def select_app_monthly_total_usages(
    *,
    month_start: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    session: AsyncSession,
) -> list[MonthlyUsageTotalAggregate]:
    """查询应用月度模型用量总计分页数据。"""

    smts = (
        select(
            AppMonthlyModelUsage.ownerapp_id,
            func.coalesce(func.sum(AppMonthlyModelUsage.call_count), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.request_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.response_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.total_tokens), 0),
        )
        .where(AppMonthlyModelUsage.month_start == month_start)
        .group_by(AppMonthlyModelUsage.ownerapp_id)
        .order_by(AppMonthlyModelUsage.ownerapp_id.asc())
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(AppMonthlyModelUsage.model_name.in_(model_names))

    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)

    rows = (await session.exec(smts)).all()
    return [
        MonthlyUsageTotalAggregate(
            ownerapp_id=str(row_ownerapp_id),
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
        ) in rows
        if row_ownerapp_id
    ]


async def count_app_monthly_total_usages(
    *,
    month_start: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> int:
    """统计应用月度模型用量总计聚合记录数量。"""

    grouped_smts = (
        select(AppMonthlyModelUsage.ownerapp_id)
        .where(AppMonthlyModelUsage.month_start == month_start)
        .group_by(AppMonthlyModelUsage.ownerapp_id)
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            grouped_smts = grouped_smts.where(AppMonthlyModelUsage.ownerapp_id == ownerapp_id)
        else:
            grouped_smts = grouped_smts.where(AppMonthlyModelUsage.ownerapp_id.is_(None))

    if model_names:
        grouped_smts = grouped_smts.where(AppMonthlyModelUsage.model_name.in_(model_names))

    count_smts = select(func.count()).select_from(grouped_smts.subquery())
    result = await session.exec(count_smts)
    return int(result.one() or 0)
