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
from typing import Optional, Tuple
from uuid import UUID

from openaiproxy.services.database.models.node.model import NodeModelQuota, NodeModelQuotaUsage
from openaiproxy.services.nodeproxy.exceptions import NodeModelQuotaExceeded
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.utils.timezone import current_timezone
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

async def get_db_process_id(*, session: AsyncSession):
    """获取数据库进程 ID"""
    smts = select(func.pg_backend_pid())
    result = await session.exec(smts)
    return result.first()


def _normalize_request_action(action: RequestAction | str | None) -> Optional[str]:
    if action is None:
        return None
    if isinstance(action, RequestAction):
        return action.value
    return str(action)


def _format_quota_detail(
    *,
    model_name: Optional[str],
    node_model_id: UUID,
    order_id: Optional[str],
) -> str:
    base = model_name or str(node_model_id)
    if order_id:
        return f"{base} (order {order_id})"
    return base


def _quota_is_exhausted(quota: NodeModelQuota) -> bool:
    """判断配额是否已经耗尽."""

    if quota.call_limit is not None and quota.call_used >= quota.call_limit:
        return True
    if quota.prompt_tokens_limit is not None and quota.prompt_tokens_used >= quota.prompt_tokens_limit:
        return True
    if quota.completion_tokens_limit is not None and quota.completion_tokens_used >= quota.completion_tokens_limit:
        return True
    if quota.total_tokens_limit is not None and quota.total_tokens_used >= quota.total_tokens_limit:
        return True
    return False


async def reserve_node_model_quota(
    *,
    session: AsyncSession,
    node_id: UUID,
    node_model_id: UUID,
    proxy_id: Optional[UUID],
    model_name: Optional[str],
    model_type: Optional[str],
    ownerapp_id: Optional[str],
    request_action: RequestAction | str | None,
    estimated_request_tokens: Optional[int],
) -> Optional[Tuple[UUID, UUID]]:
    """预占节点模型配额并创建使用记录."""

    current_time = datetime.now(tz=current_timezone())
    quota_stmt = (
        select(NodeModelQuota)
        .where(NodeModelQuota.node_model_id == node_model_id)
        .where(NodeModelQuota.expired_at.is_(None) | (NodeModelQuota.expired_at > current_time))
        .where(NodeModelQuota.call_limit.is_(None) | (NodeModelQuota.call_used < NodeModelQuota.call_limit))
        .where(NodeModelQuota.prompt_tokens_limit.is_(None) | (NodeModelQuota.prompt_tokens_used < NodeModelQuota.prompt_tokens_limit))
        .where(NodeModelQuota.completion_tokens_limit.is_(None) | (NodeModelQuota.completion_tokens_used < NodeModelQuota.completion_tokens_limit))
        .order_by(NodeModelQuota.created_at.asc(), NodeModelQuota.id.asc())
        .with_for_update()
    )
    quota_result = await session.exec(quota_stmt)
    quotas = quota_result.all()
    if not quotas:
        return None

    picked_quota: Optional[NodeModelQuota] = None

    for quota in quotas:
        if not _quota_is_exhausted(quota):
            picked_quota = quota
            break

    if picked_quota is None:
        detail = _format_quota_detail(
            model_name=model_name,
            node_model_id=node_model_id,
            order_id=quotas[-1].order_id if quotas else None,
        )
        raise NodeModelQuotaExceeded(
            f"节点模型 {detail} 配额已全部耗尽",
            detail=detail,
        )

    now = datetime.now(tz=current_timezone())
    picked_quota.updated_at = now
    picked_quota.call_used += 1
    session.add(picked_quota)

    usage_entry = NodeModelQuotaUsage(
        quota_id=picked_quota.id,
        node_id=node_id,
        node_model_id=node_model_id,
        proxy_id=proxy_id,
        nodelog_id=None,
        ownerapp_id=ownerapp_id,
        request_action=_normalize_request_action(request_action),
        call_count=1,
        request_tokens=0,
        response_tokens=0,
        total_tokens=0,
        created_at=now,
        updated_at=now,
    )
    session.add(usage_entry)
    await session.flush()

    return picked_quota.id, usage_entry.id


