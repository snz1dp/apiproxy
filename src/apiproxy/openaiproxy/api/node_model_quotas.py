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


from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from openaiproxy.api.schemas import (
    NodeModelQuotaCreate,
    NodeModelQuotaResponse,
    NodeModelQuotaUpdate,
    NodeModelQuotaUsageResponse,
    PageResponse,
)
from openaiproxy.api.utils import AsyncDbSession, check_api_key
from openaiproxy.services.database.models.node.crud import (
    count_node_model_quota_usages,
    count_node_model_quotas,
    select_node_model_by_id,
    select_node_model_quota_by_id,
    select_node_model_quota_by_unique,
    select_node_model_quota_usages,
    select_node_model_quotas,
)
from openaiproxy.services.database.models.node.model import NodeModelQuota as NodeModelQuotaModel
from openaiproxy.utils.timezone import current_time_in_timezone


router = APIRouter(tags=["节点模型配额管理"])


def _normalize_optional_str(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


async def _ensure_node_model_exists(
    node_model_id: UUID,
    *,
    session: AsyncDbSession,
) -> None:
    existed = await select_node_model_by_id(node_model_id, session=session)
    if existed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点模型不存在",
        )


@router.get(
    "/quotas",
    dependencies=[Depends(check_api_key)],
    summary="分页获取节点模型配额",
)
async def list_node_model_quotas(
    node_id: Optional[UUID] = None,
    node_model_id: Optional[UUID] = None,
    order_id: Optional[str] = None,
    expired: Optional[bool] = None,
    orderby: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[NodeModelQuotaResponse]:
    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None
    normalized_order_id = _normalize_optional_str(order_id)

    quotas = await select_node_model_quotas(
        node_ids=[node_id] if node_id else None,
        node_model_ids=[node_model_id] if node_model_id else None,
        order_id=normalized_order_id,
        expired=expired,
        orderby=orderby,
        offset=safe_offset,
        limit=safe_limit,
        session=session,
    )

    raw_total = await count_node_model_quotas(
        node_ids=[node_id] if node_id else None,
        node_model_ids=[node_model_id] if node_model_id else None,
        order_id=normalized_order_id,
        expired=expired,
        session=session,
    )
    total = raw_total if isinstance(raw_total, int) else raw_total[0]

    payload = [
        NodeModelQuotaResponse.model_validate(quota, from_attributes=True)
        for quota in quotas
    ]

    return PageResponse[NodeModelQuotaResponse](
        offset=safe_offset,
        total=int(total),
        data=payload,
    )


@router.post(
    "/quotas",
    dependencies=[Depends(check_api_key)],
    summary="创建节点模型配额",
)
async def create_node_model_quota(
    input: NodeModelQuotaCreate,
    *,
    session: AsyncDbSession,
) -> NodeModelQuotaResponse:
    await _ensure_node_model_exists(input.node_model_id, session=session)

    normalized_order_id = _normalize_optional_str(input.order_id)
    if normalized_order_id is not None:
        existed = await select_node_model_quota_by_unique(
            node_model_id=input.node_model_id,
            order_id=normalized_order_id,
            session=session,
        )
        if existed is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="节点模型配额订单ID已存在",
            )

    current_time = current_time_in_timezone()
    quota_obj = NodeModelQuotaModel(
        node_model_id=input.node_model_id,
        order_id=normalized_order_id,
        call_limit=input.call_limit,
        call_used=input.call_used,
        prompt_tokens_limit=input.prompt_tokens_limit,
        prompt_tokens_used=input.prompt_tokens_used,
        completion_tokens_limit=input.completion_tokens_limit,
        completion_tokens_used=input.completion_tokens_used,
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

    return NodeModelQuotaResponse.model_validate(quota_obj, from_attributes=True)


@router.get(
    "/quotas/usages",
    dependencies=[Depends(check_api_key)],
    summary="查询节点模型配额使用记录",
)
async def list_node_model_quota_usage(
    quota_id: Optional[UUID] = None,
    node_id: Optional[UUID] = None,
    node_model_id: Optional[UUID] = None,
    ownerapp_id: Optional[str] = None,
    request_action: Optional[str] = None,
    orderby: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[NodeModelQuotaUsageResponse]:
    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None
    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    normalized_request_action = _normalize_optional_str(request_action)

    usages = await select_node_model_quota_usages(
        quota_ids=[quota_id] if quota_id else None,
        node_ids=[node_id] if node_id else None,
        node_model_ids=[node_model_id] if node_model_id else None,
        ownerapp_id=normalized_ownerapp_id,
        request_action=normalized_request_action,
        orderby=orderby,
        offset=safe_offset,
        limit=safe_limit,
        session=session,
    )

    raw_total = await count_node_model_quota_usages(
        quota_ids=[quota_id] if quota_id else None,
        node_ids=[node_id] if node_id else None,
        node_model_ids=[node_model_id] if node_model_id else None,
        ownerapp_id=normalized_ownerapp_id,
        request_action=normalized_request_action,
        session=session,
    )
    total = raw_total if isinstance(raw_total, int) else raw_total[0]

    payload = [
        NodeModelQuotaUsageResponse.model_validate(item, from_attributes=True)
        for item in usages
    ]

    return PageResponse[NodeModelQuotaUsageResponse](
        offset=safe_offset,
        total=int(total),
        data=payload,
    )


@router.get(
    "/quotas/{quota_id}",
    dependencies=[Depends(check_api_key)],
    summary="获取节点模型配额详情",
)
async def get_node_model_quota(
    quota_id: UUID,
    *,
    session: AsyncDbSession,
) -> NodeModelQuotaResponse:
    quota = await select_node_model_quota_by_id(quota_id, session=session)
    if quota is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点模型配额不存在",
        )
    return NodeModelQuotaResponse.model_validate(quota, from_attributes=True)


@router.post(
    "/quotas/{quota_id}",
    dependencies=[Depends(check_api_key)],
    summary="更新节点模型配额",
)
async def update_node_model_quota(
    quota_id: UUID,
    update: NodeModelQuotaUpdate,
    *,
    session: AsyncDbSession,
) -> NodeModelQuotaResponse:
    quota = await select_node_model_quota_by_id(quota_id, session=session)
    if quota is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点模型配额不存在",
        )

    update_payload = update.model_dump(exclude_unset=True)
    if not update_payload:
        return NodeModelQuotaResponse.model_validate(quota, from_attributes=True)

    non_nullable_counters = {
        "call_used",
        "prompt_tokens_used",
        "completion_tokens_used",
        "total_tokens_used",
    }
    for field in list(update_payload.keys()):
        if field in non_nullable_counters and update_payload[field] is None:
            update_payload.pop(field)

    target_node_model_id = quota.node_model_id
    if "node_model_id" in update_payload:
        if update_payload["node_model_id"] is not None:
            target_node_model_id = update_payload["node_model_id"]
            await _ensure_node_model_exists(target_node_model_id, session=session)
            update_payload["node_model_id"] = target_node_model_id
        else:
            update_payload.pop("node_model_id")

    if "order_id" in update_payload:
        normalized_order_id = _normalize_optional_str(update_payload["order_id"])
        update_payload["order_id"] = normalized_order_id
        if normalized_order_id is not None:
            existed = await select_node_model_quota_by_unique(
                node_model_id=target_node_model_id,
                order_id=normalized_order_id,
                session=session,
            )
            if existed is not None and existed.id != quota.id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="节点模型配额订单ID已存在",
                )

    for field, value in update_payload.items():
        setattr(quota, field, value)

    quota.updated_at = current_time_in_timezone()
    session.add(quota)
    await session.commit()
    await session.refresh(quota)

    return NodeModelQuotaResponse.model_validate(quota, from_attributes=True)


@router.delete(
    "/quotas/{quota_id}",
    dependencies=[Depends(check_api_key)],
    summary="删除节点模型配额",
)
async def delete_node_model_quota(
    quota_id: UUID,
    *,
    session: AsyncDbSession,
):
    quota = await select_node_model_quota_by_id(quota_id, session=session)
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
