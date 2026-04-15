from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from openaiproxy.api.schemas import (
    AppMonthlyUsageTotalResponse,
    AppMonthlyModelUsageResponse,
    AppYearlyModelUsageResponse,
    AppYearlyUsageTotalResponse,
    ModelServiceRequestLogResponse,
    PageResponse,
)
from openaiproxy.api.utils import AsyncDbSession, check_api_key
from openaiproxy.services.database.models.node.crud import (
    count_app_monthly_model_usages,
    count_app_monthly_total_usages,
    count_app_yearly_model_usages,
    count_app_yearly_total_usages,
    select_app_monthly_model_usages,
    select_app_monthly_total_usages,
    select_app_yearly_model_usages,
    select_app_yearly_total_usages,
)
from openaiproxy.services.database.models.proxy.crud import (
    count_proxy_node_status_logs,
    select_proxy_node_status_logs,
)
from openaiproxy.utils.timezone import current_time_in_timezone, current_timezone


router = APIRouter(tags=["模型服务请求记录管理"])


def _normalize_optional_str(value: Optional[str]) -> Optional[str]:
    """清理可选字符串参数，空白字符串视为 None。"""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _parse_month_start(month: str) -> datetime:
    """Parse YYYY-MM to timezone-aware month start datetime."""

    try:
        parsed = datetime.strptime(month, "%Y-%m")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="month格式错误，必须为YYYY-MM",
        ) from exc
    return parsed.replace(tzinfo=current_timezone(), day=1, hour=0, minute=0, second=0, microsecond=0)


