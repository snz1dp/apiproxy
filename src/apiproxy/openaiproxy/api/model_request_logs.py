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
    _merge_model_aggregates_by_period,
    _merge_total_aggregates_by_period,
    select_app_daily_model_usage_totals_by_range,
    select_app_daily_model_usages_by_range,
    select_app_monthly_model_usage_totals_by_range,
    select_app_monthly_model_usages_by_range,
    select_app_weekly_model_usage_totals_by_range,
    select_app_weekly_model_usages_by_range,
    select_app_yearly_model_usage_totals_by_range,
    select_app_yearly_model_usages_by_range,
    select_realtime_model_usage_totals_by_day,
    select_realtime_model_usages_by_day,
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


def _parse_date_start(date_str: str, param_name: str = "date") -> datetime:
    """解析 YYYY-MM-DD 为时区感知的当天00:00时间。"""

    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{param_name}格式错误，必须为YYYY-MM-DD",
        ) from exc
    return parsed.replace(tzinfo=current_timezone(), hour=0, minute=0, second=0, microsecond=0)


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


def _parse_date_range(start_date: Optional[str], end_date: Optional[str]) -> tuple[datetime, datetime]:
    """解析 start_date 和 end_date 参数，返回闭区间 [start, end] 的时间范围。

    如果未传参数，默认查询当天。

    Args:
        start_date: 起始日期字符串（YYYY-MM-DD）。
        end_date: 结束日期字符串（YYYY-MM-DD）。

    Returns:
        (start_datetime, end_datetime_inclusive) 元组，start 为当天00:00，end_inclusive 为当天23:59:59.999999。
    """

    normalized_start = _normalize_optional_str(start_date)
    normalized_end = _normalize_optional_str(end_date)

    if normalized_start is None and normalized_end is None:
        # 默认查今天
        today = _today_start()
        return today, today

    if normalized_start is None:
        normalized_start = normalized_end
    if normalized_end is None:
        normalized_end = normalized_start

    start_dt = _parse_date_start(normalized_start, "start_date")
    end_dt = _parse_date_start(normalized_end, "end_date")

    if end_dt < start_dt:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end_date不能早于start_date",
        )

    return start_dt, end_dt


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
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    models: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[AppDailyModelUsageResponse]:
    """分页查询应用日度模型用量，按天分组返回。

    支持通过 start_date 和 end_date 指定日期范围（闭区间，YYYY-MM-DD）。
    未传参数时默认查询当天。范围跨越今天时，今天的数据从请求日志表实时聚合。
    """

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)

    range_start, range_end = _parse_date_range(start_date, end_date)
    # end_date 包含当天，所以查询上界为 end_date + 1天
    range_end_exclusive = range_end + timedelta(days=1)

    today = _today_start()
    now = current_time_in_timezone()

    # 历史部分：日表中 day_start 在 [range_start, min(range_end, today)) 的记录
    history_end = min(range_end_exclusive, today)
    history_rows: list = []
    if range_start < history_end:
        history_rows = await select_app_daily_model_usages_by_range(
            day_start=range_start,
            day_end=history_end,
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )

    # 实时部分：如果范围包含今天，从 ProxyNodeStatusLog 实时聚合今天的数据
    realtime_rows: list = []
    if range_end_exclusive > today:
        realtime_rows = await select_realtime_model_usages_by_day(
            day_start=today,
            day_end=range_end_exclusive,
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )

    # 合并：相同 (ownerapp_id, model_name, day_start) 的记录累加
    merged = _merge_model_aggregates_by_period(history_rows, realtime_rows)
    total = len(merged)
    paginated_rows = _apply_pagination(merged, safe_offset, safe_limit)
    payload = [
        AppDailyModelUsageResponse(
            id=uuid4(),
            ownerapp_id=item.ownerapp_id,
            model_name=item.model_name,
            day_start=item.day_start,
            call_count=item.call_count,
            request_tokens=item.request_tokens,
            response_tokens=item.response_tokens,
            total_tokens=item.total_tokens,
            created_at=now,
            updated_at=now,
        )
        for item in paginated_rows
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
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    models: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[AppMonthlyModelUsageResponse]:
    """分页查询应用月度模型用量，按月分组返回。

    支持通过 start_date 和 end_date 指定日期范围（闭区间，YYYY-MM-DD）。
    未传参数时默认查询当月。范围跨越本月时，本月数据需要合并日表（已过天数）和实时查询（今天）。
    """

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)

    range_start, range_end = _parse_date_range(start_date, end_date)
    range_end_exclusive = range_end + timedelta(days=1)

    today = _today_start()
    current_month_start = _current_month_start()
    now = current_time_in_timezone()

    # 计算涉及的月份范围
    query_month_start = range_start.replace(day=1)
    query_month_end = (range_end.replace(day=1) + timedelta(days=32)).replace(day=1)

    # 历史部分：月表中 month_start 在 [query_month_start, min(query_month_end, current_month_start)) 的记录
    history_month_end = min(query_month_end, current_month_start)
    history_rows: list = []
    if query_month_start < history_month_end:
        history_rows = await select_app_monthly_model_usages_by_range(
            month_start=query_month_start,
            month_end=history_month_end,
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )

    # 本月部分：如果范围包含本月，合并日表（本月已过天数，不含今天）+ 实时（今天）
    current_month_rows: list = []
    if range_end_exclusive > current_month_start and range_start < today + timedelta(days=1):
        # 日表中本月已过天数的数据
        daily_end = min(today, range_end_exclusive)
        daily_rows: list = []
        if current_month_start < daily_end:
            daily_rows = await select_app_daily_model_usages_by_range(
                day_start=current_month_start,
                day_end=daily_end,
                ownerapp_id=normalized_ownerapp_id,
                model_names=model_names,
                session=session,
            )
            # 将 DailyUsageAggregate 转换为 MonthlyUsageAggregate（保持 day_start 作为 month_start）
            daily_rows = [
                type(
                    "MonthlyUsageAggregate",
                    (),
                    {
                        "ownerapp_id": item.ownerapp_id,
                        "model_name": item.model_name,
                        "call_count": item.call_count,
                        "request_tokens": item.request_tokens,
                        "response_tokens": item.response_tokens,
                        "total_tokens": item.total_tokens,
                        "month_start": current_month_start,
                    },
                )
                for item in daily_rows
            ]

        # 实时部分：今天的数据
        realtime_rows: list = []
        if range_end_exclusive > today:
            realtime_rows = await select_realtime_model_usages_by_day(
                day_start=today,
                day_end=range_end_exclusive,
                ownerapp_id=normalized_ownerapp_id,
                model_names=model_names,
                session=session,
            )
            # 将 DailyUsageAggregate 转换为 MonthlyUsageAggregate
            realtime_rows = [
                type(
                    "MonthlyUsageAggregate",
                    (),
                    {
                        "ownerapp_id": item.ownerapp_id,
                        "model_name": item.model_name,
                        "call_count": item.call_count,
                        "request_tokens": item.request_tokens,
                        "response_tokens": item.response_tokens,
                        "total_tokens": item.total_tokens,
                        "month_start": current_month_start,
                    },
                )
                for item in realtime_rows
            ]

        # 合并日表和实时数据，按 (ownerapp_id, model_name, month_start=current_month_start) 聚合
        current_month_rows = _merge_model_aggregates_by_period(daily_rows, realtime_rows)

    # 合并历史月表数据和本月数据
    merged = _merge_model_aggregates_by_period(history_rows, current_month_rows)
    total = len(merged)
    paginated_rows = _apply_pagination(merged, safe_offset, safe_limit)
    payload = [
        AppMonthlyModelUsageResponse(
            id=uuid4(),
            ownerapp_id=item.ownerapp_id,
            model_name=item.model_name,
            month_start=item.month_start,
            call_count=item.call_count,
            request_tokens=item.request_tokens,
            response_tokens=item.response_tokens,
            total_tokens=item.total_tokens,
            created_at=now,
            updated_at=now,
        )
        for item in paginated_rows
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
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    models: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[AppWeeklyModelUsageResponse]:
    """分页查询应用周度模型用量，按周分组返回。

    支持通过 start_date 和 end_date 指定日期范围（闭区间，YYYY-MM-DD）。
    未传参数时默认查询本周。范围跨越本周时，本周数据需要合并日表（已过天数）和实时查询（今天）。
    """

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)

    range_start, range_end = _parse_date_range(start_date, end_date)
    range_end_exclusive = range_end + timedelta(days=1)

    today = _today_start()
    current_week_start = _current_week_start()
    now = current_time_in_timezone()

    # 计算涉及的周范围：将 range_start 对齐到周一
    query_week_start = range_start - timedelta(days=range_start.weekday())
    # 将 range_end 对齐到下周一
    query_week_end = (range_end + timedelta(days=1) + timedelta(days=6 - range_end.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    # 历史部分：周表中 week_start 在 [query_week_start, min(query_week_end, current_week_start)) 的记录
    history_week_end = min(query_week_end, current_week_start)
    history_rows: list = []
    if query_week_start < history_week_end:
        history_rows = await select_app_weekly_model_usages_by_range(
            week_start=query_week_start,
            week_end=history_week_end,
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )

    # 本周部分：如果范围包含本周，合并日表（本周已过天数，不含今天）+ 实时（今天）
    current_week_rows: list = []
    if range_end_exclusive > current_week_start and range_start < today + timedelta(days=1):
        # 日表中本周已过天数的数据
        daily_end = min(today, range_end_exclusive)
        daily_rows: list = []
        if current_week_start < daily_end:
            daily_rows = await select_app_daily_model_usages_by_range(
                day_start=current_week_start,
                day_end=daily_end,
                ownerapp_id=normalized_ownerapp_id,
                model_names=model_names,
                session=session,
            )
            # 将 DailyUsageAggregate 转换为带 week_start 的对象
            daily_rows = [
                type(
                    "WeeklyUsageAggregate",
                    (),
                    {
                        "ownerapp_id": item.ownerapp_id,
                        "model_name": item.model_name,
                        "call_count": item.call_count,
                        "request_tokens": item.request_tokens,
                        "response_tokens": item.response_tokens,
                        "total_tokens": item.total_tokens,
                        "week_start": current_week_start,
                    },
                )
                for item in daily_rows
            ]

        # 实时部分：今天的数据
        realtime_rows: list = []
        if range_end_exclusive > today:
            realtime_rows = await select_realtime_model_usages_by_day(
                day_start=today,
                day_end=range_end_exclusive,
                ownerapp_id=normalized_ownerapp_id,
                model_names=model_names,
                session=session,
            )
            realtime_rows = [
                type(
                    "WeeklyUsageAggregate",
                    (),
                    {
                        "ownerapp_id": item.ownerapp_id,
                        "model_name": item.model_name,
                        "call_count": item.call_count,
                        "request_tokens": item.request_tokens,
                        "response_tokens": item.response_tokens,
                        "total_tokens": item.total_tokens,
                        "week_start": current_week_start,
                    },
                )
                for item in realtime_rows
            ]

        current_week_rows = _merge_model_aggregates_by_period(daily_rows, realtime_rows)

    # 合并历史周表数据和本周数据
    merged = _merge_model_aggregates_by_period(history_rows, current_week_rows)
    total = len(merged)
    paginated_rows = _apply_pagination(merged, safe_offset, safe_limit)
    payload = [
        AppWeeklyModelUsageResponse(
            id=uuid4(),
            ownerapp_id=item.ownerapp_id,
            model_name=item.model_name,
            week_start=item.week_start,
            call_count=item.call_count,
            request_tokens=item.request_tokens,
            response_tokens=item.response_tokens,
            total_tokens=item.total_tokens,
            created_at=now,
            updated_at=now,
        )
        for item in paginated_rows
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
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    models: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[AppYearlyModelUsageResponse]:
    """分页查询应用年度模型用量，按年分组返回。

    支持通过 start_date 和 end_date 指定日期范围（闭区间，YYYY-MM-DD）。
    未传参数时默认查询本年。范围跨越本年时，本年数据需要合并：
    月表（已过月份）+ 日表（本月已过天数，不含今天）+ 实时（今天）。
    """

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)

    range_start, range_end = _parse_date_range(start_date, end_date)
    range_end_exclusive = range_end + timedelta(days=1)

    today = _today_start()
    current_month_start = _current_month_start()
    current_year_start = _current_year_start()

    # 计算涉及的年份范围
    query_year_start = range_start.year
    query_year_end = range_end.year + 1

    # 历史部分：月表中 year 在 [query_year_start, min(query_year_end, current_year)) 的记录
    history_year_end = min(query_year_end, current_year_start.year)
    history_rows: list = []
    if query_year_start < history_year_end:
        history_rows = await select_app_yearly_model_usages_by_range(
            year_start=query_year_start,
            year_end=history_year_end,
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )

    # 本年部分：如果范围包含本年
    current_year_rows: list = []
    if range_end_exclusive > current_year_start and range_start < today + timedelta(days=1):
        # 月表（已过月份）
        monthly_rows: list = []
        if current_year_start < current_month_start:
            monthly_rows = await select_app_monthly_model_usages_by_range(
                month_start=current_year_start,
                month_end=current_month_start,
                ownerapp_id=normalized_ownerapp_id,
                model_names=model_names,
                session=session,
            )
            # 转换为带 year 字段的对象
            monthly_rows = [
                type(
                    "YearlyUsageAggregate",
                    (),
                    {
                        "ownerapp_id": item.ownerapp_id,
                        "model_name": item.model_name,
                        "call_count": item.call_count,
                        "request_tokens": item.request_tokens,
                        "response_tokens": item.response_tokens,
                        "total_tokens": item.total_tokens,
                        "year": current_year_start.year,
                    },
                )
                for item in monthly_rows
            ]

        # 日表（本月已过天数，不含今天）
        daily_end = min(today, range_end_exclusive)
        daily_rows: list = []
        if current_month_start < daily_end:
            daily_rows = await select_app_daily_model_usages_by_range(
                day_start=current_month_start,
                day_end=daily_end,
                ownerapp_id=normalized_ownerapp_id,
                model_names=model_names,
                session=session,
            )
            daily_rows = [
                type(
                    "YearlyUsageAggregate",
                    (),
                    {
                        "ownerapp_id": item.ownerapp_id,
                        "model_name": item.model_name,
                        "call_count": item.call_count,
                        "request_tokens": item.request_tokens,
                        "response_tokens": item.response_tokens,
                        "total_tokens": item.total_tokens,
                        "year": current_year_start.year,
                    },
                )
                for item in daily_rows
            ]

        # 实时（今天）
        realtime_rows: list = []
        if range_end_exclusive > today:
            realtime_rows = await select_realtime_model_usages_by_day(
                day_start=today,
                day_end=range_end_exclusive,
                ownerapp_id=normalized_ownerapp_id,
                model_names=model_names,
                session=session,
            )
            realtime_rows = [
                type(
                    "YearlyUsageAggregate",
                    (),
                    {
                        "ownerapp_id": item.ownerapp_id,
                        "model_name": item.model_name,
                        "call_count": item.call_count,
                        "request_tokens": item.request_tokens,
                        "response_tokens": item.response_tokens,
                        "total_tokens": item.total_tokens,
                        "year": current_year_start.year,
                    },
                )
                for item in realtime_rows
            ]

        current_year_rows = _merge_model_aggregates_by_period(monthly_rows, daily_rows, realtime_rows)

    # 合并历史年表数据和本年数据
    merged = _merge_model_aggregates_by_period(history_rows, current_year_rows)
    total = len(merged)
    paginated_rows = _apply_pagination(merged, safe_offset, safe_limit)
    payload = [
        AppYearlyModelUsageResponse(
            ownerapp_id=item.ownerapp_id,
            model_name=item.model_name,
            year=item.year,
            call_count=item.call_count,
            request_tokens=item.request_tokens,
            response_tokens=item.response_tokens,
            total_tokens=item.total_tokens,
        )
        for item in paginated_rows
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
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    models: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[AppYearlyUsageTotalResponse]:
    """分页查询应用年度模型用量总计（不分模型），按年分组返回。

    支持通过 start_date 和 end_date 指定日期范围（闭区间，YYYY-MM-DD）。
    未传参数时默认查询本年。范围跨越本年时，本年数据需要合并：
    月表总计（已过月份）+ 日表总计（本月已过天数）+ 实时总计（今天）。
    """

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)

    range_start, range_end = _parse_date_range(start_date, end_date)
    range_end_exclusive = range_end + timedelta(days=1)

    today = _today_start()
    current_month_start = _current_month_start()
    current_year_start = _current_year_start()

    # 计算涉及的年份范围
    query_year_start = range_start.year
    query_year_end = range_end.year + 1

    # 历史部分：月表中 year 在 [query_year_start, min(query_year_end, current_year)) 的记录
    history_year_end = min(query_year_end, current_year_start.year)
    history_rows: list = []
    if query_year_start < history_year_end:
        history_rows = await select_app_yearly_model_usage_totals_by_range(
            year_start=query_year_start,
            year_end=history_year_end,
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )

    # 本年部分
    current_year_rows: list = []
    if range_end_exclusive > current_year_start and range_start < today + timedelta(days=1):
        # 月表总计（已过月份）
        monthly_totals: list = []
        if current_year_start < current_month_start:
            monthly_totals = await select_app_monthly_model_usage_totals_by_range(
                month_start=current_year_start,
                month_end=current_month_start,
                ownerapp_id=normalized_ownerapp_id,
                model_names=model_names,
                session=session,
            )
            monthly_totals = [
                type(
                    "YearlyUsageTotalAggregate",
                    (),
                    {
                        "ownerapp_id": item.ownerapp_id,
                        "call_count": item.call_count,
                        "request_tokens": item.request_tokens,
                        "response_tokens": item.response_tokens,
                        "total_tokens": item.total_tokens,
                        "year": current_year_start.year,
                    },
                )
                for item in monthly_totals
            ]

        # 日表总计（本月已过天数，不含今天）
        daily_end = min(today, range_end_exclusive)
        daily_totals: list = []
        if current_month_start < daily_end:
            daily_totals = await select_app_daily_model_usage_totals_by_range(
                day_start=current_month_start,
                day_end=daily_end,
                ownerapp_id=normalized_ownerapp_id,
                model_names=model_names,
                session=session,
            )
            daily_totals = [
                type(
                    "YearlyUsageTotalAggregate",
                    (),
                    {
                        "ownerapp_id": item.ownerapp_id,
                        "call_count": item.call_count,
                        "request_tokens": item.request_tokens,
                        "response_tokens": item.response_tokens,
                        "total_tokens": item.total_tokens,
                        "year": current_year_start.year,
                    },
                )
                for item in daily_totals
            ]

        # 实时总计（今天）
        realtime_totals: list = []
        if range_end_exclusive > today:
            realtime_totals = await select_realtime_model_usage_totals_by_day(
                day_start=today,
                day_end=range_end_exclusive,
                ownerapp_id=normalized_ownerapp_id,
                model_names=model_names,
                session=session,
            )
            realtime_totals = [
                type(
                    "YearlyUsageTotalAggregate",
                    (),
                    {
                        "ownerapp_id": item.ownerapp_id,
                        "call_count": item.call_count,
                        "request_tokens": item.request_tokens,
                        "response_tokens": item.response_tokens,
                        "total_tokens": item.total_tokens,
                        "year": current_year_start.year,
                    },
                )
                for item in realtime_totals
            ]

        current_year_rows = _merge_total_aggregates_by_period(monthly_totals, daily_totals, realtime_totals)

    # 合并历史年表数据和本年数据
    merged = _merge_total_aggregates_by_period(history_rows, current_year_rows)
    total = len(merged)
    paginated_rows = _apply_pagination(merged, safe_offset, safe_limit)
    payload = [
        AppYearlyUsageTotalResponse(
            ownerapp_id=item.ownerapp_id,
            year=item.year,
            call_count=item.call_count,
            request_tokens=item.request_tokens,
            response_tokens=item.response_tokens,
            total_tokens=item.total_tokens,
        )
        for item in paginated_rows
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
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    models: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
    *,
    session: AsyncDbSession,
) -> PageResponse[AppMonthlyUsageTotalResponse]:
    """分页查询应用月度模型用量总计（不分模型），按月分组返回。

    支持通过 start_date 和 end_date 指定日期范围（闭区间，YYYY-MM-DD）。
    未传参数时默认查询当月。范围跨越本月时，本月数据需要合并：
    日表总计（已过天数，不含今天）+ 实时总计（今天）。
    """

    safe_offset = max(offset, 0)
    safe_limit = max(limit, 0) if limit is not None else None

    normalized_ownerapp_id = _normalize_optional_str(ownerapp_id)
    model_names = _parse_csv_values(models)

    range_start, range_end = _parse_date_range(start_date, end_date)
    range_end_exclusive = range_end + timedelta(days=1)

    today = _today_start()
    current_month_start = _current_month_start()

    # 计算涉及的月份范围
    query_month_start = range_start.replace(day=1)
    query_month_end = (range_end.replace(day=1) + timedelta(days=32)).replace(day=1)

    # 历史部分：月表中 month_start 在 [query_month_start, min(query_month_end, current_month_start)) 的记录
    history_month_end = min(query_month_end, current_month_start)
    history_rows: list = []
    if query_month_start < history_month_end:
        history_rows = await select_app_monthly_model_usage_totals_by_range(
            month_start=query_month_start,
            month_end=history_month_end,
            ownerapp_id=normalized_ownerapp_id,
            model_names=model_names,
            session=session,
        )

    # 本月部分：如果范围包含本月
    current_month_rows: list = []
    if range_end_exclusive > current_month_start and range_start < today + timedelta(days=1):
        # 日表总计（本月已过天数，不含今天）
        daily_end = min(today, range_end_exclusive)
        daily_totals: list = []
        if current_month_start < daily_end:
            daily_totals = await select_app_daily_model_usage_totals_by_range(
                day_start=current_month_start,
                day_end=daily_end,
                ownerapp_id=normalized_ownerapp_id,
                model_names=model_names,
                session=session,
            )
            # 将 month_start 统一设置为 current_month_start
            daily_totals = [
                type(
                    "MonthlyUsageTotalAggregate",
                    (),
                    {
                        "ownerapp_id": item.ownerapp_id,
                        "call_count": item.call_count,
                        "request_tokens": item.request_tokens,
                        "response_tokens": item.response_tokens,
                        "total_tokens": item.total_tokens,
                        "month_start": current_month_start,
                    },
                )
                for item in daily_totals
            ]

        # 实时总计（今天）
        realtime_totals: list = []
        if range_end_exclusive > today:
            realtime_totals = await select_realtime_model_usage_totals_by_day(
                day_start=today,
                day_end=range_end_exclusive,
                ownerapp_id=normalized_ownerapp_id,
                model_names=model_names,
                session=session,
            )
            realtime_totals = [
                type(
                    "MonthlyUsageTotalAggregate",
                    (),
                    {
                        "ownerapp_id": item.ownerapp_id,
                        "call_count": item.call_count,
                        "request_tokens": item.request_tokens,
                        "response_tokens": item.response_tokens,
                        "total_tokens": item.total_tokens,
                        "month_start": current_month_start,
                    },
                )
                for item in realtime_totals
            ]

        current_month_rows = _merge_total_aggregates_by_period(daily_totals, realtime_totals)

    # 合并历史月表数据和本月数据
    merged = _merge_total_aggregates_by_period(history_rows, current_month_rows)
    total = len(merged)
    paginated_rows = _apply_pagination(merged, safe_offset, safe_limit)
    payload = [
        AppMonthlyUsageTotalResponse(
            ownerapp_id=item.ownerapp_id,
            month_start=item.month_start,
            call_count=item.call_count,
            request_tokens=item.request_tokens,
            response_tokens=item.response_tokens,
            total_tokens=item.total_tokens,
        )
        for item in paginated_rows
    ]

    return PageResponse[AppMonthlyUsageTotalResponse](
        offset=safe_offset,
        total=int(total),
        data=payload,
    )
