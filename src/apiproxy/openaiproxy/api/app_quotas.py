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

"""应用配额管理路由。"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from openaiproxy.api.schemas import (
    AppQuotaCreate,
    AppQuotaResponse,
    AppQuotaUpdate,
    AppQuotaUsageResponse,
    PageResponse,
)
from openaiproxy.api.utils import AsyncDbSession, check_strict_api_key
from openaiproxy.services.database.models.app.crud import (
    count_app_quota_usages,
    count_app_quotas,
    create_app_quota_record,
    expire_app_quota_record,
    select_app_quota_by_id,
    select_app_quota_by_unique,
    select_app_quota_usages,
    select_app_quotas,
    update_app_quota_record,
)
from openaiproxy.utils.timezone import current_time_in_timezone


router = APIRouter(tags=["应用配额管理"])


def _normalize_optional_str(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


@router.get(
    "/app-quotas",
    dependencies=[Depends(check_strict_api_key)],
    summary="分页获取应用配额",
)
async def list_app_quotas(
    ownerapp_id: Optional[str] = None,
    order_id: Optional[str] = None,
    expired: Optional[bool] = None,
    orderby: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[AppQuotaResponse]:
    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None
    normalized_order_id = _normalize_optional_str(order_id)
    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)

    quotas = await select_app_quotas(
        ownerapp_ids=[normalized_ownerapp_id] if normalized_ownerapp_id else None,
        order_id=normalized_order_id,
        expired=expired,
        orderby=orderby,
        offset=safe_offset,
        limit=safe_limit,
        session=session,
    )

    raw_total = await count_app_quotas(
        ownerapp_ids=[normalized_ownerapp_id] if normalized_ownerapp_id else None,
        order_id=normalized_order_id,
        expired=expired,
        session=session,
    )
    total = raw_total if isinstance(raw_total, int) else raw_total[0]

    payload = [
        AppQuotaResponse.model_validate(quota, from_attributes=True)
        for quota in quotas
    ]

    return PageResponse[AppQuotaResponse](
        offset=safe_offset,
        total=int(total),
        data=payload,
    )


@router.post(
    "/app-quotas",
    dependencies=[Depends(check_strict_api_key)],
    summary="创建应用配额",
)
async def create_app_quota(
    input: AppQuotaCreate,
    *,
    session: AsyncDbSession,
) -> AppQuotaResponse:
    normalized_order_id = _normalize_optional_str(input.order_id)
    if normalized_order_id is not None:
        existed = await select_app_quota_by_unique(
            ownerapp_id=input.ownerapp_id,
            order_id=normalized_order_id,
            session=session,
        )
        if existed is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="应用配额订单ID已存在",
            )

    current_time = current_time_in_timezone()
    quota_obj = await create_app_quota_record(
        session=session,
        quota_payload={
            "ownerapp_id": input.ownerapp_id,
            "order_id": normalized_order_id,
            "call_limit": input.call_limit,
            "call_used": input.call_used,
            "total_tokens_limit": input.total_tokens_limit,
            "total_tokens_used": input.total_tokens_used,
            "last_reset_at": input.last_reset_at,
            "expired_at": input.expired_at,
            "created_at": current_time,
            "updated_at": current_time,
        },
    )

    return AppQuotaResponse.model_validate(quota_obj, from_attributes=True)


@router.get(
    "/app-quotas/usages",
    dependencies=[Depends(check_strict_api_key)],
    summary="查询应用配额使用记录",
)
async def list_app_quota_usage(
    quota_id: Optional[UUID] = None,
    ownerapp_id: Optional[str] = None,
    api_key_id: Optional[UUID] = None,
    request_action: Optional[str] = None,
    orderby: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[AppQuotaUsageResponse]:
    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None
    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    normalized_request_action = _normalize_optional_str(request_action)

    usages = await select_app_quota_usages(
        quota_ids=[quota_id] if quota_id else None,
        ownerapp_ids=[normalized_ownerapp_id] if normalized_ownerapp_id else None,
        api_key_id=api_key_id,
        request_action=normalized_request_action,
        orderby=orderby,
        offset=safe_offset,
        limit=safe_limit,
        session=session,
    )

    raw_total = await count_app_quota_usages(
        quota_ids=[quota_id] if quota_id else None,
        ownerapp_ids=[normalized_ownerapp_id] if normalized_ownerapp_id else None,
        api_key_id=api_key_id,
        request_action=normalized_request_action,
        session=session,
    )
    total = raw_total if isinstance(raw_total, int) else raw_total[0]

    payload = [
        AppQuotaUsageResponse.model_validate(item, from_attributes=True)
        for item in usages
    ]

    return PageResponse[AppQuotaUsageResponse](
        offset=safe_offset,
        total=int(total),
        data=payload,
    )


@router.get(
    "/app-quotas/{quota_id}",
    dependencies=[Depends(check_strict_api_key)],
    summary="获取应用配额详情",
)
async def get_app_quota(
    quota_id: UUID,
    *,
    session: AsyncDbSession,
) -> AppQuotaResponse:
    quota = await select_app_quota_by_id(quota_id, session=session)
    if quota is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="应用配额不存在",
        )
    return AppQuotaResponse.model_validate(quota, from_attributes=True)


@router.post(
    "/app-quotas/{quota_id}",
    dependencies=[Depends(check_strict_api_key)],
    summary="更新应用配额",
)
async def update_app_quota(
    quota_id: UUID,
    update: AppQuotaUpdate,
    *,
    session: AsyncDbSession,
) -> AppQuotaResponse:
    quota = await select_app_quota_by_id(quota_id, session=session)
    if quota is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="应用配额不存在",
        )

    update_payload = update.model_dump(exclude_unset=True)
    if not update_payload:
        return AppQuotaResponse.model_validate(quota, from_attributes=True)

    non_nullable_counters = {"call_used", "total_tokens_used"}
    for field in list(update_payload.keys()):
        if field in non_nullable_counters and update_payload[field] is None:
            update_payload.pop(field)

    if "ownerapp_id" in update_payload:
        if update_payload["ownerapp_id"] is None:
            update_payload.pop("ownerapp_id")

    target_ownerapp_id = update_payload.get("ownerapp_id", quota.ownerapp_id)

    if "order_id" in update_payload:
        normalized_order_id = _normalize_optional_str(update_payload["order_id"])
        update_payload["order_id"] = normalized_order_id
        if normalized_order_id is not None:
            existed = await select_app_quota_by_unique(
                ownerapp_id=target_ownerapp_id,
                order_id=normalized_order_id,
                session=session,
            )
            if existed is not None and existed.id != quota.id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="应用配额订单ID已存在",
                )

    quota = await update_app_quota_record(
        session=session,
        quota=quota,
        update_payload=update_payload,
        updated_at=current_time_in_timezone(),
    )

    return AppQuotaResponse.model_validate(quota, from_attributes=True)


@router.delete(
    "/app-quotas/{quota_id}",
    dependencies=[Depends(check_strict_api_key)],
    summary="删除应用配额",
)
async def delete_app_quota(
    quota_id: UUID,
    *,
    session: AsyncDbSession,
):
    quota = await select_app_quota_by_id(quota_id, session=session)
    if quota is not None:
        await expire_app_quota_record(
            session=session,
            quota=quota,
            expired_at=current_time_in_timezone(),
        )
    return {
        "code": 0,
        "message": "删除成功",
    }
