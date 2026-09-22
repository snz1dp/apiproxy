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

import calendar
from dataclasses import dataclass, replace as dc_replace
from datetime import datetime, timedelta
from typing import Any, List, Optional, Sequence, Tuple
from uuid import UUID, uuid4
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import update as sa_update
from sqlmodel import func, select
from openaiproxy.utils.sqlalchemy import parse_orderby_column
from sqlmodel.ext.asyncio.session import AsyncSession
from openaiproxy.services.database.models.node.model import (
    AppDailyModelUsage,
    AppMonthlyModelUsage,
    AppWeeklyModelUsage,
    ModelType,
    Node,
    NodeApiKey,
    NodeModel,
    NodeModelQuota,
    NodeModelQuotaUsage,
    ProtocolType,
    QuotaResetCycle,
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
        "cached_tokens": getattr(usage, "cached_tokens", 0),
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
                "cached_tokens": getattr(usage, "cached_tokens", 0),
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
                "cached_tokens": getattr(usage, "cached_tokens", 0),
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
    cached_tokens: int = 0
    month_start: Optional[datetime] = None


@dataclass(slots=True)
class DailyUsageAggregate:
    """日度模型用量聚合结果。"""

    ownerapp_id: str
    model_name: str
    call_count: int
    request_tokens: int
    response_tokens: int
    total_tokens: int
    cached_tokens: int = 0
    day_start: Optional[datetime] = None


@dataclass(slots=True)
class WeeklyUsageAggregate:
    """周度模型用量聚合结果。"""

    ownerapp_id: str
    model_name: str
    call_count: int
    request_tokens: int
    response_tokens: int
    total_tokens: int
    cached_tokens: int = 0
    week_start: Optional[datetime] = None


@dataclass(slots=True)
class YearlyUsageAggregate:
    """年度模型用量聚合结果。"""

    ownerapp_id: str
    model_name: str
    call_count: int
    request_tokens: int
    response_tokens: int
    total_tokens: int
    cached_tokens: int = 0
    year: Optional[int] = None


@dataclass(slots=True)
class YearlyUsageTotalAggregate:
    """年度模型用量总计聚合结果（按应用，不分模型）。"""

    ownerapp_id: str
    call_count: int
    request_tokens: int
    response_tokens: int
    total_tokens: int
    cached_tokens: int = 0
    year: Optional[int] = None


@dataclass(slots=True)
class MonthlyUsageTotalAggregate:
    """月度模型用量总计聚合结果（按应用，不分模型）。"""

    ownerapp_id: str
    call_count: int
    request_tokens: int
    response_tokens: int
    total_tokens: int
    cached_tokens: int = 0
    month_start: Optional[datetime] = None


def _coerce_model_type(model_type: ModelType | str) -> str:
    """转换模型类型为数据库存储值"""
    return model_type.value if isinstance(model_type, ModelType) else str(model_type)


def _normalize_node_model(node_model: NodeModel | None) -> NodeModel | None:
    """Normalize persisted node model enum fields before returning ORM records."""
    if node_model is None:
        return None
    if isinstance(node_model.model_type, str):
        try:
            node_model.model_type = ModelType(node_model.model_type)
        except ValueError:
            pass
    return node_model


def _normalize_node_models(node_models: Sequence[NodeModel]) -> list[NodeModel]:
    """Normalize a sequence of node model ORM records."""
    return [normalized for node_model in node_models if (normalized := _normalize_node_model(node_model)) is not None]


async def select_node_by_url(
    url: str,
    *,
    session: AsyncSession
) -> Node | None:
    """通过 URL 查询节点"""
    smts = select(Node).where(Node.url == url)
    result = await session.exec(smts)
    return result.first()


async def create_node_record(
    *,
    session: AsyncSession,
    node_payload: dict[str, Any],
) -> Node:
    """创建节点并刷新返回。"""
    node = Node.model_validate(node_payload)
    session.add(node)
    await session.commit()
    await session.refresh(node)
    return node


async def update_node_record(
    *,
    session: AsyncSession,
    node: Node,
    update_payload: dict[str, Any],
    updated_at: datetime,
) -> Node:
    """更新节点并刷新返回。"""
    for field, value in update_payload.items():
        setattr(node, field, value)
    node.updated_at = updated_at
    session.add(node)
    await session.commit()
    await session.refresh(node)
    return node


async def delete_node_record(
    *,
    session: AsyncSession,
    node: Node,
) -> None:
    """删除节点记录。"""
    await session.delete(node)
    await session.commit()


# ── 节点独立API密钥（NodeApiKey）CRUD ──────────────────────────────


async def create_node_api_key_record(
    *,
    session: AsyncSession,
    payload: dict[str, Any],
) -> NodeApiKey:
    """创建节点API密钥记录并刷新返回。

    Args:
        session: 异步数据库会话。
        payload: NodeApiKey 字段字典（api_key 需为已加密密文）。

    Returns:
        创建后的 NodeApiKey 记录。
    """
    node_api_key = NodeApiKey.model_validate(payload)
    session.add(node_api_key)
    await session.commit()
    await session.refresh(node_api_key)
    return node_api_key


async def select_node_api_key_by_id(
    api_key_id: str | UUID,
    *,
    session: AsyncSession,
) -> NodeApiKey | None:
    """按ID查询节点API密钥记录。"""
    identifier = UUID(str(api_key_id)) if not isinstance(api_key_id, UUID) else api_key_id
    smts = select(NodeApiKey).where(NodeApiKey.id == identifier)
    result = await session.exec(smts)
    return result.first()


async def select_node_api_key_by_hash(
    *,
    node_id: str | UUID,
    api_key_hash: str,
    session: AsyncSession,
) -> NodeApiKey | None:
    """按 node_id + api_key_hash 查找记录（upsert 用）。"""
    node_uuid = UUID(str(node_id)) if not isinstance(node_id, UUID) else node_id
    smts = select(NodeApiKey).where(
        NodeApiKey.node_id == node_uuid,
        NodeApiKey.api_key_hash == api_key_hash,
    )
    result = await session.exec(smts)
    return result.first()


async def select_node_api_keys_by_node_id(
    *,
    node_id: str | UUID,
    enabled: Optional[bool] = None,
    frozen: Optional[bool] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    session: AsyncSession,
) -> List[NodeApiKey]:
    """按节点ID查询API密钥列表（分页、enabled/frozen过滤）。

    Args:
        node_id: 节点ID。
        enabled: 启用状态过滤，None 表示不过滤。
        frozen: 冻结状态过滤，True=仅冻结中（frozen_until 非空且晚于 now），
            False=仅未冻结，None 表示不过滤。
        offset: 分页偏移。
        limit: 分页大小。
        session: 异步数据库会话。

    Returns:
        符合条件的 NodeApiKey 记录列表。
    """
    node_uuid = UUID(str(node_id)) if not isinstance(node_id, UUID) else node_id
    smts = select(NodeApiKey).where(NodeApiKey.node_id == node_uuid)
    if enabled is not None:
        smts = smts.where(NodeApiKey.enabled == enabled)  # noqa: E712
    if frozen is not None:
        now = datetime.now().astimezone()
        if frozen:
            smts = smts.where(
                NodeApiKey.frozen_until.is_not(None),
                NodeApiKey.frozen_until > now,
            )
        else:
            smts = smts.where(
                (NodeApiKey.frozen_until.is_(None)) | (NodeApiKey.frozen_until <= now)
            )
    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)
    smts = smts.order_by(NodeApiKey.created_at.asc())
    result = await session.exec(smts)
    return list(result.all())


