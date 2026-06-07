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

"""API 密钥配额 reserve / finalize 核心操作。"""

from datetime import datetime
from typing import Optional, Tuple
from uuid import UUID

from openaiproxy.services.database.models.apikey.model import ApiKeyQuota, ApiKeyQuotaUsage
from openaiproxy.services.nodeproxy.exceptions import ApiKeyQuotaExceeded
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.utils.timezone import current_timezone
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession


def _normalize_request_action(action: RequestAction | str | None) -> Optional[str]:
    """将 RequestAction 枚举或字符串统一为可选字符串。"""
    if action is None:
        return None
    if isinstance(action, RequestAction):
        return action.value
    return str(action)


def _apikey_quota_is_exhausted(quota: ApiKeyQuota) -> bool:
    """判断 API 密钥配额是否已经耗尽。"""
    if quota.call_limit is not None and quota.call_used >= quota.call_limit:
        return True
    if quota.total_tokens_limit is not None and quota.total_tokens_used >= quota.total_tokens_limit:
        return True
    return False


def _remaining_capacity(limit: Optional[int], used: int) -> float:
    """返回配额剩余容量；无限配额返回正无穷。"""
    if limit is None:
        return float('inf')
    return max(limit - used, 0)


async def _select_apikey_quota_for_update(
    *,
    session: AsyncSession,
    quota_id: UUID,
) -> Optional[ApiKeyQuota]:
    """按主键锁定一条 API 密钥配额记录。"""
    quota_stmt = select(ApiKeyQuota).where(ApiKeyQuota.id == quota_id).with_for_update()
    quota_result = await session.exec(quota_stmt)
    return quota_result.first()


async def reserve_apikey_quota(
    *,
    session: AsyncSession,
    api_key_id: UUID,
    proxy_id: Optional[UUID],
    ownerapp_id: Optional[str],
    model_name: Optional[str],
    request_action: RequestAction | str | None,
    estimated_total_tokens: Optional[int] = None,
) -> Optional[Tuple[UUID, UUID]]:
    """预占 API 密钥配额并创建使用记录。

    返回 (quota_id, usage_id) 元组；无配额单时返回 None（不限制）。
    所有配额单均耗尽时抛出 ApiKeyQuotaExceeded。
    """
    current_time = datetime.now(tz=current_timezone())

    quota_stmt = (
        select(
            ApiKeyQuota.id,
            ApiKeyQuota.call_limit,
            ApiKeyQuota.call_used,
            ApiKeyQuota.total_tokens_limit,
            ApiKeyQuota.total_tokens_used,
        )
        .where(ApiKeyQuota.api_key_id == api_key_id)
        .where(ApiKeyQuota.expired_at.is_(None) | (ApiKeyQuota.expired_at > current_time))
        .where(ApiKeyQuota.call_limit.is_(None) | (ApiKeyQuota.call_used < ApiKeyQuota.call_limit))
        .order_by(ApiKeyQuota.created_at.asc(), ApiKeyQuota.id.asc())
    )
    quota_result = await session.exec(quota_stmt)
    quotas = quota_result.all()

    if not quotas:
        # 检查是否有任何配额单（包括已耗尽的）
        any_stmt = (
            select(ApiKeyQuota.id)
            .where(ApiKeyQuota.api_key_id == api_key_id)
            .where(ApiKeyQuota.expired_at.is_(None) | (ApiKeyQuota.expired_at > current_time))
            .limit(1)
        )
        any_result = await session.exec(any_stmt)
        any_quota_id = any_result.first()
        if any_quota_id is None:
            # 完全没有配额单 → 不限制
            return None
        # 有配额单但全部耗尽
        raise ApiKeyQuotaExceeded(
            f"API 密钥 {api_key_id} 配额已全部耗尽",
            detail=str(api_key_id),
        )

    candidate_quota_ids: list[UUID] = []
    remaining_capacity = 0
    unlimited_capacity = False
    for quota_id, call_limit, call_used, total_tokens_limit, total_tokens_used in quotas:
        if total_tokens_limit is None:
            unlimited_capacity = True
        else:
            remaining_capacity += max(total_tokens_limit - total_tokens_used, 0)

        call_exhausted = call_limit is not None and call_used >= call_limit
        total_exhausted = total_tokens_limit is not None and total_tokens_used >= total_tokens_limit
        if not call_exhausted and not total_exhausted:
            candidate_quota_ids.append(quota_id)

    if not candidate_quota_ids:
        raise ApiKeyQuotaExceeded(
            f"API 密钥 {api_key_id} 配额已全部耗尽",
            detail=str(api_key_id),
        )

    if estimated_total_tokens is not None and estimated_total_tokens > 0:
        if not unlimited_capacity and remaining_capacity < estimated_total_tokens:
            raise ApiKeyQuotaExceeded(
                f"API 密钥 {api_key_id} 剩余 token 配额不足",
                detail=str(api_key_id),
            )

    picked_quota: Optional[ApiKeyQuota] = None
    for quota_id in candidate_quota_ids:
        locked_quota = await _select_apikey_quota_for_update(session=session, quota_id=quota_id)
        if locked_quota is None or _apikey_quota_is_exhausted(locked_quota):
            continue
        picked_quota = locked_quota
        break

    if picked_quota is None:
        raise ApiKeyQuotaExceeded(
            f"API 密钥 {api_key_id} 配额已全部耗尽",
            detail=str(api_key_id),
        )

    now = datetime.now(tz=current_timezone())
    picked_quota.updated_at = now
    picked_quota.call_used += 1
    session.add(picked_quota)

    usage_entry = ApiKeyQuotaUsage(
        quota_id=picked_quota.id,
        api_key_id=api_key_id,
        proxy_id=proxy_id,
        nodelog_id=None,
        ownerapp_id=ownerapp_id,
        model_name=model_name,
        request_action=_normalize_request_action(request_action),
        call_count=1,
        total_tokens=0,
        created_at=now,
        updated_at=now,
    )
    session.add(usage_entry)
    await session.flush()

    return picked_quota.id, usage_entry.id