def _parse_year_start(year: str) -> datetime:
    """Parse YYYY to timezone-aware year start datetime."""

    try:
        parsed = datetime.strptime(year, "%Y")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="year格式错误，必须为YYYY",
        ) from exc
    return parsed.replace(
        tzinfo=current_timezone(),
        month=1,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def _parse_csv_values(values: Optional[str]) -> Optional[list[str]]:
    """解析逗号分隔的参数值并去除空白。"""

    normalized = _normalize_optional_str(values)
    if normalized is None:
        return None

    parsed_values = [item.strip() for item in normalized.split(",") if item.strip()]
    return parsed_values or None


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
    action: Optional[str] = "completions,embeddings,rerankdocs",
    model_name: Optional[str] = None,
    error: Optional[bool] = None,
    abort: Optional[bool] = None,
    stream: Optional[bool] = None,
    processing: Optional[bool] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
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
    normalized_model_name = _normalize_optional_str(model_name)
    action_values = action.split(',') if action is not None else None

    request_logs = await select_proxy_node_status_logs(
        log_ids=[log_id] if log_id else None,
        node_ids=[node_id] if node_id else None,
        proxy_ids=[proxy_id] if proxy_id else None,
        status_ids=[status_id] if status_id else None,
        actions=action_values,
        ownerapp_id=normalized_ownerapp_id,
        model_name=normalized_model_name,
        error=error,
        abort=abort,
        stream=stream,
        processing=processing,
        start_time=start_time,
        end_time=end_time,
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
        actions=action_values,
        ownerapp_id=normalized_ownerapp_id,
        model_name=normalized_model_name,
        error=error,
        abort=abort,
        stream=stream,
        processing=processing,
        start_time=start_time,
        end_time=end_time,
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


@router.get(
    "/request-logs/monthly-usage",
    dependencies=[Depends(check_api_key)],
    summary="按应用按月查询模型用量",
)
async def list_monthly_model_usage(
    ownerapp_id: Optional[str] = None,
    month: Optional[str] = None,
    models: Optional[str] = None,
    orderby: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[AppMonthlyModelUsageResponse]:
    """分页查询应用月度模型用量。"""

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)
    month_start = _parse_month_start(month.strip()) if month and month.strip() else None

    monthly_rows = await select_app_monthly_model_usages(
        ownerapp_id=normalized_ownerapp_id,
        month_start=month_start,
        model_names=model_names,
        orderby=orderby,
        offset=safe_offset,
        limit=safe_limit,
        session=session,
    )
    raw_total = await count_app_monthly_model_usages(
        ownerapp_id=normalized_ownerapp_id,
        month_start=month_start,
        model_names=model_names,
        session=session,
    )

    total = raw_total if isinstance(raw_total, int) else raw_total[0]
    payload = [
        AppMonthlyModelUsageResponse.model_validate(item, from_attributes=True)
        for item in monthly_rows
    ]

    return PageResponse[AppMonthlyModelUsageResponse](
        offset=safe_offset,
        total=int(total),
        data=payload,
    )


@router.get(
    "/request-logs/yearly-usage",
    dependencies=[Depends(check_api_key)],
    summary="按应用按年查询模型用量",
)
async def list_yearly_model_usage(
    ownerapp_id: Optional[str] = None,
    year: Optional[str] = None,
    models: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[AppYearlyModelUsageResponse]:
    """分页查询应用年度模型用量。"""

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)
    normalized_year = year.strip() if year and year.strip() else str(current_time_in_timezone().year)
    year_start = _parse_year_start(normalized_year)
    year_end = year_start.replace(year=year_start.year + 1)

    yearly_rows = await select_app_yearly_model_usages(
        ownerapp_id=normalized_ownerapp_id,
        year_start=year_start,
        year_end=year_end,
        model_names=model_names,
        offset=safe_offset,
        limit=safe_limit,
        session=session,
    )
    raw_total = await count_app_yearly_model_usages(
        ownerapp_id=normalized_ownerapp_id,
        year_start=year_start,
        year_end=year_end,
        model_names=model_names,
        session=session,
    )

    total = raw_total if isinstance(raw_total, int) else raw_total[0]
    payload = [
        AppYearlyModelUsageResponse(
            ownerapp_id=item.ownerapp_id,
            model_name=item.model_name,
            year=year_start.year,
            call_count=item.call_count,
            request_tokens=item.request_tokens,
            response_tokens=item.response_tokens,
            total_tokens=item.total_tokens,
        )
        for item in yearly_rows
    ]

    return PageResponse[AppYearlyModelUsageResponse](
        offset=safe_offset,
        total=int(total),
        data=payload,
    )


@router.get(
    "/request-logs/yearly-usage-total",
    dependencies=[Depends(check_api_key)],
    summary="按应用按年查询模型用量总计",
)
async def list_yearly_usage_total(
    ownerapp_id: Optional[str] = None,
    year: Optional[str] = None,
    models: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[AppYearlyUsageTotalResponse]:
    """分页查询应用年度模型用量总计（不分模型）。"""

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)
    normalized_year = year.strip() if year and year.strip() else str(current_time_in_timezone().year)
    year_start = _parse_year_start(normalized_year)
    year_end = year_start.replace(year=year_start.year + 1)

    yearly_rows = await select_app_yearly_total_usages(
        ownerapp_id=normalized_ownerapp_id,
        year_start=year_start,
        year_end=year_end,
        model_names=model_names,
        offset=safe_offset,
        limit=safe_limit,
        session=session,
    )
    raw_total = await count_app_yearly_total_usages(
        ownerapp_id=normalized_ownerapp_id,
        year_start=year_start,
        year_end=year_end,
        model_names=model_names,
        session=session,
    )

    total = raw_total if isinstance(raw_total, int) else raw_total[0]
    payload = [
        AppYearlyUsageTotalResponse(
            ownerapp_id=item.ownerapp_id,
            year=year_start.year,
            call_count=item.call_count,
            request_tokens=item.request_tokens,
            response_tokens=item.response_tokens,
            total_tokens=item.total_tokens,
        )
        for item in yearly_rows
    ]

    return PageResponse[AppYearlyUsageTotalResponse](
        offset=safe_offset,
        total=int(total),
        data=payload,
    )


@router.get(
    "/request-logs/monthly-usage-total",
    dependencies=[Depends(check_api_key)],
    summary="按应用按月查询模型用量总计",
)
async def list_monthly_usage_total(
    ownerapp_id: Optional[str] = None,
    month: Optional[str] = None,
    models: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[AppMonthlyUsageTotalResponse]:
    """分页查询应用月度模型用量总计（不分模型）。"""

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)
    normalized_month = month.strip() if month and month.strip() else current_time_in_timezone().strftime("%Y-%m")
    month_start = _parse_month_start(normalized_month)

    monthly_rows = await select_app_monthly_total_usages(
        ownerapp_id=normalized_ownerapp_id,
        month_start=month_start,
        model_names=model_names,
        offset=safe_offset,
        limit=safe_limit,
        session=session,
    )
    raw_total = await count_app_monthly_total_usages(
        ownerapp_id=normalized_ownerapp_id,
        month_start=month_start,
        model_names=model_names,
        session=session,
    )

    total = raw_total if isinstance(raw_total, int) else raw_total[0]
    payload = [
        AppMonthlyUsageTotalResponse(
            ownerapp_id=item.ownerapp_id,
            month_start=month_start,
            call_count=item.call_count,
            request_tokens=item.request_tokens,
            response_tokens=item.response_tokens,
            total_tokens=item.total_tokens,
        )
        for item in monthly_rows
    ]

    return PageResponse[AppMonthlyUsageTotalResponse](
        offset=safe_offset,
        total=int(total),
        data=payload,
    )