async def select_node_api_keys_by_node_ids(
    *,
    node_ids: Sequence[str | UUID],
    session: AsyncSession,
) -> List[NodeApiKey]:
    """批量查询多个节点的全部API密钥记录（转发刷新用，防N+1）。"""
    if not node_ids:
        return []
    node_uuids = _ensure_uuid_list(node_ids)
    smts = select(NodeApiKey).where(NodeApiKey.node_id.in_(node_uuids))
    result = await session.exec(smts)
    return list(result.all())

async def select_node_api_keys(
    *,
    node_api_key_ids: Sequence[str | UUID] | None = None,
    node_ids: Sequence[str | UUID] | None = None,
    enabled: Optional[bool] = None,
    frozen: Optional[bool] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    session: AsyncSession,
) -> List[Tuple[NodeApiKey, Optional[str]]]:
    """跨节点全局查询API密钥列表并关联节点名称（分页、密钥ID/节点ID/enabled/frozen过滤）。

    通过 LEFT JOIN openaiapi_nodes 返回节点名称，节点被删除时名称为 None。

    Args:
        node_api_key_ids: 密钥记录ID列表过滤，None 表示不过滤。
        node_ids: 节点ID列表过滤，None 表示不过滤。
        enabled: 启用状态过滤，None 表示不过滤。
        frozen: 冻结状态过滤，True=仅冻结中（frozen_until 非空且晚于 now），
            False=仅未冻结，None 表示不过滤。
        offset: 分页偏移。
        limit: 分页大小。
        session: 异步数据库会话。

    Returns:
        (NodeApiKey 记录, 节点名称) 元组列表。
    """
    smts = select(NodeApiKey, Node.name).outerjoin(
        Node, NodeApiKey.node_id == Node.id
    )
    if node_api_key_ids:
        smts = smts.where(NodeApiKey.id.in_(_ensure_uuid_list(node_api_key_ids)))
    if node_ids:
        smts = smts.where(NodeApiKey.node_id.in_(_ensure_uuid_list(node_ids)))
    if enabled is not None:
        smts = smts.where(NodeApiKey.enabled == enabled)  # noqa: E712
    if frozen is not None:
        now = datetime.now().astimezone()
        if frozen:
            smts = smts.where(
                NodeApiKey.frozen_until.is_not(None),
                NodeApiKey.frozen_until > now,
            )
        else:
            smts = smts.where(
                (NodeApiKey.frozen_until.is_(None)) | (NodeApiKey.frozen_until <= now)
            )
    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)
    smts = smts.order_by(NodeApiKey.created_at.asc())
    result = await session.exec(smts)
    return [(row[0], row[1]) for row in result.all()]

async def count_node_api_keys(
    *,
    node_api_key_ids: Sequence[str | UUID] | None = None,
    node_ids: Sequence[str | UUID] | None = None,
    enabled: Optional[bool] = None,
    frozen: Optional[bool] = None,
    session: AsyncSession,
) -> int:
    """跨节点统计API密钥数量（过滤条件与 select_node_api_keys 一致）。"""
    smts = select(func.count(NodeApiKey.id))
    if node_api_key_ids:
        smts = smts.where(NodeApiKey.id.in_(_ensure_uuid_list(node_api_key_ids)))
    if node_ids:
        smts = smts.where(NodeApiKey.node_id.in_(_ensure_uuid_list(node_ids)))
    if enabled is not None:
        smts = smts.where(NodeApiKey.enabled == enabled)  # noqa: E712
    if frozen is not None:
        now = datetime.now().astimezone()
        if frozen:
            smts = smts.where(
                NodeApiKey.frozen_until.is_not(None),
                NodeApiKey.frozen_until > now,
            )
        else:
            smts = smts.where(
                (NodeApiKey.frozen_until.is_(None)) | (NodeApiKey.frozen_until <= now)
            )
    result = await session.exec(smts)
    return int(result.one() or 0)


async def count_node_api_keys_by_node_id(
    *,
    node_id: str | UUID,
    enabled: Optional[bool] = None,
    session: AsyncSession,
) -> int:
    """统计节点下API密钥数量。"""
    node_uuid = UUID(str(node_id)) if not isinstance(node_id, UUID) else node_id
    smts = select(func.count(NodeApiKey.id)).where(NodeApiKey.node_id == node_uuid)
    if enabled is not None:
        smts = smts.where(NodeApiKey.enabled == enabled)  # noqa: E712
    result = await session.exec(smts)
    return int(result.one() or 0)


async def update_node_api_key_record(
    *,
    session: AsyncSession,
    record: NodeApiKey,
    update_payload: dict[str, Any],
    updated_at: datetime,
) -> NodeApiKey:
    """更新节点API密钥记录并刷新返回。

    Args:
        session: 异步数据库会话。
        record: 待更新的记录。
        update_payload: 需要更新的字段字典。
        updated_at: 更新时间。

    Returns:
        更新后的记录。
    """
    for field, value in update_payload.items():
        setattr(record, field, value)
    record.updated_at = updated_at
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


async def delete_node_api_key_record(
    *,
    session: AsyncSession,
    record: NodeApiKey,
) -> None:
    """删除节点API密钥记录。"""
    await session.delete(record)
    await session.commit()


async def select_active_node_api_keys(
    *,
    node_id: str | UUID,
    session: AsyncSession,
) -> List[NodeApiKey]:
    """查询节点下所有可用密钥（enabled=True 且未过期未超额且未冻结），供转发选择使用。"""
    node_uuid = UUID(str(node_id)) if not isinstance(node_id, UUID) else node_id
    now = datetime.now().astimezone()
    smts = select(NodeApiKey).where(
        NodeApiKey.node_id == node_uuid,
        NodeApiKey.enabled == True,  # noqa: E712
        (NodeApiKey.expires_at.is_(None)) | (NodeApiKey.expires_at > now),
        (NodeApiKey.max_tokens.is_(None)) | (NodeApiKey.tokens_used < NodeApiKey.max_tokens),
        (NodeApiKey.frozen_until.is_(None)) | (NodeApiKey.frozen_until <= now),
    )
    result = await session.exec(smts)
    return list(result.all())


async def increment_node_api_key_tokens_used(
    *,
    session: AsyncSession,
    api_key_id: UUID,
    delta: int,
) -> None:
    """原子累加 tokens_used，避免并发竞争。

    Args:
        session: 异步数据库会话。
        api_key_id: 密钥记录ID。
        delta: 本次请求消耗的 total_tokens 增量。
    """
    if delta <= 0:
        return
    smts = (
        sa_update(NodeApiKey)
        .where(NodeApiKey.id == api_key_id)
        .values(
            tokens_used=NodeApiKey.tokens_used + delta,
            updated_at=current_time_in_timezone(),
        )
    )
    await session.exec(smts)  # type: ignore[call-overload]
    await session.commit()


async def disable_node_api_key(
    *,
    session: AsyncSession,
    api_key_id: UUID,
    reason: str,
    disabled_at: datetime,
) -> None:
    """自动禁用API密钥：enabled=False + 记录禁用时间与原因。"""
    smts = (
        sa_update(NodeApiKey)
        .where(NodeApiKey.id == api_key_id)
        .values(
            enabled=False,
            disabled_at=disabled_at,
            disable_reason=reason,
            updated_at=current_time_in_timezone(),
        )
    )
    await session.exec(smts)  # type: ignore[call-overload]
    await session.commit()