async def finalize_node_model_quota_usage(
    *,
    session: AsyncSession,
    node_id: UUID,
    node_model_id: UUID,
    proxy_id: Optional[UUID],
    primary_quota_id: UUID,
    primary_quota_usage_id: UUID,
    model_name: Optional[str],
    request_tokens: int,
    response_tokens: int,
    total_tokens: int,
    ownerapp_id: Optional[str],
    request_action: RequestAction | str | None,
    log_id: Optional[UUID],
) -> None:
    """更新节点模型配额使用数据."""

    quota_stmt = (
        select(NodeModelQuota)
        .where(NodeModelQuota.node_model_id == node_model_id)
        .order_by(NodeModelQuota.created_at.asc(), NodeModelQuota.id.asc())
        .with_for_update()
    )
    quotas_result = await session.exec(quota_stmt)
    quotas = quotas_result.all()
    if not quotas:
        return

    now = datetime.now(tz=current_timezone())
    normalized_action = _normalize_request_action(request_action)

    usage_stmt = (
        select(NodeModelQuotaUsage)
        .where(NodeModelQuotaUsage.id == primary_quota_usage_id)
        .with_for_update()
    )
    usage_result = await session.exec(usage_stmt)
    primary_usage = usage_result.first()
    if primary_usage is None:
        primary_usage = NodeModelQuotaUsage(
            quota_id=primary_quota_id,
            node_id=node_id,
            node_model_id=node_model_id,
            proxy_id=proxy_id,
            nodelog_id=log_id,
            ownerapp_id=ownerapp_id,
            request_action=normalized_action,
            call_count=1,
            request_tokens=0,
            response_tokens=0,
            total_tokens=0,
            created_at=now,
            updated_at=now,
        )
        session.add(primary_usage)
        await session.flush()

    usage_lookup: dict[UUID, NodeModelQuotaUsage] = {primary_quota_id: primary_usage}

    remaining_prompt = int(max(request_tokens, 0))
    remaining_completion = int(max(response_tokens, 0))
    remaining_total = int(max(total_tokens, 0))

    def _capacity(limit: Optional[int], used: int) -> float:
        if limit is None:
            return float('inf')
        return max(limit - used, 0)

    for quota in quotas:
        is_primary = quota.id == primary_quota_id
        total_capacity = _capacity(quota.total_tokens_limit, quota.total_tokens_used)
        prompt_capacity = _capacity(quota.prompt_tokens_limit, quota.prompt_tokens_used)
        completion_capacity = _capacity(quota.completion_tokens_limit, quota.completion_tokens_used)

        prompt_consumed = int(min(remaining_prompt, prompt_capacity, total_capacity))
        remaining_prompt -= prompt_consumed
        total_capacity = max(total_capacity - prompt_consumed, 0)

        completion_consumed = int(min(remaining_completion, completion_capacity, total_capacity))
        remaining_completion -= completion_consumed
        total_capacity = max(total_capacity - completion_consumed, 0)

        total_consumed = prompt_consumed + completion_consumed
        if total_consumed > 0:
            quota.total_tokens_used += total_consumed
            remaining_total = max(remaining_total - total_consumed, 0)

        if prompt_consumed > 0:
            quota.prompt_tokens_used += prompt_consumed
        if completion_consumed > 0:
            quota.completion_tokens_used += completion_consumed

        if total_consumed > 0 or is_primary:
            quota.updated_at = now
            session.add(quota)

            usage = usage_lookup.get(quota.id)
            if usage is None:
                usage = NodeModelQuotaUsage(
                    quota_id=quota.id,
                    node_id=node_id,
                    node_model_id=node_model_id,
                    proxy_id=proxy_id,
                    nodelog_id=log_id,
                    ownerapp_id=ownerapp_id,
                    request_action=normalized_action,
                    call_count=0,
                    request_tokens=0,
                    response_tokens=0,
                    total_tokens=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(usage)
                await session.flush()
                usage_lookup[quota.id] = usage

            usage.request_tokens += prompt_consumed
            usage.response_tokens += completion_consumed
            usage.total_tokens += total_consumed
            if is_primary:
                usage.call_count = max(int(usage.call_count or 0), 1)
            usage.updated_at = now
            if ownerapp_id and not usage.ownerapp_id:
                usage.ownerapp_id = ownerapp_id
            if normalized_action:
                usage.request_action = normalized_action
            if log_id and usage.nodelog_id is None:
                usage.nodelog_id = log_id
            session.add(usage)
        else:
            session.add(quota)

    if remaining_prompt > 0 or remaining_completion > 0 or remaining_total > 0:
        detail = _format_quota_detail(
            model_name=model_name,
            node_model_id=node_model_id,
            order_id=quotas[-1].order_id if quotas else None,
        )
        raise NodeModelQuotaExceeded(
            f"节点模型 {detail} 配额不足，剩余请求无法分配",
            detail=detail,
        )