async def finalize_apikey_quota_usage(
    *,
    session: AsyncSession,
    api_key_id: UUID,
    primary_quota_id: UUID,
    primary_quota_usage_id: UUID,
    total_tokens: int,
    ownerapp_id: Optional[str],
    model_name: Optional[str],
    request_action: RequestAction | str | None,
    log_id: Optional[UUID],
) -> None:
    """更新 API 密钥配额的 token 使用数据。

    按 FIFO 顺序将 total_tokens 分配到各配额单的 total_tokens_used。
    """
    quota_stmt = (
        select(
            ApiKeyQuota.id,
            ApiKeyQuota.total_tokens_limit,
            ApiKeyQuota.total_tokens_used,
        )
        .where(ApiKeyQuota.api_key_id == api_key_id)
        .order_by(ApiKeyQuota.created_at.asc(), ApiKeyQuota.id.asc())
    )
    quotas_result = await session.exec(quota_stmt)
    quotas = quotas_result.all()
    if not quotas:
        return

    now = datetime.now(tz=current_timezone())
    normalized_action = _normalize_request_action(request_action)

    # 查找主 usage 记录
    usage_stmt = (
        select(ApiKeyQuotaUsage)
        .where(ApiKeyQuotaUsage.id == primary_quota_usage_id)
        .with_for_update()
    )
    usage_result = await session.exec(usage_stmt)
    primary_usage = usage_result.first()
    if primary_usage is None:
        primary_usage = ApiKeyQuotaUsage(
            quota_id=primary_quota_id,
            api_key_id=api_key_id,
            proxy_id=None,
            nodelog_id=log_id,
            ownerapp_id=ownerapp_id,
            model_name=model_name,
            request_action=normalized_action,
            call_count=1,
            total_tokens=0,
            created_at=now,
            updated_at=now,
        )
        session.add(primary_usage)
        await session.flush()

    usage_lookup: dict[UUID, ApiKeyQuotaUsage] = {primary_quota_id: primary_usage}

    remaining_total = int(max(total_tokens, 0))

    primary_handled = False
    for quota_id, total_tokens_limit, total_tokens_used in quotas:
        is_primary = quota_id == primary_quota_id
        appears_available = _remaining_capacity(total_tokens_limit, total_tokens_used) > 0

        if remaining_total <= 0 and primary_handled:
            break
        if not is_primary and (remaining_total <= 0 or not appears_available):
            continue

        quota = await _select_apikey_quota_for_update(session=session, quota_id=quota_id)
        if quota is None:
            continue

        is_primary = quota.id == primary_quota_id
        total_capacity = _remaining_capacity(quota.total_tokens_limit, quota.total_tokens_used)

        consumed = int(min(remaining_total, total_capacity))
        remaining_total -= consumed

        if consumed > 0:
            quota.total_tokens_used += consumed

        if consumed > 0 or is_primary:
            quota.updated_at = now
            session.add(quota)

            usage = usage_lookup.get(quota.id)
            if usage is None:
                usage = ApiKeyQuotaUsage(
                    quota_id=quota.id,
                    api_key_id=api_key_id,
                    proxy_id=None,
                    nodelog_id=log_id,
                    ownerapp_id=ownerapp_id,
                    model_name=model_name,
                    request_action=normalized_action,
                    call_count=0,
                    total_tokens=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(usage)
                await session.flush()
                usage_lookup[quota.id] = usage

            usage.total_tokens += consumed
            if is_primary:
                usage.call_count = max(int(usage.call_count or 0), 1)
            usage.updated_at = now
            if ownerapp_id and not usage.ownerapp_id:
                usage.ownerapp_id = ownerapp_id
            if model_name:
                usage.model_name = model_name
            if normalized_action:
                usage.request_action = normalized_action
            if log_id and usage.nodelog_id is None:
                usage.nodelog_id = log_id
            session.add(usage)

        if is_primary:
            primary_handled = True

    await session.flush()

    if remaining_total > 0:
        raise ApiKeyQuotaExceeded(
            f"API 密钥 {api_key_id} token 配额不足，剩余请求无法分配",
            detail=str(api_key_id),
        )


async def rollback_apikey_quota_usage(
    *,
    session: AsyncSession,
    quota_id: UUID,
    usage_id: UUID | None,
) -> None:
    """回滚 API 密钥配额的预占调用次数并删除使用记录。"""
    quota_stmt = select(ApiKeyQuota).where(ApiKeyQuota.id == quota_id).with_for_update()
    quota_result = await session.exec(quota_stmt)
    quota = quota_result.first()
    if quota is not None and quota.call_used > 0:
        quota.call_used -= 1
        quota.updated_at = datetime.now(tz=current_timezone())
        session.add(quota)

    if usage_id is None:
        await session.flush()
        return

    usage_stmt = select(ApiKeyQuotaUsage).where(ApiKeyQuotaUsage.id == usage_id)
    usage_result = await session.exec(usage_stmt)
    usage = usage_result.first()
    if usage is not None:
        await session.delete(usage)
    await session.flush()