async def enable_node_api_key(
    *,
    session: AsyncSession,
    api_key_id: UUID,
) -> None:
    """重新启用API密钥：enabled=True 并清空禁用/冻结痕迹、重置已用Tokens。

    quota_next_reset_at 保留不动：它描述厂商的重置节奏，不因手动启用而改变；
    若已过期由刷新循环的 roll_node_api_key_quotas 自动前推。
    """
    smts = (
        sa_update(NodeApiKey)
        .where(NodeApiKey.id == api_key_id)
        .values(
            enabled=True,
            disabled_at=None,
            disable_reason=None,
            frozen_until=None,
            frozen_at=None,
            freeze_reason=None,
            tokens_used=0,
            updated_at=current_time_in_timezone(),
        )
    )
    await session.exec(smts)  # type: ignore[call-overload]
    await session.commit()


def advance_quota_reset_time(
    current: datetime,
    cycle: QuotaResetCycle,
    now: datetime,
) -> datetime:
    """将配额重置时间按周期逐次前推，直到严格晚于 now。

    daily/weekly 使用固定时长加法；monthly 使用日历月加法，
    目标月份天数不足时钳制到月末（如 1月31日 → 2月28/29日）。

    Args:
        current: 当前记录的重置时间（timezone-aware）。
        cycle: 重置周期枚举。
        now: 当前时间（timezone-aware）。

    Returns:
        严格晚于 now 的下一个重置时间；cycle 为 none 或非法时原样返回。
    """
    if cycle in (QuotaResetCycle.none, None) or current is None:
        return current
    advanced = current
    if cycle == QuotaResetCycle.daily:
        while advanced <= now:
            advanced += timedelta(days=1)
        return advanced
    if cycle == QuotaResetCycle.weekly:
        while advanced <= now:
            advanced += timedelta(weeks=1)
        return advanced
    if cycle == QuotaResetCycle.monthly:
        # 日历月加法：保留原始"几号"，短月钳制到月末
        anchor_day = current.day
        guard = 0
        while advanced <= now and guard < 1200:  # 防御性上限，约100年
            total_months = advanced.year * 12 + (advanced.month - 1) + 1
            target_year, target_month = divmod(total_months, 12)
            target_month += 1
            max_day = calendar.monthrange(target_year, target_month)[1]
            advanced = advanced.replace(
                year=target_year,
                month=target_month,
                day=min(anchor_day, max_day),
            )
            guard += 1
        return advanced
    return advanced


async def freeze_node_api_key(
    *,
    session: AsyncSession,
    api_key_id: UUID,
    reason: str,
    frozen_at: datetime,
    frozen_until: datetime,
    next_reset_at: Optional[datetime] = None,
) -> bool:
    """冻结API密钥至指定重置时间（多实例安全：首个冻结生效，后续 no-op）。

    仅当 enabled=True 且 frozen_until IS NULL 时写入，防止：
    1. 覆盖手动禁用状态；
    2. 其他实例后到的兜底冻结覆盖先到的厂商解析时间。

    Args:
        session: 异步数据库会话。
        api_key_id: 密钥记录ID。
        reason: 冻结原因。
        frozen_at: 冻结发生时间。
        frozen_until: 冻结截止时间（自动解冻时间点）。
        next_reset_at: 厂商确认的下次重置时间，非空时同步覆盖 quota_next_reset_at。

    Returns:
        bool: 是否实际写入（False 表示已被其他实例冻结或已手动禁用）。
    """
    values: dict[str, Any] = {
        "frozen_until": frozen_until,
        "frozen_at": frozen_at,
        "freeze_reason": reason,
        "updated_at": current_time_in_timezone(),
    }
    if next_reset_at is not None:
        values["quota_next_reset_at"] = next_reset_at
    smts = (
        sa_update(NodeApiKey)
        .where(
            NodeApiKey.id == api_key_id,
            NodeApiKey.enabled == True,  # noqa: E712
            NodeApiKey.frozen_until.is_(None),
        )
        .values(**values)
    )
    result = await session.exec(smts)  # type: ignore[call-overload]
    await session.commit()
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def unfreeze_node_api_key(
    *,
    session: AsyncSession,
    api_key_id: UUID,
    next_reset_at: Optional[datetime] = None,
) -> bool:
    """手动提前解冻API密钥（幂等：仅对冻结中的记录生效）。

    清空冻结三字段、tokens_used 归零；next_reset_at 非空时同步前推
    quota_next_reset_at（避免解冻后立即被跨周期重置任务再次处理）。

    Args:
        session: 异步数据库会话。
        api_key_id: 密钥记录ID。
        next_reset_at: 前推后的下次重置时间，None 表示不修改。

    Returns:
        bool: 是否实际解冻（False 表示记录未处于冻结状态）。
    """
    values: dict[str, Any] = {
        "frozen_until": None,
        "frozen_at": None,
        "freeze_reason": None,
        "tokens_used": 0,
        "updated_at": current_time_in_timezone(),
    }
    if next_reset_at is not None:
        values["quota_next_reset_at"] = next_reset_at
    smts = (
        sa_update(NodeApiKey)
        .where(
            NodeApiKey.id == api_key_id,
            NodeApiKey.frozen_until.is_not(None),
        )
        .values(**values)
    )
    result = await session.exec(smts)  # type: ignore[call-overload]
    await session.commit()
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def roll_node_api_key_quotas(
    *,
    session: AsyncSession,
    now: datetime,
) -> Tuple[List[UUID], List[UUID]]:
    """刷新循环统一任务：A 到期解冻 + B 跨周期例行重置（多实例安全）。

    A 类：frozen_until <= now 的冻结记录 → 解冻、tokens 归零、
         quota_next_reset_at 从 frozen_until 逐周期前推至未来。
         条件 UPDATE 幂等，多实例并发时只有一个实例命中行。
    B 类：未冻结但 quota_next_reset_at <= now 的周期密钥 → tokens 归零、
         重置时间逐周期前推。使用 CAS（WHERE quota_next_reset_at = 旧值）
         防止多实例重复前推。

    Args:
        session: 异步数据库会话。
        now: 当前评估时间（timezone-aware）。

    Returns:
        (解冻的密钥ID列表, 跨周期重置的密钥ID列表)，供调用方失效节点配置指纹。
    """
    unfrozen_ids: List[UUID] = []
    rolled_ids: List[UUID] = []

    # ── A 类：到期解冻 ──
    due_stmt = select(
        NodeApiKey.id,
        NodeApiKey.frozen_until,
        NodeApiKey.quota_reset_cycle,
    ).where(
        NodeApiKey.frozen_until.is_not(None),
        NodeApiKey.frozen_until <= now,
    )
    due_rows = (await session.exec(due_stmt)).all()
    for row in due_rows:
        key_id, frozen_until, cycle_value = row[0], row[1], row[2]
        cycle = cycle_value if isinstance(cycle_value, QuotaResetCycle) else QuotaResetCycle(cycle_value)
        frozen_at_time = frozen_until
        if frozen_at_time.tzinfo is None:
            frozen_at_time = frozen_at_time.replace(tzinfo=now.tzinfo)
        new_next_reset = advance_quota_reset_time(frozen_at_time, cycle, now)
        # 条件 UPDATE：仍冻结且 frozen_until 未变，幂等防多实例重复
        unfreeze_stmt = (
            sa_update(NodeApiKey)
            .where(
                NodeApiKey.id == key_id,
                NodeApiKey.frozen_until == frozen_until,
            )
            .values(
                frozen_until=None,
                frozen_at=None,
                freeze_reason=None,
                tokens_used=0,
                quota_next_reset_at=new_next_reset,
                updated_at=current_time_in_timezone(),
            )
        )
        result = await session.exec(unfreeze_stmt)  # type: ignore[call-overload]
        if int(getattr(result, "rowcount", 0) or 0) > 0:
            unfrozen_ids.append(key_id)
    if due_rows:
        await session.commit()

    # ── B 类：未冻结但已跨周期 → 例行重置计数 ──
    stale_stmt = select(
        NodeApiKey.id,
        NodeApiKey.quota_next_reset_at,
        NodeApiKey.quota_reset_cycle,
    ).where(
        NodeApiKey.quota_reset_cycle != QuotaResetCycle.none.value,
        NodeApiKey.enabled == True,  # noqa: E712
        NodeApiKey.frozen_until.is_(None),
        NodeApiKey.quota_next_reset_at.is_not(None),
        NodeApiKey.quota_next_reset_at <= now,
    )
    stale_rows = (await session.exec(stale_stmt)).all()
    for row in stale_rows:
        key_id, old_next_reset, cycle_value = row[0], row[1], row[2]
        cycle = cycle_value if isinstance(cycle_value, QuotaResetCycle) else QuotaResetCycle(cycle_value)
        old_reset_time = old_next_reset
        if old_reset_time.tzinfo is None:
            old_reset_time = old_reset_time.replace(tzinfo=now.tzinfo)
        new_next_reset = advance_quota_reset_time(old_reset_time, cycle, now)
        # CAS：quota_next_reset_at 仍为旧值才更新，防多实例重复前推
        roll_stmt = (
            sa_update(NodeApiKey)
            .where(
                NodeApiKey.id == key_id,
                NodeApiKey.quota_next_reset_at == old_next_reset,
                NodeApiKey.frozen_until.is_(None),
            )
            .values(
                tokens_used=0,
                quota_next_reset_at=new_next_reset,
                updated_at=current_time_in_timezone(),
            )
        )
        result = await session.exec(roll_stmt)  # type: ignore[call-overload]
        if int(getattr(result, "rowcount", 0) or 0) > 0:
            rolled_ids.append(key_id)
    if stale_rows:
        await session.commit()

    return unfrozen_ids, rolled_ids


