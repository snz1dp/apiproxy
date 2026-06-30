from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status

from openaiproxy.api.schemas import (
    AppDailyModelUsageResponse,
    AppMonthlyUsageTotalResponse,
    AppMonthlyModelUsageResponse,
    AppWeeklyModelUsageResponse,
    AppYearlyModelUsageResponse,
    AppYearlyUsageTotalResponse,
    ModelServiceRequestLogResponse,
    PageResponse,
)
from openaiproxy.api.utils import AsyncDbSession, check_api_key
from openaiproxy.services.database.models.node.crud import (
    _merge_model_aggregates,
    _merge_total_aggregates,
    count_app_daily_model_usages,
    count_app_monthly_model_usages,
    count_app_monthly_total_usages,
    count_app_weekly_model_usages,
    count_app_yearly_model_usages,
    count_app_yearly_total_usages,
    select_app_daily_model_usages,
    select_app_daily_model_usages_range,
    select_app_daily_model_usage_totals_range,
    select_app_monthly_model_usages,
    select_app_monthly_model_usages_range,
    select_app_monthly_model_usage_totals_range,
    select_app_monthly_total_usages,
    select_app_weekly_model_usages,
    select_app_yearly_model_usages,
    select_app_yearly_total_usages,
    select_realtime_model_usages,
    select_realtime_model_usage_totals,
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


def _parse_day_start(day: str) -> datetime:
    """Parse YYYY-MM-DD to timezone-aware day start datetime."""

    try:
        parsed = datetime.strptime(day, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="day格式错误，必须为YYYY-MM-DD",
        ) from exc
    return parsed.replace(tzinfo=current_timezone(), hour=0, minute=0, second=0, microsecond=0)


def _parse_week_start(week_start: str) -> datetime:
    """Parse YYYY-MM-DD to timezone-aware week start datetime."""

    try:
        parsed = datetime.strptime(week_start, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="week_start格式错误，必须为YYYY-MM-DD",
        ) from exc

    normalized = parsed.replace(tzinfo=current_timezone(), hour=0, minute=0, second=0, microsecond=0)
    if normalized.weekday() != 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="week_start必须为周一日期",
        )
    return normalized


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


def _today_start() -> datetime:
    """获取当天00:00的时区感知时间。"""

    now = current_time_in_timezone()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _is_today(day: datetime) -> bool:
    """判断给定日期是否为今天。"""

    today = _today_start()
    return day.date() == today.date()


def _current_week_start() -> datetime:
    """获取本周一00:00的时区感知时间。"""

    today = _today_start()
    return today - timedelta(days=today.weekday())


def _current_month_start() -> datetime:
    """获取本月1号00:00的时区感知时间。"""

    today = _today_start()
    return today.replace(day=1)


def _current_year_start() -> datetime:
    """获取本年1月1号00:00的时区感知时间。"""

    today = _today_start()
    return today.replace(month=1, day=1)


