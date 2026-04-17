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

"""API 密钥配额管理路由。"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from openaiproxy.api.schemas import (
    ApiKeyQuotaCreate,
    ApiKeyQuotaResponse,
    ApiKeyQuotaUpdate,
    ApiKeyQuotaUsageResponse,
    PageResponse,
)
from openaiproxy.api.utils import AsyncDbSession, check_strict_api_key
from openaiproxy.services.database.models.apikey.crud import (
    count_apikey_quota_usages,
    count_apikey_quotas,
    select_apikey_by_id,
    select_apikey_quota_by_id,
    select_apikey_quota_by_unique,
    select_apikey_quota_usages,
    select_apikey_quotas,
)
from openaiproxy.services.database.models.apikey.model import ApiKeyQuota as ApiKeyQuotaModel
from openaiproxy.utils.timezone import current_time_in_timezone


router = APIRouter(tags=["API密钥配额管理"])


def _normalize_optional_str(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


async def _ensure_apikey_exists(
    api_key_id: UUID,
    *,
    session: AsyncDbSession,
) -> None:
    existed = await select_apikey_by_id(api_key_id, session=session)
    if existed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API 密钥不存在",
        )


@router.get(
    "/apikey-quotas",
    dependencies=[Depends(check_strict_api_key)],
    summary="分页获取API密钥配额",
)
async def list_apikey_quotas(
    api_key_id: Optional[UUID] = None,
    order_id: Optional[str] = None,
    expired: Optional[bool] = None,
    orderby: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[ApiKeyQuotaResponse]:
    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None
    normalized_order_id = _normalize_optional_str(order_id)

    quotas = await select_apikey_quotas(
        api_key_ids=[api_key_id] if api_key_id else None,
        order_id=normalized_order_id,
        expired=expired,
        orderby=orderby,
        offset=safe_offset,
        limit=safe_limit,
        session=session,
    )

    raw_total = await count_apikey_quotas(
        api_key_ids=[api_key_id] if api_key_id else None,
        order_id=normalized_order_id,
        expired=expired,
        session=session,
    )
    total = raw_total if isinstance(raw_total, int) else raw_total[0]

    payload = [
        ApiKeyQuotaResponse.model_validate(quota, from_attributes=True)
        for quota in quotas
    ]

    return PageResponse[ApiKeyQuotaResponse](
        offset=safe_offset,
        total=int(total),
        data=payload,
    )


@router.post(
    "/apikey-quotas",
    dependencies=[Depends(check_strict_api_key)],
    summary="创建API密钥配额",
)
async def create_apikey_quota(
    input: ApiKeyQuotaCreate,
    *,
    session: AsyncDbSession,
) -> ApiKeyQuotaResponse:
    await _ensure_apikey_exists(input.api_key_id, session=session)

    normalized_order_id = _normalize_optional_str(input.order_id)
    if normalized_order_id is not None:
        existed = await select_apikey_quota_by_unique(
            api_key_id=input.api_key_id,
            order_id=normalized_order_id,
            session=session,
        )
        if existed is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="API 密钥配额订单ID已存在",
            )

    current_time = current_time_in_timezone()
    quota_obj = ApiKeyQuotaModel(
        api_key_id=input.api_key_id,
        order_id=normalized_order_id,
        call_limit=input.call_limit,
        call_used=input.call_used,
        total_tokens_limit=input.total_tokens_limit,
        total_tokens_used=input.total_tokens_used,
        last_reset_at=input.last_reset_at,
        expired_at=input.expired_at,
        created_at=current_time,
        updated_at=current_time,
    )
    session.add(quota_obj)
    await session.commit()
    await session.refresh(quota_obj)

    return ApiKeyQuotaResponse.model_validate(quota_obj, from_attributes=True)


@router.get(
    "/apikey-quotas/usages",
    dependencies=[Depends(check_strict_api_key)],
    summary="查询API密钥配额使用记录",
)
async def list_apikey_quota_usage(
    quota_id: Optional[UUID] = None,
    api_key_id: Optional[UUID] = None,
    ownerapp_id: Optional[str] = None,
    request_action: Optional[str] = None,
    orderby: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[ApiKeyQuotaUsageResponse]:
    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None
    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    normalized_request_action = _normalize_optional_str(request_action)

    usages = await select_apikey_quota_usages(
        quota_ids=[quota_id] if quota_id else None,
        api_key_ids=[api_key_id] if api_key_id else None,
        ownerapp_id=normalized_ownerapp_id,
        request_action=normalized_request_action,
        orderby=orderby,
        offset=safe_offset,
        limit=safe_limit,
        session=session,
    )

    raw_total = await count_apikey_quota_usages(
        quota_ids=[quota_id] if quota_id else None,
        api_key_ids=[api_key_id] if api_key_id else None,
        ownerapp_id=normalized_ownerapp_id,
        request_action=normalized_request_action,
        session=session,
    )
    total = raw_total if isinstance(raw_total, int) else raw_total[0]

    payload = [
        ApiKeyQuotaUsageResponse.model_validate(item, from_attributes=True)
        for item in usages
    ]

    return PageResponse[ApiKeyQuotaUsageResponse](
        offset=safe_offset,
        total=int(total),
        data=payload,
    )


@router.get(
    "/apikey-quotas/{quota_id}",
    dependencies=[Depends(check_strict_api_key)],
    summary="获取API密钥配额详情",
)
async def get_apikey_quota(
    quota_id: UUID,
    *,
    session: AsyncDbSession,
) -> ApiKeyQuotaResponse:
    quota = await select_apikey_quota_by_id(quota_id, session=session)
    if quota is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API 密钥配额不存在",
        )
    return ApiKeyQuotaResponse.model_validate(quota, from_attributes=True)


@router.post(
    "/apikey-quotas/{quota_id}",
    dependencies=[Depends(check_strict_api_key)],
    summary="更新API密钥配额",
)
async def update_apikey_quota(
    quota_id: UUID,
    update: ApiKeyQuotaUpdate,
    *,
    session: AsyncDbSession,
) -> ApiKeyQuotaResponse:
    quota = await select_apikey_quota_by_id(quota_id, session=session)
    if quota is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API 密钥配额不存在",
        )

    update_payload = update.model_dump(exclude_unset=True)
    if not update_payload:
        return ApiKeyQuotaResponse.model_validate(quota, from_attributes=True)

    non_nullable_counters = {"call_used", "total_tokens_used"}
    for field in list(update_payload.keys()):
        if field in non_nullable_counters and update_payload[field] is None:
            update_payload.pop(field)

    target_api_key_id = quota.api_key_id
    if "api_key_id" in update_payload:
        if update_payload["api_key_id"] is not None:
            target_api_key_id = update_payload["api_key_id"]
            await _ensure_apikey_exists(target_api_key_id, session=session)
        else:
            update_payload.pop("api_key_id")

    if "order_id" in update_payload:
        normalized_order_id = _normalize_optional_str(update_payload["order_id"])
        update_payload["order_id"] = normalized_order_id
        if normalized_order_id is not None:
            existed = await select_apikey_quota_by_unique(
                api_key_id=target_api_key_id,
                order_id=normalized_order_id,
                session=session,
            )
            if existed is not None and existed.id != quota.id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="API 密钥配额订单ID已存在",
                )

    for field, value in update_payload.items():
        setattr(quota, field, value)

    quota.updated_at = current_time_in_timezone()
    session.add(quota)
    await session.commit()
    await session.refresh(quota)

    return ApiKeyQuotaResponse.model_validate(quota, from_attributes=True)


@router.delete(
    "/apikey-quotas/{quota_id}",
    dependencies=[Depends(check_strict_api_key)],
    summary="删除API密钥配额",
)
async def delete_apikey_quota(
    quota_id: UUID,
    *,
    session: AsyncDbSession,
):
    quota = await select_apikey_quota_by_id(quota_id, session=session)
    if quota is not None:
        now = current_time_in_timezone()
        quota.expired_at = quota.expired_at or now
        quota.updated_at = now
        session.add(quota)
        await session.commit()
    return {
        "code": 0,
        "message": "删除成功",
    }
