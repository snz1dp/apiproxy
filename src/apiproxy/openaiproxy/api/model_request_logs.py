from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends

from openaiproxy.api.schemas import (
    ModelServiceRequestLogResponse,
    PageResponse,
)
from openaiproxy.api.utils import AsyncDbSession, check_api_key
from openaiproxy.services.database.models.proxy.crud import (
    count_proxy_node_status_logs,
    select_proxy_node_status_logs,
)


router = APIRouter(tags=["模型服务请求记录管理"])


def _normalize_optional_str(value: Optional[str]) -> Optional[str]:
    """清理可选字符串参数，空白字符串视为 None。"""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


@router.get(
    "/request-logs",
    dependencies=[Depends(check_api_key)],
    summary="查询模型服务接口请求记录",
)
async def list_model_service_request_logs(
    log_id: Optional[UUID] = None,
    node_id: Optional[UUID] = None,
    proxy_id: Optional[UUID] = None,
    status_id: Optional[UUID] = None,
    ownerapp_id: Optional[str] = None,
    action: Optional[str] = None,
    model_name: Optional[str] = None,
    error: Optional[bool] = None,
    abort: Optional[bool] = None,
    stream: Optional[bool] = None,
    processing: Optional[bool] = None,
    orderby: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[ModelServiceRequestLogResponse]:
    """分页查询模型服务接口请求记录。"""
    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    normalized_action = _normalize_optional_str(action)
    normalized_model_name = _normalize_optional_str(model_name)

    request_logs = await select_proxy_node_status_logs(
        log_ids=[log_id] if log_id else None,
        node_ids=[node_id] if node_id else None,
        proxy_ids=[proxy_id] if proxy_id else None,
        status_ids=[status_id] if status_id else None,
        ownerapp_id=normalized_ownerapp_id,
        action=normalized_action,
        model_name=normalized_model_name,
        error=error,
        abort=abort,
        stream=stream,
        processing=processing,
        orderby=orderby,
        offset=safe_offset,
        limit=safe_limit,
        session=session,
    )

    raw_total = await count_proxy_node_status_logs(
        log_ids=[log_id] if log_id else None,
        node_ids=[node_id] if node_id else None,
        proxy_ids=[proxy_id] if proxy_id else None,
        status_ids=[status_id] if status_id else None,
        ownerapp_id=normalized_ownerapp_id,
        action=normalized_action,
        model_name=normalized_model_name,
        error=error,
        abort=abort,
        stream=stream,
        processing=processing,
        session=session,
    )

    total = raw_total if isinstance(raw_total, int) else raw_total[0]
    payload = [
        ModelServiceRequestLogResponse.model_validate(item, from_attributes=True)
        for item in request_logs
    ]

    return PageResponse[ModelServiceRequestLogResponse](
        offset=safe_offset,
        total=int(total),
        data=payload,
    )