def _apply_pagination(items: list, offset: int, limit: Optional[int]) -> list:
    """对列表应用分页。"""

    paginated = items[offset:]
    if limit is not None and limit > 0:
        paginated = paginated[:limit]
    return paginated


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
    action: Optional[str] = "completions,responses,embeddings,rerankdocs",
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
    "/request-logs/daily-usage",
    dependencies=[Depends(check_api_key)],
    summary="按应用按天查询模型用量",
)
async def list_daily_model_usage(
    ownerapp_id: Optional[str] = None,
    day: Optional[str] = None,
    models: Optional[str] = None,
    orderby: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[AppDailyModelUsageResponse]:
    """分页查询应用日度模型用量。

    每天数据为当晚定时聚合，因此查询今天数据时实时从请求日志表聚合。
    """

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)

    # 未指定day时默认今天
    if day and day.strip():
        day_start = _parse_day_start(day.strip())
    else:
        day_start = _today_start()

    now = current_time_in_timezone()

    if _is_today(day_start):
        # 今天：实时从 ProxyNodeStatusLog 聚合
        day_end = day_start + timedelta(days=1)
        realtime_rows = await select_realtime_model_usages(
            start_time=day_start,
            end_time=day_end,
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )
        total = len(realtime_rows)
        paginated_rows = _apply_pagination(realtime_rows, safe_offset, safe_limit)
        payload = [
            AppDailyModelUsageResponse(
                id=uuid4(),
                ownerapp_id=item.ownerapp_id,
                model_name=item.model_name,
                day_start=day_start,
                call_count=item.call_count,
                request_tokens=item.request_tokens,
                response_tokens=item.response_tokens,
                total_tokens=item.total_tokens,
                created_at=now,
                updated_at=now,
            )
            for item in paginated_rows
        ]
    else:
        # 过去：从 AppDailyModelUsage 表查询
        daily_rows = await select_app_daily_model_usages(
            ownerapp_id=normalized_ownerapp_id,
            day_start=day_start,
            model_names=model_names,
            orderby=orderby,
            offset=safe_offset,
            limit=safe_limit,
            session=session,
        )
        raw_total = await count_app_daily_model_usages(
            ownerapp_id=normalized_ownerapp_id,
            day_start=day_start,
            model_names=model_names,
            session=session,
        )
        total = raw_total if isinstance(raw_total, int) else raw_total[0]
        payload = [
            AppDailyModelUsageResponse.model_validate(item, from_attributes=True)
            for item in daily_rows
        ]

    return PageResponse[AppDailyModelUsageResponse](
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
    """分页查询应用月度模型用量。

    每月数据为月底定时聚合，因此查询本月数据时需要合并日表（已过天数）和实时查询（今天）。
    """

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)

    # 未指定month时默认本月
    if month and month.strip():
        month_start = _parse_month_start(month.strip())
    else:
        month_start = _current_month_start()

    today = _today_start()
    now = current_time_in_timezone()

    if month_start.date() == _current_month_start().date():
        # 本月：日表（本月已过天数，不含今天）+ 实时（今天）
        daily_rows = await select_app_daily_model_usages_range(
            day_start=month_start,
            day_end=today,
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )
        realtime_rows = await select_realtime_model_usages(
            start_time=today,
            end_time=today + timedelta(days=1),
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )
        merged = _merge_model_aggregates(daily_rows, realtime_rows)
        total = len(merged)
        paginated_rows = _apply_pagination(merged, safe_offset, safe_limit)
        payload = [
            AppMonthlyModelUsageResponse(
                id=uuid4(),
                ownerapp_id=item.ownerapp_id,
                model_name=item.model_name,
                month_start=month_start,
                call_count=item.call_count,
                request_tokens=item.request_tokens,
                response_tokens=item.response_tokens,
                total_tokens=item.total_tokens,
                created_at=now,
                updated_at=now,
            )
            for item in paginated_rows
        ]
    else:
        # 过去：从 AppMonthlyModelUsage 表查询
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
    "/request-logs/weekly-usage",
    dependencies=[Depends(check_api_key)],
    summary="按应用按周查询模型用量",
)
async def list_weekly_model_usage(
    ownerapp_id: Optional[str] = None,
    week_start: Optional[str] = None,
    models: Optional[str] = None,
    orderby: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[AppWeeklyModelUsageResponse]:
    """分页查询应用周度模型用量。

    每周数据为周末定时聚合，因此查询本周数据时需要合并日表（已过天数）和实时查询（今天）。
    """

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)

    # 未指定week_start时默认本周
    if week_start and week_start.strip():
        normalized_week_start = _parse_week_start(week_start.strip())
    else:
        normalized_week_start = _current_week_start()

    today = _today_start()
    now = current_time_in_timezone()

    if normalized_week_start.date() == _current_week_start().date():
        # 本周：日表（本周已过天数，不含今天）+ 实时（今天）
        daily_rows = await select_app_daily_model_usages_range(
            day_start=normalized_week_start,
            day_end=today,
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )
        realtime_rows = await select_realtime_model_usages(
            start_time=today,
            end_time=today + timedelta(days=1),
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )
        merged = _merge_model_aggregates(daily_rows, realtime_rows)
        total = len(merged)
        paginated_rows = _apply_pagination(merged, safe_offset, safe_limit)
        payload = [
            AppWeeklyModelUsageResponse(
                id=uuid4(),
                ownerapp_id=item.ownerapp_id,
                model_name=item.model_name,
                week_start=normalized_week_start,
                call_count=item.call_count,
                request_tokens=item.request_tokens,
                response_tokens=item.response_tokens,
                total_tokens=item.total_tokens,
                created_at=now,
                updated_at=now,
            )
            for item in paginated_rows
        ]
    else:
        # 过去：从 AppWeeklyModelUsage 表查询
        weekly_rows = await select_app_weekly_model_usages(
            ownerapp_id=normalized_ownerapp_id,
            week_start=normalized_week_start,
            model_names=model_names,
            orderby=orderby,
            offset=safe_offset,
            limit=safe_limit,
            session=session,
        )
        raw_total = await count_app_weekly_model_usages(
            ownerapp_id=normalized_ownerapp_id,
            week_start=normalized_week_start,
            model_names=model_names,
            session=session,
        )
        total = raw_total if isinstance(raw_total, int) else raw_total[0]
        payload = [
            AppWeeklyModelUsageResponse.model_validate(item, from_attributes=True)
            for item in weekly_rows
        ]

    return PageResponse[AppWeeklyModelUsageResponse](
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
    """分页查询应用年度模型用量。

    年度数据为年底定时聚合，因此查询本年数据时需要合并：
    月表（已过月份）+ 日表（本月已过天数，不含今天）+ 实时（今天）。
    """

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)
    normalized_year = year.strip() if year and year.strip() else str(current_time_in_timezone().year)
    year_start = _parse_year_start(normalized_year)
    year_end = year_start.replace(year=year_start.year + 1)

    today = _today_start()
    current_month_start = _current_month_start()

    if year_start.date() == _current_year_start().date():
        # 本年：月表（已过月份）+ 日表（本月已过天数，不含今天）+ 实时（今天）
        monthly_rows = await select_app_monthly_model_usages_range(
            month_start=year_start,
            month_end=current_month_start,
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )
        daily_rows = await select_app_daily_model_usages_range(
            day_start=current_month_start,
            day_end=today,
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )
        realtime_rows = await select_realtime_model_usages(
            start_time=today,
            end_time=today + timedelta(days=1),
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )
        merged = _merge_model_aggregates(monthly_rows, daily_rows, realtime_rows)
        total = len(merged)
        paginated_rows = _apply_pagination(merged, safe_offset, safe_limit)
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
            for item in paginated_rows
        ]
    else:
        # 过去：从 AppMonthlyModelUsage 表聚合
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
    """分页查询应用年度模型用量总计（不分模型）。

    年度数据为年底定时聚合，因此查询本年数据时需要合并：
    月表总计（已过月份）+ 日表总计（本月已过天数，不含今天）+ 实时总计（今天）。
    """

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)
    normalized_year = year.strip() if year and year.strip() else str(current_time_in_timezone().year)
    year_start = _parse_year_start(normalized_year)
    year_end = year_start.replace(year=year_start.year + 1)

    today = _today_start()
    current_month_start = _current_month_start()

    if year_start.date() == _current_year_start().date():
        # 本年：月表总计（已过月份）+ 日表总计（本月已过天数）+ 实时总计（今天）
        monthly_totals = await select_app_monthly_model_usage_totals_range(
            month_start=year_start,
            month_end=current_month_start,
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )
        daily_totals = await select_app_daily_model_usage_totals_range(
            day_start=current_month_start,
            day_end=today,
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )
        realtime_totals = await select_realtime_model_usage_totals(
            start_time=today,
            end_time=today + timedelta(days=1),
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )
        merged = _merge_total_aggregates(monthly_totals, daily_totals, realtime_totals)
        total = len(merged)
        paginated_rows = _apply_pagination(merged, safe_offset, safe_limit)
        payload = [
            AppYearlyUsageTotalResponse(
                ownerapp_id=item.ownerapp_id,
                year=year_start.year,
                call_count=item.call_count,
                request_tokens=item.request_tokens,
                response_tokens=item.response_tokens,
                total_tokens=item.total_tokens,
            )
            for item in paginated_rows
        ]
    else:
        # 过去：从 AppMonthlyModelUsage 表聚合
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
    """分页查询应用月度模型用量总计（不分模型）。

    每月数据为月底定时聚合，因此查询本月数据时需要合并：
    日表总计（已过天数，不含今天）+ 实时总计（今天）。
    """

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)
    normalized_month = month.strip() if month and month.strip() else current_time_in_timezone().strftime("%Y-%m")
    month_start = _parse_month_start(normalized_month)

    today = _today_start()

    if month_start.date() == _current_month_start().date():
        # 本月：日表总计（已过天数，不含今天）+ 实时总计（今天）
        daily_totals = await select_app_daily_model_usage_totals_range(
            day_start=month_start,
            day_end=today,
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )
        realtime_totals = await select_realtime_model_usage_totals(
            start_time=today,
            end_time=today + timedelta(days=1),
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )
        merged = _merge_total_aggregates(daily_totals, realtime_totals)
        total = len(merged)
        paginated_rows = _apply_pagination(merged, safe_offset, safe_limit)
        payload = [
            AppMonthlyUsageTotalResponse(
                ownerapp_id=item.ownerapp_id,
                month_start=month_start,
                call_count=item.call_count,
                request_tokens=item.request_tokens,
                response_tokens=item.response_tokens,
                total_tokens=item.total_tokens,
            )
            for item in paginated_rows
        ]
    else:
        # 过去：从 AppMonthlyModelUsage 表聚合
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