async def upsert_legacy_node_with_models(
    *,
    session: AsyncSession,
    node_url: str,
    node_type: str | None,
    encrypted_api_key: str | None,
    health_check: bool | None,
    trusted_without_models_endpoint: bool | None,
    auto_v1_api: bool | None,
    protocol_type: ProtocolType,
    request_proxy_url: str | None,
    available: bool | None,
    updated_at: datetime,
    model_names: list[str],
    model_type: ModelType,
) -> Node:
    """按遗留节点状态载荷创建或更新节点及其模型。"""
    db_node = await select_node_by_url(node_url, session=session)
    if db_node:
        if encrypted_api_key is not None:
            db_node.api_key = encrypted_api_key
        if health_check is not None:
            db_node.health_check = health_check
        if trusted_without_models_endpoint is not None:
            db_node.trusted_without_models_endpoint = trusted_without_models_endpoint
        if auto_v1_api is not None:
            db_node.auto_v1_api = auto_v1_api
        db_node.protocol_type = protocol_type
        db_node.request_proxy_url = request_proxy_url
        if available is not None:
            db_node.enabled = bool(available)
        if node_type and not db_node.name:
            db_node.name = node_type
        db_node.updated_at = updated_at
        session.add(db_node)
    else:
        db_node = Node(
            url=node_url,
            name=node_type,
            api_key=encrypted_api_key,
            health_check=health_check if health_check is not None else True,
            trusted_without_models_endpoint=bool(trusted_without_models_endpoint),
            auto_v1_api=True if auto_v1_api is None else bool(auto_v1_api),
            protocol_type=protocol_type,
            request_proxy_url=request_proxy_url,
            enabled=bool(available) if available is not None else True,
            created_at=updated_at,
            updated_at=updated_at,
        )
        session.add(db_node)
        await session.flush()

    seen_models: set[str] = set()
    for model_name in model_names:
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
            if available is not None:
                existed_model.enabled = bool(available)
                session.add(existed_model)
            continue

        session.add(
            NodeModel(
                node_id=db_node.id,
                model_name=model_name,
                model_type=model_type,
                enabled=bool(available) if available is not None else True,
            )
        )

    await session.commit()
    await session.refresh(db_node)
    return db_node


async def disable_node_and_models_by_url(
    *,
    session: AsyncSession,
    node_url: str,
    updated_at: datetime,
) -> bool:
    """按 URL 禁用节点及其关联模型。"""
    db_node = await select_node_by_url(node_url, session=session)
    if db_node is None:
        return False

    db_node.enabled = False
    db_node.updated_at = updated_at
    session.add(db_node)

    models = await select_node_models(node_ids=db_node.id, session=session)
    for model in models:
        if model.enabled:
            model.enabled = False
            session.add(model)

    await session.commit()
    return True


async def update_node_reason_by_url(
    *,
    session: AsyncSession,
    node_url: str,
    reason: Optional[str],
    updated_at: datetime,
) -> bool:
    """按 URL 更新节点不可用原因。"""
    db_node = await select_node_by_url(node_url, session=session)
    if db_node is None:
        return False

    db_node.reason = reason
    db_node.updated_at = updated_at
    session.add(db_node)

    await session.commit()
    return True

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
    return _normalize_node_model(result.first())


async def create_node_model_record(
    *,
    session: AsyncSession,
    model_payload: dict[str, Any],
) -> NodeModel:
    """创建节点模型并刷新返回。"""
    node_model = NodeModel.model_validate(model_payload)
    session.add(node_model)
    await session.commit()
    await session.refresh(node_model)
    return _normalize_node_model(node_model)


async def update_node_model_record(
    *,
    session: AsyncSession,
    node_model: NodeModel,
    update_payload: dict[str, Any],
) -> NodeModel:
    """更新节点模型并刷新返回。"""
    for field, value in update_payload.items():
        setattr(node_model, field, value)
    session.add(node_model)
    await session.commit()
    await session.refresh(node_model)
    return _normalize_node_model(node_model)


async def delete_node_model_record(
    *,
    session: AsyncSession,
    node_model: NodeModel,
) -> None:
    """删除节点模型。"""
    await session.delete(node_model)
    await session.commit()


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
    return _normalize_node_model(result.first())


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
    return _normalize_node_models(result.all())


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


async def create_node_model_quota_record(
    *,
    session: AsyncSession,
    quota_payload: dict[str, Any],
) -> NodeModelQuota:
    """创建节点模型配额并刷新返回。"""
    quota = NodeModelQuota.model_validate(quota_payload)
    session.add(quota)
    await session.commit()
    await session.refresh(quota)
    return quota


async def update_node_model_quota_record(
    *,
    session: AsyncSession,
    quota: NodeModelQuota,
    update_payload: dict[str, Any],
    updated_at: datetime,
) -> NodeModelQuota:
    """更新节点模型配额并刷新返回。"""
    for field, value in update_payload.items():
        setattr(quota, field, value)
    quota.updated_at = updated_at
    session.add(quota)
    await session.commit()
    await session.refresh(quota)
    return quota


