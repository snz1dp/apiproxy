"""应用模型访问策略管理路由。"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError

from openaiproxy.api.schemas import (
    AppModelAccessPolicyCreate,
    AppModelAccessPolicyRead,
    AppModelAccessPolicyUpdate,
    PageResponse,
)
from openaiproxy.api.utils import AsyncDbSession, check_strict_api_key
from openaiproxy.services.database.models.app.crud import (
    count_app_model_access_policies,
    create_app_model_access_policy_record,
    select_app_model_access_policies,
    select_app_model_access_policy_by_id,
    select_app_model_access_policy_by_ownerapp_id,
    update_app_model_access_policy_record,
)
from openaiproxy.services.database.models.app.model import AppModelAccessPolicy
from openaiproxy.utils.timezone import current_time_in_timezone


router = APIRouter(tags=["应用模型访问策略管理"])


def _normalize_optional_str(value: Optional[str]) -> Optional[str]:
    """标准化可选字符串查询参数。"""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _to_policy_read(policy: AppModelAccessPolicy) -> AppModelAccessPolicyRead:
    """将 ORM 对象转换为接口响应。"""
    return AppModelAccessPolicyRead.model_validate(policy, from_attributes=True)


@router.get(
    "/app-model-access-policies",
    dependencies=[Depends(check_strict_api_key)],
    summary="分页获取应用模型访问策略",
)
async def list_app_model_access_policies(
    ownerapp_id: Optional[str] = None,
    orderby: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[AppModelAccessPolicyRead]:
    """分页获取应用模型访问策略。"""
    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None
    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)

    policies = await select_app_model_access_policies(
        ownerapp_id=normalized_ownerapp_id,
        orderby=orderby,
        offset=safe_offset,
        limit=safe_limit,
        session=session,
    )
    raw_total = await count_app_model_access_policies(
        ownerapp_id=normalized_ownerapp_id,
        session=session,
    )
    total = raw_total if isinstance(raw_total, int) else raw_total[0]

    return PageResponse[AppModelAccessPolicyRead](
        offset=safe_offset,
        total=int(total),
        data=[_to_policy_read(item) for item in policies],
    )


@router.post(
    "/app-model-access-policies",
    dependencies=[Depends(check_strict_api_key)],
    summary="创建应用模型访问策略",
)
async def create_app_model_access_policy(
    input: AppModelAccessPolicyCreate,
    *,
    session: AsyncDbSession,
) -> AppModelAccessPolicyRead:
    """创建应用模型访问策略。"""
    existed = await select_app_model_access_policy_by_ownerapp_id(input.ownerapp_id, session=session)
    if existed is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="应用模型访问策略已存在",
        )

    current_time = current_time_in_timezone()
    try:
        policy = await create_app_model_access_policy_record(
            session=session,
            policy_payload={
                "ownerapp_id": input.ownerapp_id,
                "allowed_models": input.allowed_models,
                "created_at": current_time,
                "updated_at": current_time,
            },
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="应用模型访问策略已存在",
        ) from exc

    return _to_policy_read(policy)


@router.get(
    "/app-model-access-policies/{policy_id}",
    dependencies=[Depends(check_strict_api_key)],
    summary="获取应用模型访问策略详情",
)
async def get_app_model_access_policy(
    policy_id: UUID,
    *,
    session: AsyncDbSession,
) -> AppModelAccessPolicyRead:
    """获取应用模型访问策略详情。"""
    policy = await select_app_model_access_policy_by_id(policy_id, session=session)
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="应用模型访问策略不存在",
        )
    return _to_policy_read(policy)


@router.post(
    "/app-model-access-policies/{policy_id}",
    dependencies=[Depends(check_strict_api_key)],
    summary="更新应用模型访问策略",
)
async def update_app_model_access_policy(
    policy_id: UUID,
    update: AppModelAccessPolicyUpdate,
    *,
    session: AsyncDbSession,
) -> AppModelAccessPolicyRead:
    """更新应用模型访问策略。"""
    policy = await select_app_model_access_policy_by_id(policy_id, session=session)
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="应用模型访问策略不存在",
        )

    update_payload = update.model_dump(exclude_unset=True)
    if not update_payload:
        return _to_policy_read(policy)

    policy = await update_app_model_access_policy_record(
        session=session,
        policy=policy,
        update_payload=update_payload,
        updated_at=current_time_in_timezone(),
    )
    return _to_policy_read(policy)