async def expire_node_model_quota_record(
    *,
    session: AsyncSession,
    quota: NodeModelQuota,
    expired_at: datetime,
) -> NodeModelQuota:
    """软删除节点模型配额并刷新返回。"""
    quota.expired_at = quota.expired_at or expired_at
    quota.updated_at = expired_at
    session.add(quota)
    await session.commit()
    await session.refresh(quota)
    return quota


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
            func.coalesce(func.sum(ProxyNodeStatusLog.cached_tokens), 0),
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
    for ownerapp_id, model_name, call_count, request_tokens, response_tokens, total_tokens, cached_tokens in rows:
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
                cached_tokens=int(cached_tokens or 0),
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
            func.coalesce(func.sum(ProxyNodeStatusLog.cached_tokens), 0),
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
            cached_tokens=int(cached_tokens or 0),
        )
        for ownerapp_id, model_name, call_count, request_tokens, response_tokens, total_tokens, cached_tokens in rows
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
            func.coalesce(func.sum(ProxyNodeStatusLog.cached_tokens), 0),
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
            cached_tokens=int(cached_tokens or 0),
        )
        for ownerapp_id, model_name, call_count, request_tokens, response_tokens, total_tokens, cached_tokens in rows
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
            func.coalesce(func.sum(AppMonthlyModelUsage.cached_tokens), 0),
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
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_model_name,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
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
            func.coalesce(func.sum(AppMonthlyModelUsage.cached_tokens), 0),
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
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
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
            func.coalesce(func.sum(AppMonthlyModelUsage.cached_tokens), 0),
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
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
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


# ── 实时查询与合并辅助函数 ──────────────────────────────────────────


async def select_realtime_model_usages(
    *,
    start_time: datetime,
    end_time: Optional[datetime] = None,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[DailyUsageAggregate]:
    """从 ProxyNodeStatusLog 实时聚合指定时间区间内的应用模型用量。

    Args:
        start_time: 区间起始时间（含）。
        end_time: 区间结束时间（不含），为 None 则不限。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按应用+模型分组的用量聚合列表。
    """

    smts = (
        select(
            ProxyNodeStatusLog.ownerapp_id,
            ProxyNodeStatusLog.model_name,
            func.count(ProxyNodeStatusLog.id),
            func.coalesce(func.sum(ProxyNodeStatusLog.request_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.response_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.total_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.cached_tokens), 0),
        )
        .where(
            ProxyNodeStatusLog.end_at.is_not(None),
            ProxyNodeStatusLog.start_at >= start_time,
            ProxyNodeStatusLog.ownerapp_id.is_not(None),
            ProxyNodeStatusLog.model_name.is_not(None),
        )
        .group_by(ProxyNodeStatusLog.ownerapp_id, ProxyNodeStatusLog.model_name)
    )

    if end_time is not None:
        smts = smts.where(ProxyNodeStatusLog.start_at < end_time)

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(ProxyNodeStatusLog.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(ProxyNodeStatusLog.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(ProxyNodeStatusLog.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        DailyUsageAggregate(
            ownerapp_id=str(row_ownerapp_id),
            model_name=str(row_model_name),
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_model_name,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id and row_model_name
    ]


async def select_realtime_model_usage_totals(
    *,
    start_time: datetime,
    end_time: Optional[datetime] = None,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[MonthlyUsageTotalAggregate]:
    """从 ProxyNodeStatusLog 实时聚合指定时间区间内的应用用量总计（不分模型）。

    Args:
        start_time: 区间起始时间（含）。
        end_time: 区间结束时间（不含），为 None 则不限。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按应用分组的用量总计列表。
    """

    smts = (
        select(
            ProxyNodeStatusLog.ownerapp_id,
            func.coalesce(func.count(ProxyNodeStatusLog.id), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.request_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.response_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.total_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.cached_tokens), 0),
        )
        .where(
            ProxyNodeStatusLog.end_at.is_not(None),
            ProxyNodeStatusLog.start_at >= start_time,
            ProxyNodeStatusLog.ownerapp_id.is_not(None),
        )
        .group_by(ProxyNodeStatusLog.ownerapp_id)
    )

    if end_time is not None:
        smts = smts.where(ProxyNodeStatusLog.start_at < end_time)

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(ProxyNodeStatusLog.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(ProxyNodeStatusLog.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(ProxyNodeStatusLog.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        MonthlyUsageTotalAggregate(
            ownerapp_id=str(row_ownerapp_id),
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id
    ]


async def select_app_daily_model_usages_range(
    *,
    day_start: datetime,
    day_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[DailyUsageAggregate]:
    """从 AppDailyModelUsage 表查询指定日期区间内的用量（按应用+模型分组汇总）。

    Args:
        day_start: 起始日期（含）。
        day_end: 结束日期（不含）。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按应用+模型分组的用量聚合列表。
    """

    smts = (
        select(
            AppDailyModelUsage.ownerapp_id,
            AppDailyModelUsage.model_name,
            func.coalesce(func.sum(AppDailyModelUsage.call_count), 0),
            func.coalesce(func.sum(AppDailyModelUsage.request_tokens), 0),
            func.coalesce(func.sum(AppDailyModelUsage.response_tokens), 0),
            func.coalesce(func.sum(AppDailyModelUsage.total_tokens), 0),
            func.coalesce(func.sum(AppDailyModelUsage.cached_tokens), 0),
        )
        .where(
            AppDailyModelUsage.day_start >= day_start,
            AppDailyModelUsage.day_start < day_end,
        )
        .group_by(AppDailyModelUsage.ownerapp_id, AppDailyModelUsage.model_name)
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppDailyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppDailyModelUsage.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(AppDailyModelUsage.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        DailyUsageAggregate(
            ownerapp_id=str(row_ownerapp_id),
            model_name=str(row_model_name),
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_model_name,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id and row_model_name
    ]


async def select_app_daily_model_usage_totals_range(
    *,
    day_start: datetime,
    day_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[MonthlyUsageTotalAggregate]:
    """从 AppDailyModelUsage 表查询指定日期区间内的用量总计（按应用分组汇总）。

    Args:
        day_start: 起始日期（含）。
        day_end: 结束日期（不含）。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按应用分组的用量总计列表。
    """

    smts = (
        select(
            AppDailyModelUsage.ownerapp_id,
            func.coalesce(func.sum(AppDailyModelUsage.call_count), 0),
            func.coalesce(func.sum(AppDailyModelUsage.request_tokens), 0),
            func.coalesce(func.sum(AppDailyModelUsage.response_tokens), 0),
            func.coalesce(func.sum(AppDailyModelUsage.total_tokens), 0),
            func.coalesce(func.sum(AppDailyModelUsage.cached_tokens), 0),
        )
        .where(
            AppDailyModelUsage.day_start >= day_start,
            AppDailyModelUsage.day_start < day_end,
        )
        .group_by(AppDailyModelUsage.ownerapp_id)
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppDailyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppDailyModelUsage.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(AppDailyModelUsage.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        MonthlyUsageTotalAggregate(
            ownerapp_id=str(row_ownerapp_id),
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id
    ]


async def select_app_monthly_model_usages_range(
    *,
    month_start: datetime,
    month_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[MonthlyUsageAggregate]:
    """从 AppMonthlyModelUsage 表查询指定月份区间内的用量（按应用+模型分组汇总）。

    Args:
        month_start: 起始月份（含）。
        month_end: 结束月份（不含）。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按应用+模型分组的用量聚合列表。
    """

    smts = (
        select(
            AppMonthlyModelUsage.ownerapp_id,
            AppMonthlyModelUsage.model_name,
            func.coalesce(func.sum(AppMonthlyModelUsage.call_count), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.request_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.response_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.total_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.cached_tokens), 0),
        )
        .where(
            AppMonthlyModelUsage.month_start >= month_start,
            AppMonthlyModelUsage.month_start < month_end,
        )
        .group_by(AppMonthlyModelUsage.ownerapp_id, AppMonthlyModelUsage.model_name)
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(AppMonthlyModelUsage.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        MonthlyUsageAggregate(
            ownerapp_id=str(row_ownerapp_id),
            model_name=str(row_model_name),
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_model_name,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id and row_model_name
    ]


async def select_app_monthly_model_usage_totals_range(
    *,
    month_start: datetime,
    month_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[MonthlyUsageTotalAggregate]:
    """从 AppMonthlyModelUsage 表查询指定月份区间内的用量总计（按应用分组汇总）。

    Args:
        month_start: 起始月份（含）。
        month_end: 结束月份（不含）。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按应用分组的用量总计列表。
    """

    smts = (
        select(
            AppMonthlyModelUsage.ownerapp_id,
            func.coalesce(func.sum(AppMonthlyModelUsage.call_count), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.request_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.response_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.total_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.cached_tokens), 0),
        )
        .where(
            AppMonthlyModelUsage.month_start >= month_start,
            AppMonthlyModelUsage.month_start < month_end,
        )
        .group_by(AppMonthlyModelUsage.ownerapp_id)
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(AppMonthlyModelUsage.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        MonthlyUsageTotalAggregate(
            ownerapp_id=str(row_ownerapp_id),
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id
    ]


def _merge_model_aggregates(
    *aggregate_lists: list,
) -> list[DailyUsageAggregate]:
    """合并多个按应用+模型分组的用量聚合列表，相同键的条目累加。

    Args:
        *aggregate_lists: 多个聚合列表，元素需含 ownerapp_id 和 model_name 属性。

    Returns:
        合并后的用量聚合列表。
    """

    merged: dict[tuple[str, str], DailyUsageAggregate] = {}
    for aggregate_list in aggregate_lists:
        for item in aggregate_list:
            key = (item.ownerapp_id, item.model_name)
            if key in merged:
                existing = merged[key]
                merged[key] = DailyUsageAggregate(
                    ownerapp_id=existing.ownerapp_id,
                    model_name=existing.model_name,
                    call_count=existing.call_count + item.call_count,
                    request_tokens=existing.request_tokens + item.request_tokens,
                    response_tokens=existing.response_tokens + item.response_tokens,
                    total_tokens=existing.total_tokens + item.total_tokens,
                    cached_tokens=getattr(existing, 'cached_tokens', 0) + getattr(item, 'cached_tokens', 0),
                )
            else:
                merged[key] = DailyUsageAggregate(
                    ownerapp_id=item.ownerapp_id,
                    model_name=item.model_name,
                    call_count=item.call_count,
                    request_tokens=item.request_tokens,
                    response_tokens=item.response_tokens,
                    total_tokens=item.total_tokens,
                    cached_tokens=getattr(item, 'cached_tokens', 0),
                )
    return list(merged.values())


def _merge_total_aggregates(
    *aggregate_lists: list[MonthlyUsageTotalAggregate],
) -> list[MonthlyUsageTotalAggregate]:
    """合并多个按应用分组的用量总计列表，相同应用的条目累加。

    Args:
        *aggregate_lists: 多个总计列表，元素需含 ownerapp_id 属性。

    Returns:
        合并后的用量总计列表。
    """

    merged: dict[str, MonthlyUsageTotalAggregate] = {}
    for aggregate_list in aggregate_lists:
        for item in aggregate_list:
            key = item.ownerapp_id
            if key in merged:
                existing = merged[key]
                merged[key] = MonthlyUsageTotalAggregate(
                    ownerapp_id=existing.ownerapp_id,
                    call_count=existing.call_count + item.call_count,
                    request_tokens=existing.request_tokens + item.request_tokens,
                    response_tokens=existing.response_tokens + item.response_tokens,
                    total_tokens=existing.total_tokens + item.total_tokens,
                    cached_tokens=getattr(existing, 'cached_tokens', 0) + getattr(item, 'cached_tokens', 0),
                )
            else:
                merged[key] = MonthlyUsageTotalAggregate(
                    ownerapp_id=item.ownerapp_id,
                    call_count=item.call_count,
                    request_tokens=item.request_tokens,
                    response_tokens=item.response_tokens,
                    total_tokens=item.total_tokens,
                    cached_tokens=getattr(item, 'cached_tokens', 0),
                )
    return list(merged.values())


# ── 按周期分组的范围查询函数 ────────────────────────────────────────


async def select_app_daily_model_usages_by_range(
    *,
    day_start: datetime,
    day_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[DailyUsageAggregate]:
    """从 AppDailyModelUsage 表查询指定日期区间内的用量，按天+应用+模型分组返回。

    Args:
        day_start: 起始日期（含）。
        day_end: 结束日期（不含）。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按天+应用+模型分组的用量聚合列表，每条记录包含 day_start 字段。
    """

    smts = (
        select(
            AppDailyModelUsage.ownerapp_id,
            AppDailyModelUsage.model_name,
            AppDailyModelUsage.day_start,
            func.coalesce(func.sum(AppDailyModelUsage.call_count), 0),
            func.coalesce(func.sum(AppDailyModelUsage.request_tokens), 0),
            func.coalesce(func.sum(AppDailyModelUsage.response_tokens), 0),
            func.coalesce(func.sum(AppDailyModelUsage.total_tokens), 0),
            func.coalesce(func.sum(AppDailyModelUsage.cached_tokens), 0),
        )
        .where(
            AppDailyModelUsage.day_start >= day_start,
            AppDailyModelUsage.day_start < day_end,
        )
        .group_by(
            AppDailyModelUsage.ownerapp_id,
            AppDailyModelUsage.model_name,
            AppDailyModelUsage.day_start,
        )
        .order_by(AppDailyModelUsage.day_start.asc())
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppDailyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppDailyModelUsage.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(AppDailyModelUsage.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        DailyUsageAggregate(
            ownerapp_id=str(row_ownerapp_id),
            model_name=str(row_model_name),
            day_start=row_day_start,
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_model_name,
            row_day_start,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id and row_model_name
    ]


async def select_app_daily_model_usage_totals_by_range(
    *,
    day_start: datetime,
    day_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[MonthlyUsageTotalAggregate]:
    """从 AppDailyModelUsage 表查询指定日期区间内的用量总计，按天+应用分组返回。

    Args:
        day_start: 起始日期（含）。
        day_end: 结束日期（不含）。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按天+应用分组的用量总计列表，每条记录包含 month_start 字段（实际为 day_start）。
    """

    smts = (
        select(
            AppDailyModelUsage.ownerapp_id,
            AppDailyModelUsage.day_start,
            func.coalesce(func.sum(AppDailyModelUsage.call_count), 0),
            func.coalesce(func.sum(AppDailyModelUsage.request_tokens), 0),
            func.coalesce(func.sum(AppDailyModelUsage.response_tokens), 0),
            func.coalesce(func.sum(AppDailyModelUsage.total_tokens), 0),
            func.coalesce(func.sum(AppDailyModelUsage.cached_tokens), 0),
        )
        .where(
            AppDailyModelUsage.day_start >= day_start,
            AppDailyModelUsage.day_start < day_end,
        )
        .group_by(
            AppDailyModelUsage.ownerapp_id,
            AppDailyModelUsage.day_start,
        )
        .order_by(AppDailyModelUsage.day_start.asc())
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppDailyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppDailyModelUsage.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(AppDailyModelUsage.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        MonthlyUsageTotalAggregate(
            ownerapp_id=str(row_ownerapp_id),
            month_start=row_day_start,
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_day_start,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id
    ]


async def select_app_weekly_model_usages_by_range(
    *,
    week_start: datetime,
    week_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[WeeklyUsageAggregate]:
    """从 AppWeeklyModelUsage 表查询指定周区间内的用量，按周+应用+模型分组返回。

    Args:
        week_start: 起始周（含）。
        week_end: 结束周（不含）。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按周+应用+模型分组的用量聚合列表，每条记录包含 week_start 字段。
    """

    smts = (
        select(
            AppWeeklyModelUsage.ownerapp_id,
            AppWeeklyModelUsage.model_name,
            AppWeeklyModelUsage.week_start,
            func.coalesce(func.sum(AppWeeklyModelUsage.call_count), 0),
            func.coalesce(func.sum(AppWeeklyModelUsage.request_tokens), 0),
            func.coalesce(func.sum(AppWeeklyModelUsage.response_tokens), 0),
            func.coalesce(func.sum(AppWeeklyModelUsage.total_tokens), 0),
            func.coalesce(func.sum(AppWeeklyModelUsage.cached_tokens), 0),
        )
        .where(
            AppWeeklyModelUsage.week_start >= week_start,
            AppWeeklyModelUsage.week_start < week_end,
        )
        .group_by(
            AppWeeklyModelUsage.ownerapp_id,
            AppWeeklyModelUsage.model_name,
            AppWeeklyModelUsage.week_start,
        )
        .order_by(AppWeeklyModelUsage.week_start.asc())
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppWeeklyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppWeeklyModelUsage.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(AppWeeklyModelUsage.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        WeeklyUsageAggregate(
            ownerapp_id=str(row_ownerapp_id),
            model_name=str(row_model_name),
            week_start=row_week_start,
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_model_name,
            row_week_start,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id and row_model_name
    ]


async def select_app_weekly_model_usage_totals_by_range(
    *,
    week_start: datetime,
    week_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[MonthlyUsageTotalAggregate]:
    """从 AppWeeklyModelUsage 表查询指定周区间内的用量总计，按周+应用分组返回。

    Args:
        week_start: 起始周（含）。
        week_end: 结束周（不含）。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按周+应用分组的用量总计列表，每条记录包含 month_start 字段（实际为 week_start）。
    """

    smts = (
        select(
            AppWeeklyModelUsage.ownerapp_id,
            AppWeeklyModelUsage.week_start,
            func.coalesce(func.sum(AppWeeklyModelUsage.call_count), 0),
            func.coalesce(func.sum(AppWeeklyModelUsage.request_tokens), 0),
            func.coalesce(func.sum(AppWeeklyModelUsage.response_tokens), 0),
            func.coalesce(func.sum(AppWeeklyModelUsage.total_tokens), 0),
            func.coalesce(func.sum(AppWeeklyModelUsage.cached_tokens), 0),
        )
        .where(
            AppWeeklyModelUsage.week_start >= week_start,
            AppWeeklyModelUsage.week_start < week_end,
        )
        .group_by(
            AppWeeklyModelUsage.ownerapp_id,
            AppWeeklyModelUsage.week_start,
        )
        .order_by(AppWeeklyModelUsage.week_start.asc())
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppWeeklyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppWeeklyModelUsage.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(AppWeeklyModelUsage.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        MonthlyUsageTotalAggregate(
            ownerapp_id=str(row_ownerapp_id),
            month_start=row_week_start,
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_week_start,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id
    ]


async def select_app_monthly_model_usages_by_range(
    *,
    month_start: datetime,
    month_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[MonthlyUsageAggregate]:
    """从 AppMonthlyModelUsage 表查询指定月份区间内的用量，按月+应用+模型分组返回。

    Args:
        month_start: 起始月份（含）。
        month_end: 结束月份（不含）。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按月+应用+模型分组的用量聚合列表，每条记录包含 month_start 字段。
    """

    smts = (
        select(
            AppMonthlyModelUsage.ownerapp_id,
            AppMonthlyModelUsage.model_name,
            AppMonthlyModelUsage.month_start,
            func.coalesce(func.sum(AppMonthlyModelUsage.call_count), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.request_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.response_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.total_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.cached_tokens), 0),
        )
        .where(
            AppMonthlyModelUsage.month_start >= month_start,
            AppMonthlyModelUsage.month_start < month_end,
        )
        .group_by(
            AppMonthlyModelUsage.ownerapp_id,
            AppMonthlyModelUsage.model_name,
            AppMonthlyModelUsage.month_start,
        )
        .order_by(AppMonthlyModelUsage.month_start.asc())
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(AppMonthlyModelUsage.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        MonthlyUsageAggregate(
            ownerapp_id=str(row_ownerapp_id),
            model_name=str(row_model_name),
            month_start=row_month_start,
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_model_name,
            row_month_start,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id and row_model_name
    ]


async def select_app_monthly_model_usage_totals_by_range(
    *,
    month_start: datetime,
    month_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[MonthlyUsageTotalAggregate]:
    """从 AppMonthlyModelUsage 表查询指定月份区间内的用量总计，按月+应用分组返回。

    Args:
        month_start: 起始月份（含）。
        month_end: 结束月份（不含）。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按月+应用分组的用量总计列表，每条记录包含 month_start 字段。
    """

    smts = (
        select(
            AppMonthlyModelUsage.ownerapp_id,
            AppMonthlyModelUsage.month_start,
            func.coalesce(func.sum(AppMonthlyModelUsage.call_count), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.request_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.response_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.total_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.cached_tokens), 0),
        )
        .where(
            AppMonthlyModelUsage.month_start >= month_start,
            AppMonthlyModelUsage.month_start < month_end,
        )
        .group_by(
            AppMonthlyModelUsage.ownerapp_id,
            AppMonthlyModelUsage.month_start,
        )
        .order_by(AppMonthlyModelUsage.month_start.asc())
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(AppMonthlyModelUsage.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        MonthlyUsageTotalAggregate(
            ownerapp_id=str(row_ownerapp_id),
            month_start=row_month_start,
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_month_start,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id
    ]


async def select_app_yearly_model_usages_by_range(
    *,
    year_start: int,
    year_end: int,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[YearlyUsageAggregate]:
    """从 AppMonthlyModelUsage 表查询指定年份区间内的用量，按年+应用+模型分组返回。

    Args:
        year_start: 起始年份（含）。
        year_end: 结束年份（不含）。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按年+应用+模型分组的用量聚合列表，每条记录包含 year 字段。
    """

    # 提取年份用于分组
    year_expr = func.extract("year", AppMonthlyModelUsage.month_start).label("year")
    smts = (
        select(
            AppMonthlyModelUsage.ownerapp_id,
            AppMonthlyModelUsage.model_name,
            year_expr,
            func.coalesce(func.sum(AppMonthlyModelUsage.call_count), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.request_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.response_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.total_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.cached_tokens), 0),
        )
        .where(
            func.extract("year", AppMonthlyModelUsage.month_start) >= year_start,
            func.extract("year", AppMonthlyModelUsage.month_start) < year_end,
        )
        .group_by(
            AppMonthlyModelUsage.ownerapp_id,
            AppMonthlyModelUsage.model_name,
            year_expr,
        )
        .order_by(year_expr.asc())
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(AppMonthlyModelUsage.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        YearlyUsageAggregate(
            ownerapp_id=str(row_ownerapp_id),
            model_name=str(row_model_name),
            year=int(row_year),
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_model_name,
            row_year,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id and row_model_name
    ]


async def select_app_yearly_model_usage_totals_by_range(
    *,
    year_start: int,
    year_end: int,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[YearlyUsageTotalAggregate]:
    """从 AppMonthlyModelUsage 表查询指定年份区间内的用量总计，按年+应用分组返回。

    Args:
        year_start: 起始年份（含）。
        year_end: 结束年份（不含）。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按年+应用分组的用量总计列表，每条记录包含 year 字段。
    """

    year_expr = func.extract("year", AppMonthlyModelUsage.month_start).label("year")
    smts = (
        select(
            AppMonthlyModelUsage.ownerapp_id,
            year_expr,
            func.coalesce(func.sum(AppMonthlyModelUsage.call_count), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.request_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.response_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.total_tokens), 0),
            func.coalesce(func.sum(AppMonthlyModelUsage.cached_tokens), 0),
        )
        .where(
            func.extract("year", AppMonthlyModelUsage.month_start) >= year_start,
            func.extract("year", AppMonthlyModelUsage.month_start) < year_end,
        )
        .group_by(
            AppMonthlyModelUsage.ownerapp_id,
            year_expr,
        )
        .order_by(year_expr.asc())
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(AppMonthlyModelUsage.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(AppMonthlyModelUsage.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        YearlyUsageTotalAggregate(
            ownerapp_id=str(row_ownerapp_id),
            year=int(row_year),
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_year,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id
    ]


async def select_realtime_model_usages_by_day(
    *,
    day_start: datetime,
    day_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[DailyUsageAggregate]:
    """从 ProxyNodeStatusLog 实时聚合指定日期区间内的用量，按天+应用+模型分组返回。

    Args:
        day_start: 起始日期（含）。
        day_end: 结束日期（不含）。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按天+应用+模型分组的用量聚合列表，每条记录包含 day_start 字段。
    """

    day_expr = func.date(ProxyNodeStatusLog.start_at).label("day")
    smts = (
        select(
            ProxyNodeStatusLog.ownerapp_id,
            ProxyNodeStatusLog.model_name,
            day_expr,
            func.count(ProxyNodeStatusLog.id),
            func.coalesce(func.sum(ProxyNodeStatusLog.request_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.response_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.total_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.cached_tokens), 0),
        )
        .where(
            ProxyNodeStatusLog.end_at.is_not(None),
            ProxyNodeStatusLog.start_at >= day_start,
            ProxyNodeStatusLog.start_at < day_end,
            ProxyNodeStatusLog.ownerapp_id.is_not(None),
            ProxyNodeStatusLog.model_name.is_not(None),
        )
        .group_by(
            ProxyNodeStatusLog.ownerapp_id,
            ProxyNodeStatusLog.model_name,
            day_expr,
        )
        .order_by(day_expr.asc())
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(ProxyNodeStatusLog.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(ProxyNodeStatusLog.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(ProxyNodeStatusLog.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        DailyUsageAggregate(
            ownerapp_id=str(row_ownerapp_id),
            model_name=str(row_model_name),
            day_start=row_day,
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_model_name,
            row_day,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id and row_model_name
    ]


async def select_realtime_model_usage_totals_by_day(
    *,
    day_start: datetime,
    day_end: datetime,
    ownerapp_id: Optional[str] = None,
    model_names: Optional[list[str]] = None,
    session: AsyncSession,
) -> list[MonthlyUsageTotalAggregate]:
    """从 ProxyNodeStatusLog 实时聚合指定日期区间内的用量总计，按天+应用分组返回。

    Args:
        day_start: 起始日期（含）。
        day_end: 结束日期（不含）。
        ownerapp_id: 应用ID过滤。
        model_names: 模型名过滤。
        session: 异步数据库会话。

    Returns:
        按天+应用分组的用量总计列表，每条记录包含 month_start 字段（实际为 day）。
    """

    day_expr = func.date(ProxyNodeStatusLog.start_at).label("day")
    smts = (
        select(
            ProxyNodeStatusLog.ownerapp_id,
            day_expr,
            func.coalesce(func.count(ProxyNodeStatusLog.id), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.request_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.response_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.total_tokens), 0),
            func.coalesce(func.sum(ProxyNodeStatusLog.cached_tokens), 0),
        )
        .where(
            ProxyNodeStatusLog.end_at.is_not(None),
            ProxyNodeStatusLog.start_at >= day_start,
            ProxyNodeStatusLog.start_at < day_end,
            ProxyNodeStatusLog.ownerapp_id.is_not(None),
        )
        .group_by(
            ProxyNodeStatusLog.ownerapp_id,
            day_expr,
        )
        .order_by(day_expr.asc())
    )

    if ownerapp_id is not None:
        if ownerapp_id:
            smts = smts.where(ProxyNodeStatusLog.ownerapp_id == ownerapp_id)
        else:
            smts = smts.where(ProxyNodeStatusLog.ownerapp_id.is_(None))

    if model_names:
        smts = smts.where(ProxyNodeStatusLog.model_name.in_(model_names))

    rows = (await session.exec(smts)).all()
    return [
        MonthlyUsageTotalAggregate(
            ownerapp_id=str(row_ownerapp_id),
            month_start=row_day,
            call_count=int(row_call_count or 0),
            request_tokens=int(row_request_tokens or 0),
            response_tokens=int(row_response_tokens or 0),
            total_tokens=int(row_total_tokens or 0),
            cached_tokens=int(row_cached_tokens or 0),
        )
        for (
            row_ownerapp_id,
            row_day,
            row_call_count,
            row_request_tokens,
            row_response_tokens,
            row_total_tokens,
            row_cached_tokens,
        ) in rows
        if row_ownerapp_id
    ]


def _merge_model_aggregates_by_period(
    *aggregate_lists: list,
) -> list:
    """合并多个按周期+应用+模型分组的用量聚合列表，相同键的条目累加。

    支持带 day_start/week_start/month_start/year 字段的聚合对象。

    Args:
        *aggregate_lists: 多个聚合列表。

    Returns:
        合并后的用量聚合列表。
    """

    merged: dict[tuple, object] = {}
    for aggregate_list in aggregate_lists:
        for item in aggregate_list:
            # 确定周期键和值
            period_value = getattr(item, "day_start", None) or getattr(item, "week_start", None) or getattr(item, "month_start", None) or getattr(item, "year", None)
            key = (item.ownerapp_id, item.model_name, period_value)
            if key in merged:
                existing = merged[key]
                # 使用 dataclasses.replace 创建新对象，兼容 slots=True
                merged[key] = dc_replace(
                    existing,
                    call_count=existing.call_count + item.call_count,
                    request_tokens=existing.request_tokens + item.request_tokens,
                    response_tokens=existing.response_tokens + item.response_tokens,
                    total_tokens=existing.total_tokens + item.total_tokens,
                    cached_tokens=getattr(existing, 'cached_tokens', 0) + getattr(item, 'cached_tokens', 0),
                )
            else:
                merged[key] = item
    return list(merged.values())


def _merge_total_aggregates_by_period(
    *aggregate_lists: list,
) -> list:
    """合并多个按周期+应用分组的用量总计列表，相同键的条目累加。

    支持带 month_start/year 字段的聚合对象。

    Args:
        *aggregate_lists: 多个总计列表。

    Returns:
        合并后的用量总计列表。
    """

    merged: dict[tuple, object] = {}
    for aggregate_list in aggregate_lists:
        for item in aggregate_list:
            period_value = getattr(item, "month_start", None) or getattr(item, "year", None)
            key = (item.ownerapp_id, period_value)
            if key in merged:
                existing = merged[key]
                # 使用 dataclasses.replace 创建新对象，兼容 slots=True
                merged[key] = dc_replace(
                    existing,
                    call_count=existing.call_count + item.call_count,
                    request_tokens=existing.request_tokens + item.request_tokens,
                    response_tokens=existing.response_tokens + item.response_tokens,
                    total_tokens=existing.total_tokens + item.total_tokens,
                    cached_tokens=getattr(existing, 'cached_tokens', 0) + getattr(item, 'cached_tokens', 0),
                )
            else:
                merged[key] = item
    return list(merged.values())
