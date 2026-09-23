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

import asyncio
from collections import defaultdict, deque
import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
import orjson
import os
import random
import re
import socket
import threading
import time
import traceback
from typing import Any, Deque, Dict, Optional, Tuple, TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse
import numpy as np
from openaiproxy.logging import logger
from openaiproxy.services.database.models.node.model import (
    ModelType,
    NodeModel,
    NodeModelQuota,
    ProtocolType,
    QuotaResetCycle,
)
from openaiproxy.services.nodeproxy.exceptions import (
    ApiKeyQuotaExceeded,
    AppQuotaExceeded,
    NorthboundQuotaProcessingError,
    NodeModelQuotaExceeded,
)
from openaiproxy.services.deps import async_session_scope
from openaiproxy.services.database.utils import (
    finalize_northbound_quotas_transactionally,
    reserve_northbound_quotas_transactionally,
    rollback_northbound_quotas_transactionally,
)
from openaiproxy.utils.apikey import ApiKeyEncryptionError, decrypt_api_key
from openaiproxy.services.database.models.apikey.utils import (
    finalize_apikey_quota_usage,
    rollback_apikey_quota_usage,
    reserve_apikey_quota,
)
from openaiproxy.services.database.models.app.utils import (
    finalize_app_quota_usage,
    rollback_app_quota_usage,
    reserve_app_quota,
)
from openaiproxy.services.database.models.node.utils import (
    finalize_node_model_quota_usage,
    rollback_node_model_quota_usage,
    rollup_previous_day_usage_transactionally,
    rollup_previous_month_usage_transactionally,
    rollup_previous_week_usage_transactionally,
    reserve_node_model_quota,
)
from openaiproxy.utils.async_helpers import run_until_complete
import requests

from openaiproxy.services.base import Service
from openaiproxy.services.database.models.node.crud import (
    advance_quota_reset_time,
    disable_node_api_key,
    enable_node_api_key,
    freeze_node_api_key,
    increment_node_api_key_tokens_used,
    roll_node_api_key_quotas,
    select_node_api_keys_by_node_ids,
    select_node_model_quotas,
    select_node_models,
    select_nodes,
    update_node_reason_by_url,
)
from openaiproxy.services.database.models.proxy.crud import (
    acquire_database_task_lock_transactionally,
    create_proxy_node_status_log_entry,
    delete_proxy_node_status_logs_before_transactionally,
    fetch_proxy_node_metrics,
    failed_notin_proccessing_node_status_logs_transactionally,
    get_or_create_proxy_node_status,
    release_database_task_lock_transactionally,
    restore_proxy_node_status_availability_by_node_url,
    select_proxy_node_status,
    update_proxy_node_status_log_entry,
    upsert_proxy_node_status,
    upsert_proxy_instance_transactionally,
)
from openaiproxy.services.database.models.proxy.utils import (
    delete_proxy_node_status_by_ids,
    select_stale_proxy_node_status,
)
from openaiproxy.services.nodeproxy.schemas import ErrorResponse
from openaiproxy.services.nodeproxy.constants import (
    API_READ_TIMEOUT, LATENCY_DEQUE_LEN,
    ErrorCodes, Strategy, err_msg
)
from openaiproxy.services.nodeproxy.schemas import NodeApiKeyEntry, Status
from openaiproxy.services.database.models import ProxyNodeStatus
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.utils.timezone import current_timezone

if TYPE_CHECKING:
    from openaiproxy.services.settings.service import SettingsService
    from openaiproxy.services.database.models.proxy.model import ProxyInstance

NODE_HEALTH_CHECK_ENDPOINT = '/v1/models'
NODE_HEALTH_CHECK_TIMEOUT = (5, 15)
QUOTA_EXHAUSTION_BACKOFF_SECONDS = 7200
ROLLUP_TASK_LOCK_SECONDS = 60 * 60
STREAM_CONNECT_TIMEOUT = 3600
STREAM_READ_TIMEOUT = 7200
REQUEST_LEASE_GRACE_SECONDS = 5
BACKEND_CAPACITY_EXHAUSTED_CODES = frozenset({
    'insufficient_quota',
    'billing_hard_limit_reached',
    'billing_not_active',
    'rate_limit_exceeded',
    'quota_exceeded',
    'resource_exhausted',
    'credit_balance_too_low',
    'balance_insufficient',
    'tokens_limit_reached',
})
BACKEND_CAPACITY_EXHAUSTED_TYPES = frozenset({
    'insufficient_quota',
    'rate_limit_error',
    'rate_limit_exceeded',
    'quota_exceeded',
    'resource_exhausted',
    'billing_hard_limit_reached',
})
BACKEND_CAPACITY_EXHAUSTED_HINTS = (
    'insufficient_quota',
    'billing_hard_limit_reached',
    'billing not active',
    'quota_exceeded',
    'quota exceeded',
    'quota exhausted',
    'exceeded your current quota',
    'current quota',
    'rate_limit_exceeded',
    'rate limit exceeded',
    'resource exhausted',
    'credit balance is too low',
    'out of credits',
    '余额不足',
    '额度不足',
    '配额不足',
    '配额已耗尽',
    '额度已用完',
    '无可用资源包',
    '请充值',
    'quota has been exhausted',
    'token-plan',
)
# 无效密钥（401/403/鉴权失败）识别关键词：命中即计入密钥级失败计数
INVALID_API_KEY_HINTS = (
    'invalid_api_key',
    'invalid api key',
    'incorrect api key',
    'api key is invalid',
    'invalid x-api-key',
    'invalid bearer token',
    'invalid token',
    'invalid auth',
    'authentication_error',
    'authentication error',
    'authentication required',
    'unauthorized',
    'permission denied',
    'forbidden',
    '鉴权失败',
    '认证失败',
    '密钥无效',
    '无效的密钥',
    '无效的api',
    '令牌无效',
)
INVALID_API_KEY_CODES = frozenset({
    'invalid_api_key',
    'invalid_api_key_error',
    'invalid_auth',
    'invalid_token',
    'authentication_error',
    'authentication_required',
    'permission_denied',
    'unauthorized',
    'forbidden',
})
# 同一密钥连续鉴权失败达到该阈值后自动禁用（实例内存态计数，成功一次即清零）
INVALID_API_KEY_FAILURE_THRESHOLD = 3

# 千问 TokenPlan 格式：reset at 09-14 09:13:00 UTC（无年份）
RESET_TIME_QWEN_PATTERN = re.compile(
    r'reset\s+at\s+(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})\s*(?:UTC|GMT)',
    re.IGNORECASE,
)
# 通用 ISO 格式：reset(s) at/on 2026-09-14T09:13:00Z 或带时区偏移
RESET_TIME_ISO_PATTERN = re.compile(
    r'reset(?:s|ting)?\s+(?:at|on)\s+'
    r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)',
    re.IGNORECASE,
)
# 解析出的重置时间合法性上限：超过 40 天视为脏数据，回退到周期计算
RESET_TIME_MAX_AHEAD_DAYS = 40


def heart_beat_controller(
    proxy_controller, stop_event: threading.Event
):
    while not stop_event.wait(proxy_controller.health_internval):
        logger.debug('开始执行心跳检查')
        try:
            proxy_controller._reclaim_expired_request_leases()
        except Exception:  # noqa: BLE001
            logger.exception('回收超时请求租约失败')
        try:
            proxy_controller.perform_node_health_checks()
        except Exception:  # noqa: BLE001
            logger.exception('执行节点健康检查失败')
        try:
            proxy_controller.remove_stale_nodes_by_expiration()
        except Exception:  # noqa: BLE001
            logger.exception('移除过期节点失败')


def create_error_response(
    status: HTTPStatus,
    message: str,
    error_type='invalid_request_error'
):
    """Create error response according to http status and message.

    Args:
        status (HTTPStatus): HTTP status codes and reason phrases
        message (str): error message
        error_type (str): error type
    """
    return JSONResponse(
        ErrorResponse(
            message=message,
            type=error_type,
            code=status.value
        ).model_dump(),
        status_code=status.value
    )


@dataclass
class _NodeMetadata:
    node_id: UUID
    config_version: str
    status_id: Optional[UUID] = None
    last_snapshot: Optional[tuple[int, float, float, bool]] = None
    removed: bool = False
    model_index: Dict[Tuple[str, str], UUID] = field(default_factory=dict)
    api_key_ids: list[UUID] = field(default_factory=list)
    # 密钥级鉴权失败计数（D3）：key 连续鉴权失败达到阈值自动禁用；
    # 成功一次即清零；密钥被禁用/删除/刷新重建时同步移除
    api_key_auth_failures: Dict[UUID, int] = field(default_factory=dict)


@dataclass
class _RequestContext:
    start_time: float
    request_id: UUID = field(default_factory=uuid4)
    first_response_time: Optional[float] = None
    model_name: Optional[str] = None
    model_type: Optional[str] = None
    request_protocol: ProtocolType = ProtocolType.openai
    ownerapp_id: Optional[str] = None
    request_action: RequestAction = RequestAction.completions
    request_tokens: Optional[int] = None
    response_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    stream: bool = False
    log_id: Optional[UUID] = None
    error: bool = False
    error_message: Optional[str] = None
    error_stack: Optional[str] = None
    backend_capacity_exhausted: bool = False
    request_data: Optional[str] = None
    response_data: Optional[str] = None
    abort: bool = False
    last_activity_time: Optional[float] = None
    lease_expires_at: Optional[float] = None
    lease_reclaimed: bool = False
    node_model_id: Optional[UUID] = None
    node_id: Optional[UUID] = None
    quota_id: Optional[UUID] = None
    quota_usage_id: Optional[UUID] = None
    client_ip: Optional[str] = None
    api_key_id: Optional[str] = None
    apikey_quota_id: Optional[UUID] = None
    apikey_quota_usage_id: Optional[UUID] = None
    app_quota_id: Optional[UUID] = None
    app_quota_usage_id: Optional[UUID] = None
    node_api_key_id: Optional[UUID] = None
    node_api_key_entry: Optional[NodeApiKeyEntry] = None
    # 本请求内已失败（限额/无效）的节点密钥ID集合：同节点重试时排除，
    # 避免加权随机再次选中刚失败的密钥
    attempted_api_key_ids: set = field(default_factory=set)


@dataclass
class _QuotaReservation:
    quota_id: UUID
    usage_id: UUID


@dataclass
class _ActiveRequestLease:
    request_id: UUID
    node_url: str
    expires_at: float
    context: _RequestContext


@dataclass
class _NodeMetrics:
    unfinished: int
    latency_samples: list[float]
    average_latency: Optional[float]
    speed: Optional[float]


class NodeProxyService(Service):

    name = "nodeproxy_service"

    """Manage all the sub nodes.

    Args:
        config_path (str): the path of the config file.
        strategy (str): the strategy to dispatch node to handle the requests.
            - random: not fully radom, but decided by the speed of nodes.
            - min_expected_latency: will compute the expected latency to
                process the requests. The sooner of the node, the more requests
                will be dispatched to it.
            - min_observed_latency: Based on previous finished requests. The
                sooner they get processed, the more requests will be dispatched
                to.
    """

    def __init__(
        self,
        settings_service: "SettingsService",
    ) -> None:
        self._lock = threading.RLock()
        self.nodes = dict()
        self.snode = dict()
        settings = settings_service.settings
        self.strategy = Strategy.from_str(settings.proxy_strategy)
        self._settings_service = settings_service
        self._stop_event = threading.Event()
        self.proxy_instance_id = settings.instance_id
        self._refresh_interval = settings.refresh_interval
        self._health_internval = settings.health_internval
        self._nodelogs_hold_days = settings.nodelogs_hold_days
        self._proxy_request_timeout = settings.proxy_request_timeout
        self._proxy_stream_connect_timeout = settings.proxy_stream_connect_timeout
        self._proxy_stream_read_timeout = settings.proxy_stream_read_timeout
        self._node_metadata: Dict[str, _NodeMetadata] = {}
        self._offline_nodes: Dict[str, Status] = {}
        self._instance_name: Optional[str] = None
        self._instance_ip: Optional[str] = None
        self._instance_process_id: Optional[str] = None
        self._proxy_instance_registered = False
        self._quota_exhausted_models: Dict[str,
                                           Dict[tuple[str, str], float]] = {}
        self._active_request_leases: Dict[UUID, _ActiveRequestLease] = {}
        # 密钥级鉴权失败计数（实例内存态）：独立于节点元数据，供跨请求熔断判定
        self._api_key_auth_failures: Dict[UUID, int] = {}
        self._quota_exhaustion_ttl = QUOTA_EXHAUSTION_BACKOFF_SECONDS
        try:
            self._ensure_proxy_instance_registration()
        except Exception:  # noqa: BLE001
            logger.exception('初始化时注册代理实例失败')

        try:
            run_until_complete(
                self._refresh_nodes_from_database(initial_load=True)
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                '初始化时从数据库加载节点配置失败')

        self.config_refresh_thread = threading.Thread(
            target=self._refresh_loop,
            name='node-manager-refresh',
            daemon=True,
        )
        self.config_refresh_thread.start()

        self.heart_beat_thread = threading.Thread(
            target=heart_beat_controller,
            args=(self, self._stop_event),
            daemon=True
        )
        self.heart_beat_thread.start()

    def pre_call(
        self,
        node_url: str,
        request_action: RequestAction,
        *,
        stream: bool = False,
        model_name: Optional[str] = None,
        model_type: Optional[str | ModelType] = None,
        request_protocol: ProtocolType = ProtocolType.openai,
        ownerapp_id: Optional[str] = None,
        request_count: Optional[int] = None,
        estimated_total_tokens: Optional[int] = None,
        request_data: Optional[str] = None,
        client_ip: Optional[str] = None,
        api_key_id: Optional[str] = None,
        exclude_api_key_ids: Optional[set[UUID]] = None,
        attempted_api_key_ids: Optional[set[UUID]] = None,
    ) -> _RequestContext:
        """Prepare runtime bookkeeping before dispatching a request.

        Args:
            exclude_api_key_ids: 选择密钥时需排除的ID集合（本请求内已失败的密钥）。
            attempted_api_key_ids: 跨尝试保持的已失败密钥ID集合（写入上下文）。
        """

        normalized_type = self._normalize_model_type(model_type)
        context = _RequestContext(
            start_time=time.time(),
            model_name=model_name,
            model_type=normalized_type,
            request_protocol=request_protocol,
            ownerapp_id=ownerapp_id,
            request_tokens=request_count,
            request_action=request_action,
            stream=stream,
            request_data=request_data,
            client_ip=client_ip,
            api_key_id=api_key_id,
        )

        # 北向配额预占（API Key + App 双层）
        self._reserve_northbound_quota(
            context=context,
            request_action=request_action,
            estimated_total_tokens=estimated_total_tokens,
        )

        node_model_id = self._resolve_node_model_id(
            node_url=node_url,
            model_name=model_name,
            model_type=normalized_type,
        )
        context.node_model_id = node_model_id

        if node_model_id is not None:
            if self._is_node_model_quota_exhausted(
                node_url,
                model_name=model_name,
                model_type=normalized_type,
            ):
                self._rollback_northbound_quota(context)
                detail = self._format_model_detail(model_name, normalized_type)
                raise NodeModelQuotaExceeded('节点模型配额已耗尽', detail=detail)

            try:
                reservation = self._reserve_node_model_quota(
                    context=context,
                    node_url=node_url,
                    node_model_id=node_model_id,
                    model_name=model_name,
                    model_type=normalized_type,
                    ownerapp_id=ownerapp_id,
                    request_action=request_action,
                    estimated_request_tokens=request_count,
                )
            except NodeModelQuotaExceeded as exc:
                self._rollback_northbound_quota(context)
                detail = getattr(exc, 'detail', None) or self._format_model_detail(
                    model_name, normalized_type)
                self._mark_node_model_quota_exhausted(
                    node_url,
                    model_name=model_name,
                    model_type=normalized_type,
                    detail=detail,
                )
                raise

            if reservation is not None:
                self._clear_node_model_quota_mark(
                    node_url,
                    model_name=model_name,
                    model_type=normalized_type,
                )
                context.quota_id = reservation.quota_id
                context.quota_usage_id = reservation.usage_id

        self._register_request_lease(node_url, context)

        # 选择节点独立API密钥（优先级加权随机），写入上下文供转发与日志使用；
        # 无可用独立密钥时保持 None，路由层回退使用 Node.api_key（向后兼容）；
        # 同节点换密钥重试时排除本请求内已失败的密钥
        merged_excluded_ids = set(exclude_api_key_ids or ())
        if attempted_api_key_ids:
            merged_excluded_ids.update(attempted_api_key_ids)
        selected_api_key_entry = self.select_node_api_key(
            node_url, exclude_api_key_ids=merged_excluded_ids or None)
        if selected_api_key_entry is not None:
            context.node_api_key_entry = selected_api_key_entry
            context.node_api_key_id = selected_api_key_entry.api_key_id

        # 跨尝试保持已失败密钥集合，供后续重试继续排除
        if attempted_api_key_ids:
            context.attempted_api_key_ids = set(attempted_api_key_ids)

        return context

    def _determine_instance_identity(self) -> tuple[str, str, str]:
        instance_name = socket.gethostname() or 'nodeproxy'
        instance_ip = self._guess_ip_address()
        return instance_name, instance_ip

    def _guess_ip_address(self) -> str:
        fallback = '127.0.0.1'
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(('8.8.8.8', 80))
                ip_addr = sock.getsockname()[0]
                if ip_addr:
                    return ip_addr
        except OSError:
            pass
        try:
            ip_addr = socket.gethostbyname(socket.gethostname())
            if ip_addr:
                return ip_addr
        except OSError:
            pass
        return fallback

    def _build_rollup_task_owner_token(self) -> str:
        """构建当前实例的报表任务锁 owner 标识。"""

        instance_name = self._instance_name or socket.gethostname() or 'nodeproxy'
        instance_ip = self._instance_ip or self._guess_ip_address()
        process_id = self._instance_process_id or str(os.getpid())
        instance_id = str(
            self.proxy_instance_id) if self.proxy_instance_id else ""
        return f'{instance_id}:{instance_name}:{instance_ip}:{process_id}'

    async def _acquire_rollup_task_lock(self, *, task_name: str, task_label: str) -> str | None:
        """尝试获取报表任务锁，失败时返回空值并记录忽略日志。"""

        owner_token = self._build_rollup_task_owner_token()
        lock_acquired = await acquire_database_task_lock_transactionally(
            task_name=task_name,
            owner_token=owner_token,
            lease_seconds=ROLLUP_TASK_LOCK_SECONDS,
        )
        if not lock_acquired:
            logger.info('{}已有任务在执行，忽略本次调度', task_label)
            return None
        return owner_token

    async def _release_rollup_task_lock(
        self,
        *,
        task_name: str,
        task_label: str,
        owner_token: str,
    ) -> None:
        """释放报表任务锁。"""

        try:
            await release_database_task_lock_transactionally(
                task_name=task_name,
                owner_token=owner_token,
            )
        except Exception:
            logger.exception('释放{}任务锁失败', task_label)

    def _ensure_proxy_instance_registration(self) -> None:
        instance_name, instance_ip = self._determine_instance_identity()
        self._instance_name = instance_name
        self._instance_ip = instance_ip

        desired_id = self.proxy_instance_id or uuid4()
        proxy_row = run_until_complete(
            self._register_proxy_instance_async(
                instance_name=instance_name,
                instance_ip=instance_ip,
                desired_id=desired_id,
            )
        )
        if proxy_row is None:
            return

        if self.proxy_instance_id != proxy_row.id:
            self.proxy_instance_id = proxy_row.id

        self._proxy_instance_registered = True
        logger.info(
            f"已登记代理实例: id={proxy_row.id} name={proxy_row.instance_name} ip={proxy_row.instance_ip}",
        )

        if self._settings_service is not None:
            try:
                self._settings_service.set('instance_id', str(proxy_row.id))
            except Exception:  # noqa: BLE001
                logger.exception('写入代理实例 ID 至配置失败')

    async def _register_proxy_instance_async(
        self,
        *,
        instance_name: str,
        instance_ip: str,
        desired_id: UUID,
    ) -> Optional['ProxyInstance']:
        proxy_row, db_process_id = await upsert_proxy_instance_transactionally(
            instance_id=desired_id,
            instance_name=instance_name,
            instance_ip=instance_ip,
        )
        self._instance_process_id = db_process_id
        self._settings_service.settings.instance_id = str(proxy_row.id)
        return proxy_row

    def _build_config_version(self, db_node, models: list[str]) -> str:
        updated_at = getattr(db_node, 'updated_at', None)
        timestamp = updated_at.isoformat() if updated_at else ''
        enabled_flag = getattr(db_node, 'enabled', True)
        auto_v1_api = getattr(db_node, 'auto_v1_api', True)
        models_part = ','.join(models)
        return f'{timestamp}:{int(bool(enabled_flag))}:{int(bool(auto_v1_api))}:{models_part}'

    def _refresh_loop(self):
        while not self._stop_event.is_set():
            try:
                run_until_complete(self._refresh_nodes_from_database())
            except Exception:  # noqa: BLE001
                logger.exception('从数据库刷新节点配置失败')
            finally:
                if not self._stop_event.wait(self._refresh_interval or 60):
                    continue
                break

    async def _refresh_nodes_from_database(self, *, initial_load: bool = False) -> None:
        with self._lock:
            previous_nodes = {
                url: copy.deepcopy(status) for url, status in self.snode.items()
            }
            previous_metadata = dict(self._node_metadata)

        # 配额滚动任务：A 到期解冻 + B 跨周期例行重置（多实例安全，条件UPDATE/CAS幂等）
        # 必须在加载密钥记录之前执行，使解冻结果在本轮刷新内生效
        roll_now = datetime.now(tz=current_timezone())
        try:
            async with async_session_scope() as roll_session:
                unfrozen_ids, rolled_ids = await roll_node_api_key_quotas(
                    session=roll_session,
                    now=roll_now,
                )
            # 说明：api_key_entries 每轮刷新都从数据库无条件重建，且 roll 任务
            # 在下方加载查询之前已提交，解冻/重置结果本轮即可见，无需失效配置指纹。
            if unfrozen_ids or rolled_ids:
                if unfrozen_ids:
                    logger.info(
                        '自动解冻 {} 个到达重置时间的节点API密钥: {}',
                        len(unfrozen_ids),
                        ', '.join(str(key_id) for key_id in unfrozen_ids),
                    )
                if rolled_ids:
                    logger.info(
                        '跨周期重置 {} 个节点API密钥的Tokens计数: {}',
                        len(rolled_ids),
                        ', '.join(str(key_id) for key_id in rolled_ids),
                    )
        except Exception:  # noqa: BLE001
            logger.exception('执行节点API密钥配额滚动任务失败')

        async with async_session_scope() as session:
            db_nodes = await select_nodes(
                enabled=True,
                expired=False,
                session=session
            )
            new_nodes: Dict[str, Status] = {}
            new_snode: Dict[str, Status] = {}
            new_metadata: Dict[str, _NodeMetadata] = {}
            config_changed: set[str] = set()

            if db_nodes:
                node_ids = [
                    node.id for node in db_nodes if node.id is not None
                ]
                model_records_map: dict[UUID,
                                        list[NodeModel]] = defaultdict(list)
                model_ids_set: set[UUID] = set()
                if node_ids:
                    db_models = await select_node_models(node_ids=node_ids, session=session)
                    for model in db_models:
                        if model.enabled is False:
                            continue
                        model_records_map[model.node_id].append(model)
                        if model.id is not None:
                            model_ids_set.add(model.id)

                quota_records_map: dict[UUID,
                                        list[NodeModelQuota]] = defaultdict(list)
                model_ids = list(model_ids_set)
                if model_ids:
                    quota_records = await select_node_model_quotas(
                        node_model_ids=model_ids,
                        session=session,
                    )
                    for quota in quota_records:
                        quota_records_map[quota.node_model_id].append(quota)

                # 批量加载各节点的独立API密钥记录（防N+1），按节点分组
                api_key_records_map: dict[UUID, list] = defaultdict(list)
                if node_ids:
                    api_key_records = await select_node_api_keys_by_node_ids(
                        node_ids=node_ids,
                        session=session,
                    )
                    for api_key_record in api_key_records:
                        api_key_records_map[api_key_record.node_id].append(
                            api_key_record)

                status_map: dict[UUID, ProxyNodeStatus] = {}
                if node_ids:
                    db_statuses = await select_proxy_node_status(
                        proxy_instance_ids=[
                            self.proxy_instance_id] if self.proxy_instance_id else None,
                        node_ids=node_ids,
                        session=session,
                    )
                    for status_row in db_statuses:
                        current = status_map.get(status_row.node_id)
                        if current is None:
                            status_map[status_row.node_id] = status_row
                        elif current.updated_at and status_row.updated_at and status_row.updated_at > current.updated_at:
                            status_map[status_row.node_id] = status_row

                evaluation_now = datetime.now(tz=current_timezone())

                for db_node in db_nodes:
                    node_url = db_node.url
                    if not node_url:
                        continue

                    status_row = status_map.get(
                        db_node.id) if db_node.id else None

                    model_index: Dict[Tuple[str, str], UUID] = {}
                    type_candidates: set[str] = set()
                    models: list[str] = []
                    model_quota_summary: dict[str, Optional[bool]] = {}
                    quota_exhausted_details: list[str] = []
                    if db_node.id is not None:
                        model_records = model_records_map.get(db_node.id, [])
                        model_names: set[str] = set()
                        for model_record in model_records:
                            model_name = model_record.model_name
                            if not model_name:
                                continue
                            model_names.add(model_name)
                            type_value = model_record.model_type.value if hasattr(
                                model_record.model_type, 'value') else str(model_record.model_type)
                            normalized_type = str(
                                type_value or ModelType.chat.value).lower()
                            type_candidates.add(normalized_type)
                            model_index[(model_name.lower(),
                                         normalized_type)] = model_record.id

                            detail_key = self._format_model_detail(
                                model_name, normalized_type)
                            quota_entries = quota_records_map.get(
                                model_record.id, []) if model_record.id is not None else []
                            quota_available, quota_tracked = self._evaluate_node_model_quota_state(
                                quota_entries,
                                current_time=evaluation_now,
                            )
                            if not quota_tracked:
                                model_quota_summary[detail_key] = None
                                self._clear_node_model_quota_mark(
                                    node_url,
                                    model_name=model_name,
                                    model_type=normalized_type,
                                )
                            else:
                                model_quota_summary[detail_key] = quota_available
                                if quota_available:
                                    self._clear_node_model_quota_mark(
                                        node_url,
                                        model_name=model_name,
                                        model_type=normalized_type,
                                    )
                                else:
                                    quota_exhausted_details.append(detail_key)
                                    self._mark_node_model_quota_exhausted(
                                        node_url,
                                        model_name=model_name,
                                        model_type=normalized_type,
                                        detail=detail_key,
                                    )
                        models = sorted(model_names)
                    else:
                        model_records = []

                    enabled_flag = db_node.enabled if db_node.enabled is not None else True
                    trusted_without_models_endpoint = bool(
                        db_node.trusted_without_models_endpoint
                    )
                    available_flag = self._resolve_node_availability(
                        enabled_flag=bool(enabled_flag),
                        persisted_available=(
                            status_row.avaiaible if status_row is not None else None
                        ),
                        trusted_without_models_endpoint=trusted_without_models_endpoint,
                    )

                    status_types = sorted(type_candidates)
                    if not status_types and db_node.name:
                        status_types = []

                    unfinished = 0
                    average_latency = None
                    speed_value = None
                    latency_samples: list[float] = []
                    status_id: Optional[UUID] = status_row.id if status_row else None

                    if db_node.id is not None:
                        unfinished, average_latency, speed_value, latency_samples = await fetch_proxy_node_metrics(
                            session=session,
                            node_id=db_node.id,
                            proxy_id=self.proxy_instance_id,
                            history_limit=LATENCY_DEQUE_LEN,
                        )

                    if not latency_samples and status_row and status_row.latency and status_row.latency > 0:
                        latency_samples = [float(status_row.latency)]

                    latency_deque = deque(
                        latency_samples, maxlen=LATENCY_DEQUE_LEN)

                    if speed_value is None and average_latency and average_latency > 0:
                        speed_value = 1.0 / average_latency
                    if speed_value is None and status_row and status_row.speed is not None:
                        speed_value = status_row.speed

                    if self.proxy_instance_id is not None and db_node.id is not None:
                        status_entry = await upsert_proxy_node_status(
                            session=session,
                            node_id=db_node.id,
                            proxy_id=self.proxy_instance_id,
                            status_id=status_row.id if status_row else None,
                            unfinished=int(unfinished),
                            latency=float(average_latency or 0.0),
                            speed=float(
                                speed_value if speed_value is not None else -1.0),
                            avaiaible=bool(available_flag),
                        )
                        if status_entry is not None:
                            status_id = status_entry.id

                    stored_api_key: Optional[str] = None
                    if db_node.api_key:
                        try:
                            stored_api_key = decrypt_api_key(db_node.api_key)
                        except ApiKeyEncryptionError:
                            logger.warning(
                                f'节点 {node_url} 数据库API密钥解密失败，将使用密文密钥')
                            stored_api_key = db_node.api_key

                    # 构建节点独立API密钥运行时条目（过滤禁用/过期/超额，解密失败单条跳过）
                    api_key_entries: list[NodeApiKeyEntry] = []
                    if db_node.id is not None:
                        api_key_entries = self._build_node_api_key_entries(
                            node_url=node_url,
                            api_key_records=api_key_records_map.get(
                                db_node.id, []),
                            evaluation_now=evaluation_now,
                        )

                    status_obj = Status(
                        models=models,
                        types=status_types,
                        unfinished=int(unfinished),
                        latency=latency_deque,
                        speed=speed_value,
                        auto_v1_api=bool(db_node.auto_v1_api),
                        avaiaible=available_flag,
                        api_key=stored_api_key,
                        api_keys=api_key_entries,
                        protocol_type=db_node.protocol_type,
                        request_proxy_url=db_node.request_proxy_url,
                        health_check=db_node.health_check,
                        trusted_without_models_endpoint=trusted_without_models_endpoint,
                        model_quota=model_quota_summary,
                        quota_exhausted_models=quota_exhausted_details,
                    )

                    new_snode[node_url] = status_obj
                    if status_obj.avaiaible and status_obj.models:
                        new_nodes[node_url] = status_obj

                    config_version = self._build_config_version(
                        db_node, models)
                    prev_meta = previous_metadata.get(node_url)
                    if prev_meta and prev_meta.config_version == config_version:
                        last_snapshot = prev_meta.last_snapshot
                    else:
                        last_snapshot = None
                        config_changed.add(node_url)

                    if status_id is None and prev_meta is not None:
                        status_id = prev_meta.status_id

                    new_metadata[node_url] = _NodeMetadata(
                        node_id=db_node.id,
                        config_version=config_version,
                        status_id=status_id,
                        last_snapshot=last_snapshot,
                        removed=False,
                        model_index=model_index,
                        api_key_ids=[
                            record.id for record in api_key_records_map.get(db_node.id, [])],
                    )

        with self._lock:
            prev_urls = set(self.snode.keys())
            current_urls = set(new_snode.keys())
            self.snode = new_snode
            self.nodes = new_nodes

            metadata: Dict[str, _NodeMetadata] = {}
            metadata.update(new_metadata)

            removed_urls = prev_urls - current_urls
            for url in removed_urls:
                prev_meta = previous_metadata.get(url)
                if prev_meta is None:
                    continue
                prev_meta.removed = True
                prev_meta.last_snapshot = None
                metadata[url] = prev_meta
                offline_status = previous_nodes.get(url)
                if offline_status is None:
                    offline_status = Status(
                        models=[],
                        types=[],
                        unfinished=0,
                        latency=deque(maxlen=LATENCY_DEQUE_LEN),
                        speed=-1,
                        auto_v1_api=True,
                        avaiaible=False,
                        api_key=None,
                        protocol_type=ProtocolType.openai,
                        request_proxy_url=None,
                        health_check=None,
                        trusted_without_models_endpoint=False,
                    )
                else:
                    offline_status = copy.deepcopy(offline_status)
                    offline_status.avaiaible = False
                    offline_status.unfinished = 0
                    if not isinstance(offline_status.latency, deque):
                        offline_status.latency = deque(
                            list(offline_status.latency),
                            maxlen=LATENCY_DEQUE_LEN
                        )
                self._offline_nodes[url] = offline_status

            added_urls = current_urls - prev_urls
            for url in added_urls:
                self._offline_nodes.pop(url, None)

            for url in config_changed:
                if url in metadata:
                    metadata[url].last_snapshot = None

            self._node_metadata = metadata

        added = current_urls - prev_urls
        removed = prev_urls - current_urls

        self._purge_quota_exhaustion_marks(
            current_urls=current_urls,
            removed_urls=removed,
            config_changed=config_changed,
        )

        if added or removed:
            logger.info(
                '节点配置已更新，新增节点: {}，移除节点: {}',
                sorted(added),
                sorted(removed),
            )

        if initial_load and not new_nodes:
            logger.warning(
                '初始化时未从数据库加载到可用节点')

    @staticmethod
    def _resolve_node_availability(
        *,
        enabled_flag: bool,
        persisted_available: Optional[bool],
        trusted_without_models_endpoint: bool,
    ) -> bool:
        """Resolve node availability without forcing trusted nodes through /v1/models."""
        if not enabled_flag:
            return False
        if trusted_without_models_endpoint:
            return True
        if persisted_available is None:
            return True
        return bool(persisted_available)

    @staticmethod
    def _build_node_api_key_entries(
        *,
        node_url: str,
        api_key_records: list,
        evaluation_now: datetime,
    ) -> list[NodeApiKeyEntry]:
        """将数据库密钥记录转换为运行时条目列表。

        过滤规则：enabled=True、未过期、未超额；解密失败的单条跳过并告警，
        不影响其他密钥。

        Args:
            node_url: 节点URL（日志用）。
            api_key_records: 该节点的 NodeApiKey 记录列表。
            evaluation_now: 当前评估时间（带时区）。

        Returns:
            可用密钥的运行时条目列表。
        """
        entries: list[NodeApiKeyEntry] = []
        for record in api_key_records:
            if not record.enabled:
                continue
            if record.expires_at is not None:
                expires_at = record.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(
                        tzinfo=evaluation_now.tzinfo)
                if expires_at <= evaluation_now:
                    continue
            # 冻结中的密钥不参与选择（防御性：正常情况下 roll 任务已先行解冻）
            if record.frozen_until is not None:
                frozen_until = record.frozen_until
                if frozen_until.tzinfo is None:
                    frozen_until = frozen_until.replace(
                        tzinfo=evaluation_now.tzinfo)
                if frozen_until > evaluation_now:
                    continue
            max_tokens = record.max_tokens
            if max_tokens is not None and int(record.tokens_used or 0) >= int(max_tokens):
                continue
            try:
                plain_api_key = decrypt_api_key(record.api_key)
            except ApiKeyEncryptionError:
                logger.warning(
                    '节点 {} 的API密钥记录 {} 解密失败，已跳过',
                    node_url,
                    record.id,
                )
                continue
            cycle_value = record.quota_reset_cycle
            if not isinstance(cycle_value, QuotaResetCycle):
                try:
                    cycle_value = QuotaResetCycle(cycle_value)
                except ValueError:
                    cycle_value = QuotaResetCycle.none
            entries.append(
                NodeApiKeyEntry(
                    api_key_id=record.id,
                    api_key=plain_api_key,
                    priority=int(record.priority or 0),
                    max_tokens=max_tokens,
                    tokens_used=int(record.tokens_used or 0),
                    quota_reset_cycle=cycle_value,
                    quota_next_reset_at=record.quota_next_reset_at,
                )
            )
        return entries

    @staticmethod
    def _select_api_key(status: Status) -> Optional[NodeApiKeyEntry]:
        """按优先级权重加权随机选择一个API密钥。

        Args:
            status: 节点运行时状态。

        Returns:
            选中的密钥条目；无可用密钥时返回 None（调用方回退 status.api_key）。
        """
        candidates = [
            entry for entry in status.api_keys
            if entry.priority > 0
            and (entry.max_tokens is None or entry.tokens_used < entry.max_tokens)
        ]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        total_weight = sum(entry.priority for entry in candidates)
        random_value = random.uniform(0, total_weight)
        cumulative = 0
        for entry in candidates:
            cumulative += entry.priority
            if random_value <= cumulative:
                return entry
        return candidates[-1]  # 浮点精度兜底

    def select_node_api_key(
        self,
        node_url: str,
        *,
        exclude_api_key_ids: Optional[set[UUID]] = None,
    ) -> Optional[NodeApiKeyEntry]:
        """为指定节点加权随机选择一个可用API密钥（线程安全）。

        Args:
            node_url: 节点URL。
            exclude_api_key_ids: 需要排除的密钥ID集合（本请求内已失败的密钥）。

        Returns:
            选中的密钥条目；节点无独立密钥或全部不可用时返回 None。
        """
        with self._lock:
            status = self.snode.get(node_url)
            if status is None:
                status = self.nodes.get(node_url)
            if status is None or not status.api_keys:
                return None
            excluded_ids = set(exclude_api_key_ids or ())
            if excluded_ids:
                filtered_status = status.model_copy(
                    update={'api_keys': [
                        entry for entry in status.api_keys
                        if entry.api_key_id not in excluded_ids
                    ]})
                if not filtered_status.api_keys:
                    return None
                return self._select_api_key(filtered_status)
            return self._select_api_key(status)

    def resolve_backend_api_key(
        self,
        node_url: str,
        *,
        selected_entry: Optional[NodeApiKeyEntry] = None,
    ) -> Optional[str]:
        """统一解析下游转发使用的 API 密钥（OpenAI / Anthropic 共用）。

        解析优先级：
        1. 调用方传入的已选条目（pre_call 记账时选中的独立密钥）——保证
           转发密钥与日志、配额记账所用密钥严格一致；
        2. 否则按 priority 加权随机选取节点当前可用的独立密钥；
        3. 最后回退 Node.api_key（向后兼容仅配置默认密钥的旧节点）。

        Args:
            node_url: 目标节点 URL。
            selected_entry: 请求上下文已选中的密钥条目；有记账的转发路径
                必须传入，避免与 pre_call 的选择结果发生偏离。

        Returns:
            下游请求使用的明文密钥；无任何可用密钥时返回 None。
        """
        if selected_entry is not None and selected_entry.api_key:
            return selected_entry.api_key
        entry = self.select_node_api_key(node_url)
        if entry is not None and entry.api_key:
            return entry.api_key
        with self._lock:
            status = self.snode.get(node_url)
            if status is None:
                status = self.nodes.get(node_url)
            return status.api_key if status is not None else None

    def restore_node_api_key_availability(self, api_key_id: UUID) -> bool:
        """重新启用节点API密钥：数据库落盘 + 本实例标记配置变更。

        使被自动禁用的密钥在下一轮 _refresh_nodes_from_database 时重新进入
        运行时条目（跨实例同步同样依赖刷新周期收敛）。

        Args:
            api_key_id: 密钥记录ID。

        Returns:
            bool: 数据库操作是否成功。
        """

        async def _enable() -> None:
            async with async_session_scope() as session:
                await enable_node_api_key(session=session, api_key_id=api_key_id)

        try:
            run_until_complete(_enable())
        except Exception:  # noqa: BLE001
            logger.exception('重新启用节点API密钥 {} 失败', api_key_id)
            return False

        # 使所属节点的配置指纹失效，触发下一轮刷新时重建该节点的运行时密钥条目
        with self._lock:
            for meta in self._node_metadata.values():
                if api_key_id in meta.api_key_ids:
                    meta.config_version = ''

        logger.info('节点API密钥 {} 已被手动重新启用', api_key_id)
        return True

    def _remove_api_key_from_memory(self, api_key_id: UUID) -> None:
        """从本实例所有节点的内存状态中移除指定密钥条目。"""
        with self._lock:
            for status in self.snode.values():
                status.api_keys = [
                    entry for entry in status.api_keys
                    if entry.api_key_id != api_key_id
                ]
            for status in self.nodes.values():
                status.api_keys = [
                    entry for entry in status.api_keys
                    if entry.api_key_id != api_key_id
                ]
            for status in self._offline_nodes.values():
                status.api_keys = [
                    entry for entry in status.api_keys
                    if entry.api_key_id != api_key_id
                ]
            # 密钥已移除，同步清零鉴权失败计数，避免残留导致误熔断
            auth_failures = getattr(self, '_api_key_auth_failures', None)
            if isinstance(auth_failures, dict):
                auth_failures.pop(api_key_id, None)
            for meta in self._node_metadata.values():
                meta.api_key_auth_failures.pop(api_key_id, None)

    def forget_node_api_key(self, api_key_id: UUID) -> None:
        """密钥被删除后从本实例内存状态中移除，避免刷新周期内继续被选中。

        跨实例同步依赖各实例的 _refresh_nodes_from_database 刷新周期收敛。

        Args:
            api_key_id: 已删除的密钥记录ID。
        """
        self._remove_api_key_from_memory(api_key_id)

    def _disable_node_api_key(self, *, api_key_id: UUID, reason: str) -> None:
        """自动禁用APIKEY：数据库落盘 + 本实例内存移除。

        跨实例同步依赖各实例的 _refresh_nodes_from_database 刷新周期收敛，
        与现有节点禁用行为一致。

        Args:
            api_key_id: 密钥记录ID。
            reason: 禁用原因。
        """

        async def _disable() -> None:
            async with async_session_scope() as session:
                await disable_node_api_key(
                    session=session,
                    api_key_id=api_key_id,
                    reason=reason,
                    disabled_at=datetime.now(tz=current_timezone()),
                )

        try:
            run_until_complete(_disable())
            self._remove_api_key_from_memory(api_key_id)
            logger.warning('节点API密钥 {} 已自动禁用: {}', api_key_id, reason)
        except Exception:  # noqa: BLE001
            logger.exception('自动禁用节点API密钥 {} 失败', api_key_id)

    @staticmethod
    def _parse_reset_time_from_error(
        payload: Any,
        error_message: Optional[str],
        *,
        now: datetime,
    ) -> Optional[datetime]:
        """从下游限额错误中解析厂商给出的精确重置时间。

        支持格式（按优先级）：
        1. 千问 TokenPlan：``reset at 09-14 09:13:00 UTC``（无年份，按当前年份推断，
           若候选时间早于 now 超过 1 天则视为明年）
        2. 通用 ISO：``reset(s) at/on 2026-09-14T09:13:00Z``（含时区偏移）

        合法性校验：解析结果必须晚于 now 且不超过 now + 40 天，
        否则视为解析失败（返回 None，由调用方回退到 quota_next_reset_at）。

        Args:
            payload: 已解析的响应体（dict 或原始内容），用于提取 error.message。
            error_message: 上下文中的错误消息（优先级低于 payload）。
            now: 当前时间（timezone-aware）。

        Returns:
            解析出的重置时间（转为本地时区的 aware datetime）；失败返回 None。
        """
        candidate_texts: list[str] = []
        if isinstance(payload, dict):
            error_part = payload.get('error')
            if isinstance(error_part, dict):
                message_value = error_part.get('message')
                if isinstance(message_value, str):
                    candidate_texts.append(message_value)
        if error_message:
            candidate_texts.append(error_message)
        if not candidate_texts:
            return None

        for text in candidate_texts:
            parsed: Optional[datetime] = None
            # 优先匹配千问无年份格式
            qwen_match = RESET_TIME_QWEN_PATTERN.search(text)
            if qwen_match:
                month, day, hour, minute, second = (
                    int(g) for g in qwen_match.groups())
                try:
                    utc_time = datetime(
                        now.year, month, day, hour, minute, second,
                        tzinfo=timezone.utc,
                    )
                    # 候选时间早于 now 超过 1 天 → 实际是明年（跨年场景）
                    if utc_time < now - timedelta(days=1):
                        utc_time = utc_time.replace(year=now.year + 1)
                    parsed = utc_time
                except ValueError:
                    parsed = None
            if parsed is None:
                iso_match = RESET_TIME_ISO_PATTERN.search(text)
                if iso_match:
                    try:
                        parsed = datetime.fromisoformat(
                            iso_match.group(1).replace('Z', '+00:00')
                        )
                    except ValueError:
                        parsed = None
            if parsed is None:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            local_parsed = parsed.astimezone(now.tzinfo)
            # 合法性校验：必须晚于 now 且不超过 40 天
            if local_parsed <= now:
                continue
            if local_parsed > now + timedelta(days=RESET_TIME_MAX_AHEAD_DAYS):
                continue
            return local_parsed
        return None

    def _freeze_node_api_key(
        self,
        *,
        entry: NodeApiKeyEntry,
        reason: str,
        payload: Any = None,
        error_message: Optional[str] = None,
    ) -> None:
        """冻结API密钥至下次重置时间：数据库落盘 + 本实例内存移除。

        决策链：
        1. 错误消息解析出厂商重置时间 → frozen_until = 解析值，
           并同步覆盖 quota_next_reset_at（厂商确认，永久纠偏）
        2. 解析失败 → frozen_until = quota_next_reset_at
           （若已过去则按周期前推到未来，防御数据滞后）

        多实例安全：freeze_node_api_key 带条件 UPDATE（enabled=True 且未冻结），
        首个冻结生效，后到实例 no-op（本地内存移除照常执行，自愈）。
        跨实例收敛依赖各实例的 _refresh_nodes_from_database 刷新周期。

        Args:
            entry: 触发限额的密钥运行时条目。
            reason: 冻结原因描述。
            payload: 已解析的下游响应体（用于解析厂商重置时间）。
            error_message: 下游错误消息。
        """
        now = datetime.now(tz=current_timezone())
        parsed_reset_time = self._parse_reset_time_from_error(
            payload, error_message, now=now)

        if parsed_reset_time is not None:
            frozen_until = parsed_reset_time
            next_reset_at: Optional[datetime] = parsed_reset_time
            freeze_reason = f'下游限额(厂商指定重置时间): {reason}'
        else:
            base_reset_time = entry.quota_next_reset_at
            if base_reset_time is not None and base_reset_time.tzinfo is None:
                base_reset_time = base_reset_time.replace(tzinfo=now.tzinfo)
            if base_reset_time is None or base_reset_time <= now:
                # 数据滞后防御：按周期从 now 前推一个周期（严格晚于 now）
                base_reset_time = advance_quota_reset_time(
                    now, entry.quota_reset_cycle, now)
            frozen_until = base_reset_time
            next_reset_at = None
            freeze_reason = f'下游限额错误: {reason}'

        async def _freeze() -> None:
            async with async_session_scope() as session:
                await freeze_node_api_key(
                    session=session,
                    api_key_id=entry.api_key_id,
                    reason=freeze_reason,
                    frozen_at=now,
                    frozen_until=frozen_until,
                    next_reset_at=next_reset_at,
                )

        try:
            run_until_complete(_freeze())
            self._remove_api_key_from_memory(entry.api_key_id)
            logger.warning(
                '节点API密钥 {} 已冻结至 {}: {}',
                entry.api_key_id,
                frozen_until.isoformat(),
                freeze_reason,
            )
        except Exception:  # noqa: BLE001
            logger.exception('冻结节点API密钥 {} 失败', entry.api_key_id)

    def _post_process_api_key_usage(
        self,
        context: _RequestContext,
    ) -> None:
        """请求后处理：累计Tokens、检测限额、触发自动禁用。

        Args:
            context: 请求上下文（需含 node_api_key_entry）。
        """
        entry = context.node_api_key_entry
        if entry is None:
            return

        total_tokens = int(context.total_tokens or 0)

        # 1. 原子累加 tokens_used（total_tokens 为 0 时跳过数据库写入）
        if total_tokens > 0:
            async def _increment() -> None:
                async with async_session_scope() as session:
                    await increment_node_api_key_tokens_used(
                        session=session,
                        api_key_id=entry.api_key_id,
                        delta=total_tokens,
                    )

            try:
                run_until_complete(_increment())
            except Exception:  # noqa: BLE001
                logger.exception(
                    '累计节点API密钥 {} 的Tokens用量失败', entry.api_key_id)

        # 2. 检测下游限额错误（复用现有容量耗尽识别 + HTTP状态语义）
        if context.backend_capacity_exhausted or context.error:
            error_message = context.error_message or ''
            payload = None
            if context.response_data:
                try:
                    payload = orjson.loads(context.response_data)
                except Exception:  # noqa: BLE001
                    payload = context.response_data
            is_rate_limited = (
                context.backend_capacity_exhausted
                or (payload is not None and self.is_backend_capacity_exhausted_error(payload))
                or self._is_rate_limit_error_message(error_message)
            )
            if is_rate_limited:
                reason_message = error_message or self.describe_backend_capacity_exhausted_error(
                    payload)
                self._freeze_or_disable_node_api_key(
                    entry=entry,
                    reason=f'下游限额错误: {reason_message}',
                    payload=payload,
                    error_message=error_message,
                )
                return

            # 2.5 检测无效密钥错误（401/403类）：累计失败计数，达到阈值自动禁用
            is_invalid_key = (
                self._is_invalid_api_key_error_payload(payload)
                or self._is_invalid_api_key_error_message(error_message)
            )
            if is_invalid_key:
                failure_count = self._record_api_key_auth_failure(entry)
                if failure_count >= INVALID_API_KEY_FAILURE_THRESHOLD:
                    self._disable_node_api_key(
                        api_key_id=entry.api_key_id,
                        reason=(
                            f'无效密钥连续失败{failure_count}次: '
                            f'{error_message or "鉴权失败"}'
                        ),
                    )
                return

        # 2.6 请求成功（无错误）：清零该密钥的鉴权失败计数
        if not context.error and not context.backend_capacity_exhausted:
            self._reset_api_key_auth_failures(entry.api_key_id)

        # 3. 检测 Tokens 用量是否达到上限
        if entry.max_tokens is not None and total_tokens > 0:
            new_used = entry.tokens_used + total_tokens
            if new_used >= entry.max_tokens:
                self._freeze_or_disable_node_api_key(
                    entry=entry,
                    reason=f'已用Tokens({new_used})达到上限({entry.max_tokens})',
                )

    def _freeze_or_disable_node_api_key(
        self,
        *,
        entry: NodeApiKeyEntry,
        reason: str,
        payload: Any = None,
        error_message: Optional[str] = None,
    ) -> None:
        """按密钥的重置周期决定冻结还是禁用。

        quota_reset_cycle 为 none（默认）时保持现状：自动禁用，需手动恢复；
        其余周期时冻结至下次重置时间，到期由刷新循环自动解冻。

        Args:
            entry: 触发限额的密钥运行时条目。
            reason: 限额原因描述。
            payload: 已解析的下游响应体（冻结时用于解析厂商重置时间）。
            error_message: 下游错误消息。
        """
        if entry.quota_reset_cycle == QuotaResetCycle.none:
            self._disable_node_api_key(
                api_key_id=entry.api_key_id, reason=reason)
            return
        self._freeze_node_api_key(
            entry=entry,
            reason=reason,
            payload=payload,
            error_message=error_message,
        )

    @staticmethod
    def _is_rate_limit_error_message(error_message: Optional[str]) -> bool:
        """判断错误信息是否包含限额/配额关键词。"""
        if not error_message:
            return False
        lower_message = error_message.lower()
        return any(hint in lower_message for hint in BACKEND_CAPACITY_EXHAUSTED_HINTS)

    @staticmethod
    def _is_invalid_api_key_error_message(error_message: Optional[str]) -> bool:
        """判断错误信息是否为无效密钥/鉴权失败（401/403类）。

        Args:
            error_message: 上下文中的错误消息或下游错误文本。

        Returns:
            bool: 命中无效密钥关键词时返回 True。
        """
        if not error_message:
            return False
        lower_message = error_message.lower()
        return any(hint in lower_message for hint in INVALID_API_KEY_HINTS)

    @staticmethod
    def _is_invalid_api_key_error_payload(payload: Any) -> bool:
        """判断下游响应体是否为无效密钥错误（基于 code/type 字段）。

        Args:
            payload: 已解析的下游响应体（dict）。

        Returns:
            bool: 命中无效密钥错误码/类型时返回 True。
        """
        if not isinstance(payload, dict):
            return False
        error_part = payload.get('error')
        if not isinstance(error_part, dict):
            error_part = payload
        for field in ('code', 'type'):
            value = error_part.get(field)
            if isinstance(value, str) and value.strip().lower() in INVALID_API_KEY_CODES:
                return True
        return False

    def _record_api_key_auth_failure(self, entry: NodeApiKeyEntry) -> int:
        """记录一次密钥鉴权失败并返回累计失败次数。

        计数为实例内存态；达到阈值时由调用方触发自动禁用。

        Args:
            entry: 触发鉴权失败的密钥运行时条目。

        Returns:
            int: 该密钥在本实例内的连续鉴权失败次数。
        """
        with self._lock:
            auth_failures = getattr(self, '_api_key_auth_failures', None)
            if not isinstance(auth_failures, dict):
                # 绕过 __init__ 构造的实例（测试stub）无该属性，惰性初始化
                auth_failures = {}
                self._api_key_auth_failures = auth_failures
            failure_count = auth_failures.get(entry.api_key_id, 0) + 1
            auth_failures[entry.api_key_id] = failure_count
            return failure_count

    def _reset_api_key_auth_failures(self, api_key_id: UUID) -> None:
        """密钥成功完成请求后清零其鉴权失败计数。

        Args:
            api_key_id: 成功请求所使用的密钥记录ID。
        """
        with self._lock:
            auth_failures = getattr(self, '_api_key_auth_failures', None)
            if isinstance(auth_failures, dict):
                auth_failures.pop(api_key_id, None)

    @staticmethod
    def _should_probe_status(status: Status) -> bool:
        if status.trusted_without_models_endpoint:
            return False
        if status.health_check is False:
            return False
        return True

    @staticmethod
    def _build_backend_request_url(node_url: str, endpoint: str, *, auto_v1_api: bool = True) -> str:
        """拼接节点请求地址，避免节点地址已带 `/v1` 时重复前缀。"""
        # 处理空 endpoint,避免node_url后错误拼接/
        if not endpoint:
            return node_url
        normalized_node_url = node_url.rstrip('/')
        normalized_endpoint = endpoint if endpoint.startswith(
            '/') else f'/{endpoint}'
        if not auto_v1_api:
            if normalized_endpoint == '/v1':
                normalized_endpoint = ''
            elif normalized_endpoint.startswith('/v1/'):
                normalized_endpoint = normalized_endpoint[3:]
        if normalized_node_url.endswith('/v1') and normalized_endpoint == '/v1':
            normalized_endpoint = ''
        elif normalized_node_url.endswith('/v1') and normalized_endpoint.startswith('/v1/'):
            normalized_endpoint = normalized_endpoint[3:]
        return f'{normalized_node_url}{normalized_endpoint}'

    @staticmethod
    def _build_models_url(node_url: str, *, auto_v1_api: bool = True) -> str:
        return NodeProxyService._build_backend_request_url(
            node_url,
            NODE_HEALTH_CHECK_ENDPOINT,
            auto_v1_api=auto_v1_api,
        )

    def _resolve_node_auto_v1_api(self, node_url: str) -> bool:
        with self._lock:
            snode = getattr(self, 'snode', None) or {}
            nodes = getattr(self, 'nodes', None) or {}
            offline_nodes = getattr(self, '_offline_nodes', None) or {}

            status = snode.get(node_url)
            if status is None:
                status = nodes.get(node_url)
            if status is None:
                status = offline_nodes.get(node_url)

        if status is None or status.auto_v1_api is None:
            return True
        return bool(status.auto_v1_api)

    @staticmethod
    def _build_backend_proxy_mapping(request_proxy_url: Optional[str]) -> Optional[dict[str, str]]:
        """Build a requests-compatible proxy mapping for node requests."""
        if not request_proxy_url:
            return None
        return {
            'http': request_proxy_url,
            'https': request_proxy_url,
        }

    @staticmethod
    def _build_backend_headers(
        *,
        api_key: Optional[str],
        protocol_type: ProtocolType,
    ) -> Optional[dict[str, str]]:
        """Build backend auth headers according to node protocol type."""
        if protocol_type == ProtocolType.anthropic:
            headers = {'anthropic-version': '2023-06-01'}
            if api_key:
                headers['x-api-key'] = api_key
            return headers
        if api_key is not None:
            return {'Authorization': f'Bearer {api_key}'}
        return None

    @staticmethod
    def _merge_backend_headers(
        *,
        base_headers: Optional[dict[str, str]],
        extra_headers: Optional[dict[str, str]],
    ) -> Optional[dict[str, str]]:
        """Merge generated backend headers with caller-provided headers."""
        if not extra_headers:
            return base_headers
        merged_headers = dict(base_headers or {})
        for header_name, header_value in extra_headers.items():
            if header_value is None:
                continue
            merged_headers[header_name] = header_value
        return merged_headers or None

    @staticmethod
    def _select_health_check_api_key(status: Status) -> Optional[str]:
        """为健康检查选择API密钥：优先使用 priority 最高的独立密钥。

        健康检查不累计 tokens_used，也不触发自动禁用。

        Args:
            status: 节点运行时状态。

        Returns:
            健康检查使用的密钥明文；无独立密钥时回退 status.api_key。
        """
        candidates = [
            entry for entry in status.api_keys
            if entry.priority > 0
        ]
        if candidates:
            best_entry = max(candidates, key=lambda entry: entry.priority)
            return best_entry.api_key
        return status.api_key

    def perform_node_health_checks(self) -> None:
        node_candidates: list[tuple[str, Optional[str],
                                    ProtocolType, bool, Optional[str]]] = []
        with self._lock:
            for node_url, status in self.snode.items():
                if not self._should_probe_status(status):
                    continue
                node_candidates.append((
                    node_url,
                    self._select_health_check_api_key(status),
                    status.protocol_type,
                    bool(status.auto_v1_api) if status.auto_v1_api is not None else True,
                    status.request_proxy_url,
                ))

        for node_url, api_key, protocol_type, auto_v1_api, request_proxy_url in node_candidates:
            self._check_single_node(
                node_url=node_url,
                api_key=api_key,
                protocol_type=protocol_type,
                auto_v1_api=auto_v1_api,
                request_proxy_url=request_proxy_url,
            )

    def _check_single_node(
        self,
        node_url: str,
        api_key: Optional[str],
        protocol_type: ProtocolType,
        auto_v1_api: bool,
        request_proxy_url: Optional[str],
    ) -> None:
        """对单个节点执行健康检查（D4：失败时降级用次优密钥重试）。

        首次检查失败且节点存在其他健康独立密钥时，用次优密钥再试一次，
        避免单一密钥鉴权失败导致整个节点被误判下线。
        """
        if not node_url:
            return

        available, latency, error_message = self._probe_node_health(
            node_url=node_url,
            api_key=api_key,
            protocol_type=protocol_type,
            auto_v1_api=auto_v1_api,
            request_proxy_url=request_proxy_url,
        )

        # D4：首选密钥失败时，降级用次优密钥重试一次
        if not available:
            fallback_api_key = self._select_health_check_fallback_api_key(
                node_url, failed_api_key=api_key)
            if fallback_api_key is not None:
                logger.info(
                    '节点 {} 健康检查首选密钥失败，降级用次优密钥重试',
                    node_url,
                )
                available, retry_latency, retry_error = self._probe_node_health(
                    node_url=node_url,
                    api_key=fallback_api_key,
                    protocol_type=protocol_type,
                    auto_v1_api=auto_v1_api,
                    request_proxy_url=request_proxy_url,
                )
                if available:
                    latency = retry_latency
                    error_message = None
                else:
                    error_message = (
                        f'首选密钥: {error_message or "未知错误"}; '
                        f'次优密钥: {retry_error or "未知错误"}'
                    )

        started_at = time.time() - latency
        self._apply_health_check_result(
            node_url=node_url,
            available=available,
            latency=latency,
            started_at=started_at,
            error_message=error_message,
        )

    def _probe_node_health(
        self,
        *,
        node_url: str,
        api_key: Optional[str],
        protocol_type: ProtocolType,
        auto_v1_api: bool,
        request_proxy_url: Optional[str],
    ) -> tuple[bool, float, Optional[str]]:
        """用指定密钥探测节点一次，返回（是否可用, 耗时, 错误信息）。"""
        headers = self._build_backend_headers(
            api_key=api_key,
            protocol_type=protocol_type,
        )
        proxies = self._build_backend_proxy_mapping(request_proxy_url)
        started_at = time.time()
        available = False
        error_message: Optional[str] = None

        try:
            response = requests.get(
                self._build_models_url(node_url, auto_v1_api=auto_v1_api),
                headers=headers,
                proxies=proxies,
                timeout=NODE_HEALTH_CHECK_TIMEOUT,
            )
            if response.status_code == HTTPStatus.OK:
                available = True
            else:
                error_message = f'HTTP {response.status_code}'
        except requests.RequestException as exc:
            error_message = str(exc)
        except Exception as exc:  # noqa: BLE001 - defensive guard
            error_message = str(exc)

        latency = max(time.time() - started_at, 0.0)
        return available, latency, error_message

    def _select_health_check_fallback_api_key(
        self,
        node_url: str,
        failed_api_key: Optional[str],
    ) -> Optional[str]:
        """健康检查失败后选择降级重试密钥（排除刚失败的密钥）。

        Args:
            node_url: 节点地址。
            failed_api_key: 刚检查失败的首选密钥明文。

        Returns:
            次优密钥明文；无其他健康独立密钥时返回 None。
        """
        with self._lock:
            status = self.snode.get(node_url)
            if status is None:
                return None
            candidates = [
                entry for entry in status.api_keys
                if entry.priority > 0
                and entry.api_key != failed_api_key
            ]
        if not candidates:
            return None
        fallback_entry = max(candidates, key=lambda entry: entry.priority)
        return fallback_entry.api_key

    def _apply_health_check_result(
        self,
        *,
        node_url: str,
        available: bool,
        latency: float,
        started_at: float,
        error_message: Optional[str],
    ) -> None:
        meta_snapshot: Optional[_NodeMetadata] = None
        previous_available: Optional[bool] = None
        snapshot: Optional[tuple[int, float, float, bool]] = None

        with self._lock:
            status = self.snode.get(node_url)
            if status is None:
                return

            previous_available = bool(status.avaiaible)
            status.avaiaible = available

            if available and status.models:
                self.nodes[node_url] = status
                self._offline_nodes.pop(node_url, None)
            else:
                self.nodes.pop(node_url, None)
                if not available:
                    offline_snapshot = copy.deepcopy(status)
                    offline_snapshot.avaiaible = False
                    if not isinstance(offline_snapshot.latency, deque):
                        offline_snapshot.latency = deque(
                            list(offline_snapshot.latency or []),
                            maxlen=LATENCY_DEQUE_LEN,
                        )
                    self._offline_nodes[node_url] = offline_snapshot

            meta = self._node_metadata.get(node_url)
            if meta is not None:
                meta_snapshot = copy.deepcopy(meta)

            last_latency = 0.0
            if status.latency and len(status.latency):
                try:
                    last_latency = float(status.latency[-1])
                except (TypeError, ValueError):  # pragma: no cover - defensive
                    last_latency = 0.0
            speed_value = float(
                status.speed) if status.speed is not None else -1.0
            snapshot = (int(status.unfinished), last_latency,
                        speed_value, bool(available))

        new_status_id: Optional[UUID] = None
        if meta_snapshot and meta_snapshot.node_id is not None:
            try:
                new_status_id = run_until_complete(
                    self._persist_health_check_result_async(
                        node_id=meta_snapshot.node_id,
                        status_id=meta_snapshot.status_id,
                        available=available,
                        latency=latency,
                        started_at=started_at,
                        previous_available=previous_available,
                        error_message=error_message,
                    )
                )
            except Exception:  # noqa: BLE001
                logger.exception('记录节点 {} 的心跳检查结果失败', node_url)

        if meta_snapshot:
            with self._lock:
                meta = self._node_metadata.get(node_url)
                if meta is not None:
                    if new_status_id is not None:
                        meta.status_id = new_status_id
                    meta.last_snapshot = snapshot

        if previous_available is not None and previous_available != available:
            self._persist_node_reason(
                node_url,
                None if available else error_message,
            )
            if available:
                logger.info('节点 {} 心跳检查通过', node_url)
            else:
                logger.warning('节点 {} 心跳检查失败: {}', node_url,
                               error_message or '未知错误')

    async def _persist_health_check_result_async(
        self,
        *,
        node_id: UUID,
        status_id: Optional[UUID],
        available: bool,
        latency: float,
        started_at: float,
        previous_available: Optional[bool],
        error_message: Optional[str],
    ) -> Optional[UUID]:
        async with async_session_scope() as session:
            try:
                status_row = await upsert_proxy_node_status(
                    session=session,
                    node_id=node_id,
                    proxy_id=self.proxy_instance_id,
                    status_id=status_id,
                    unfinished=0,
                    latency=0.0,
                    speed=-1.0,
                    avaiaible=available,
                )
                if status_row is None:
                    return None

                should_log = previous_available != available or not available
                if should_log:
                    latency_value = float(max(latency, 0.0))
                    try:
                        start_at = datetime.fromtimestamp(
                            started_at, tz=current_timezone())
                    except (OSError, OverflowError, ValueError):  # pragma: no cover - defensive
                        start_at = datetime.now(
                            tz=current_timezone()) - timedelta(seconds=latency_value)
                    end_at = start_at + timedelta(seconds=latency_value)
                    await create_proxy_node_status_log_entry(
                        session=session,
                        node_id=node_id,
                        proxy_id=self.proxy_instance_id,
                        status_id=status_row.id,
                        ownerapp_id=None,
                        request_protocol=ProtocolType.openai,
                        model_name=None,
                        action=RequestAction.healthcheck,
                        start_at=start_at,
                        end_at=end_at,
                        latency=latency_value,
                        request_tokens=0,
                        response_tokens=0,
                        total_tokens=0,
                        error=not available,
                        error_message=error_message if not available else None,
                        error_stack=None,
                    )

                return status_row.id
            except Exception:
                raise

    @property
    def model_list(self):
        """Supported model list."""
        model_names: list[str] = []
        with self._lock:
            for node_status in self.snode.values():
                models = node_status.models or []
                model_names.extend(models)
        return model_names

    @staticmethod
    def _match_request_protocol(
        node_protocol: ProtocolType,
        request_protocol: ProtocolType,
        allow_cross_protocol: bool,
    ) -> tuple[bool, bool]:
        """Return whether a node can serve the request and whether it is preferred."""
        if request_protocol == ProtocolType.anthropic:
            if node_protocol in {ProtocolType.anthropic, ProtocolType.both}:
                return True, True
            if allow_cross_protocol and node_protocol == ProtocolType.openai:
                return True, False
            return False, False

        if node_protocol in {ProtocolType.openai, ProtocolType.both}:
            return True, True
        if allow_cross_protocol and node_protocol == ProtocolType.anthropic:
            return True, False
        return False, False

    def list_models_for_protocol(
        self,
        request_protocol: ProtocolType = ProtocolType.openai,
        *,
        allow_cross_protocol: bool = True,
    ) -> list[str]:
        """List models visible to a northbound protocol."""
        model_names: list[str] = []
        with self._lock:
            for node_status in self.snode.values():
                matched, _ = self._match_request_protocol(
                    node_status.protocol_type,
                    request_protocol,
                    allow_cross_protocol,
                )
                if matched:
                    model_names.extend(node_status.models or [])
        return list(dict.fromkeys(model_names))

    @staticmethod
    def filter_models_by_allowed_models(
        model_names: list[str],
        effective_allowed_models: Optional[list[str]] = None,
    ) -> list[str]:
        """按最终生效白名单过滤模型列表。"""
        deduplicated_model_names = list(dict.fromkeys(model_names))
        if effective_allowed_models is None:
            return deduplicated_model_names
        allowed_model_set = set(effective_allowed_models)
        return [model_name for model_name in deduplicated_model_names if model_name in allowed_model_set]

    @staticmethod
    def is_model_allowed(
        model_name: str,
        effective_allowed_models: Optional[list[str]] = None,
    ) -> bool:
        """判断单个模型是否在最终生效白名单内。"""
        if effective_allowed_models is None:
            return True
        return model_name in set(effective_allowed_models)

    def supports_model(
        self,
        model_name: str,
        model_type: Optional[str] = None,
        *,
        request_protocol: ProtocolType = ProtocolType.openai,
        allow_cross_protocol: bool = False,
    ) -> bool:
        """Return whether any node supports the requested model and optional type."""
        normalized_type = self._normalize_model_type(model_type)
        with self._lock:
            for node_status in self.snode.values():
                matched, _ = self._match_request_protocol(
                    node_status.protocol_type,
                    request_protocol,
                    allow_cross_protocol,
                )
                if not matched:
                    continue
                if self._status_supports_model(node_status, model_name, normalized_type):
                    return True
        return False

    @property
    def health_internval(self) -> int:
        """Return the preferred health interval in seconds."""
        return self._health_internval

    @property
    def nodelogs_hold_days(self) -> int:
        """Return the number of days to hold node logs."""
        return self._nodelogs_hold_days

    @property
    def status(self):
        """Return the status."""
        noderet = dict()
        with self._lock:
            for node_url, node_status in self.snode.items():
                noderet[node_url] = copy.deepcopy(node_status)

        return noderet

    def get_node_url(
        self,
        model_name: str,
        model_type: Optional[str] = None,
        *,
        request_protocol: ProtocolType = ProtocolType.openai,
        allow_cross_protocol: bool = False,
        exclude_node_urls: Optional[set[str]] = None,
    ):
        """Select a node that can serve the requested model and type.

        Args:
            model_name (str): Model identifier requested by the client.
            model_type (Optional[str]): Optional model type hint (e.g. ``chat``).
            exclude_node_urls (Optional[set[str]]): Nodes that have already been attempted.

        Returns:
            Optional[str]: The selected node URL, or ``None`` if unavailable.
        """

        normalized_type = self._normalize_model_type(model_type)
        detail = self._format_model_detail(model_name, normalized_type)
        excluded_urls = set(exclude_node_urls or ())

        def _select_candidate(
            matched_with_speed: list[tuple[str, float]],
            matched_without_speed: list[str],
            latency_map: dict[str, float],
        ) -> Optional[str]:
            all_matched_urls = [url for url,
                                _ in matched_with_speed] + matched_without_speed
            if not all_matched_urls:
                return None

            speeds = [speed for _, speed in matched_with_speed]
            average_speed = sum(speeds) / len(speeds) if speeds else 1.0
            all_the_speeds = speeds + \
                [average_speed] * len(matched_without_speed)

            if self.strategy == Strategy.RANDOM:
                speed_sum = sum(all_the_speeds)
                if speed_sum <= 0:
                    weights = [1 / len(all_the_speeds)] * len(all_the_speeds)
                else:
                    weights = [speed / speed_sum for speed in all_the_speeds]
                index = random.choices(
                    range(len(all_matched_urls)), weights=weights)[0]
                return all_matched_urls[index]

            if self.strategy == Strategy.MIN_EXPECTED_LATENCY:
                min_latency = float('inf')
                min_index = 0
                indexes = list(range(len(all_the_speeds)))
                random.shuffle(indexes)
                for index in indexes:
                    node_url = all_matched_urls[index]
                    status = self.nodes.get(node_url)
                    unfinished = int(status.unfinished) if status else 0
                    speed = all_the_speeds[index] or 1
                    latency = unfinished / speed
                    if latency < min_latency:
                        min_latency = latency
                        min_index = index
                return all_matched_urls[min_index]

            if self.strategy == Strategy.MIN_OBSERVED_LATENCY:
                latency_values = [latency_map.get(
                    url, float('inf')) for url in all_matched_urls]
                if not latency_values:
                    return None
                index = int(np.argmin(np.array(latency_values)))
                return all_matched_urls[index]

            raise ValueError(f'错误的: {self.strategy}')

        with self._lock:
            preferred_with_speed: list[tuple[str, float]] = []
            preferred_without_speed: list[str] = []
            preferred_latency_map: dict[str, float] = {}
            fallback_with_speed: list[tuple[str, float]] = []
            fallback_without_speed: list[str] = []
            fallback_latency_map: dict[str, float] = {}
            quota_filtered = False

            for node_url, node_status in self.nodes.items():
                if node_url in excluded_urls:
                    continue
                matched_protocol, is_preferred = self._match_request_protocol(
                    node_status.protocol_type,
                    request_protocol,
                    allow_cross_protocol,
                )
                if not matched_protocol:
                    continue
                if not self._status_supports_model(node_status, model_name, normalized_type):
                    continue
                if self._is_node_model_quota_exhausted(
                    node_url,
                    model_name=model_name,
                    model_type=normalized_type,
                ):
                    quota_filtered = True
                    continue
                target_with_speed = preferred_with_speed if is_preferred else fallback_with_speed
                target_without_speed = preferred_without_speed if is_preferred else fallback_without_speed
                target_latency_map = preferred_latency_map if is_preferred else fallback_latency_map
                if node_status.speed is not None:
                    target_with_speed.append(
                        (node_url, float(node_status.speed)))
                else:
                    target_without_speed.append(node_url)
                if len(node_status.latency):
                    target_latency_map[node_url] = float(
                        np.mean(np.array(node_status.latency)))
                else:
                    target_latency_map[node_url] = float('inf')

            selected_node_url = _select_candidate(
                preferred_with_speed,
                preferred_without_speed,
                preferred_latency_map,
            )
            if selected_node_url is not None:
                return selected_node_url

            selected_node_url = _select_candidate(
                fallback_with_speed,
                fallback_without_speed,
                fallback_latency_map,
            )
            if selected_node_url is not None:
                return selected_node_url

            if not (
                preferred_with_speed or preferred_without_speed or fallback_with_speed or fallback_without_speed
            ):
                if quota_filtered:
                    raise NodeModelQuotaExceeded('节点模型配额已耗尽', detail=detail)
                return None
            if quota_filtered:
                raise NodeModelQuotaExceeded('节点模型配额已耗尽', detail=detail)
            return None

    @classmethod
    def is_backend_capacity_exhausted_error(cls, payload: Any) -> bool:
        """Return whether the backend payload indicates quota or rate-limit exhaustion."""

        markers: list[str] = []

        def _collect_markers(value: Any) -> None:
            if value is None:
                return
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized:
                    markers.append(normalized)
                return
            if isinstance(value, (int, float, bool)):
                markers.append(str(value).strip().lower())
                return
            if isinstance(value, list):
                for item in value:
                    _collect_markers(item)
                return
            if not isinstance(value, dict):
                normalized = str(value).strip().lower()
                if normalized:
                    markers.append(normalized)
                return

            for key in (
                'code',
                'type',
                'message',
                'detail',
                'text',
                'error',
                'error_description',
                'errorDescription',
            ):
                if key in value:
                    _collect_markers(value.get(key))

            data_obj = value.get('data')
            if isinstance(data_obj, dict):
                for key in ('code', 'type', 'message', 'detail', 'text'):
                    if key in data_obj:
                        _collect_markers(data_obj.get(key))

        _collect_markers(payload)
        for marker in markers:
            if marker in BACKEND_CAPACITY_EXHAUSTED_CODES:
                return True
            if marker in BACKEND_CAPACITY_EXHAUSTED_TYPES:
                return True
            if any(hint in marker for hint in BACKEND_CAPACITY_EXHAUSTED_HINTS):
                return True
        return False

    @classmethod
    def describe_backend_capacity_exhausted_error(cls, payload: Any) -> str:
        """Extract a concise reason string from a backend capacity exhaustion payload."""

        candidates: list[str] = []

        def _collect(value: Any) -> None:
            if value is None:
                return
            if isinstance(value, str):
                normalized = value.strip()
                if normalized:
                    candidates.append(normalized)
                return
            if isinstance(value, (int, float, bool)):
                candidates.append(str(value).strip())
                return
            if isinstance(value, list):
                for item in value:
                    _collect(item)
                return
            if not isinstance(value, dict):
                normalized = str(value).strip()
                if normalized:
                    candidates.append(normalized)
                return

            for key in (
                'message',
                'detail',
                'text',
                'error_description',
                'errorDescription',
                'code',
                'type',
            ):
                if key in value:
                    _collect(value.get(key))

            error_obj = value.get('error')
            if isinstance(error_obj, dict):
                _collect(error_obj)

            data_obj = value.get('data')
            if isinstance(data_obj, dict):
                _collect(data_obj)

        _collect(payload)
        for candidate in candidates:
            if candidate:
                return candidate
        return '后端容量已耗尽'

    @staticmethod
    def _serialize_backend_payload(payload: Any) -> Optional[str]:
        """Serialize arbitrary backend payloads into request-log friendly text."""

        if payload is None:
            return None
        if isinstance(payload, str):
            return payload
        if isinstance(payload, (bytes, bytearray)):
            try:
                return bytes(payload).decode('utf-8', errors='ignore')
            except Exception:  # noqa: BLE001
                return None
        try:
            return orjson.dumps(payload).decode('utf-8', errors='ignore')
        except Exception:  # noqa: BLE001
            return str(payload)

    def mark_backend_node_unavailable(
        self,
        node_url: str,
        *,
        reason: Optional[str] = None,
    ) -> bool:
        """Mark a backend node unavailable until a later health check recovers it."""

        if not node_url:
            return False

        with self._lock:
            status = self.snode.get(node_url)
            if status is None:
                return False

            was_available = bool(status.avaiaible) or node_url in self.nodes
            status.avaiaible = False
            self.nodes.pop(node_url, None)

            offline_snapshot = copy.deepcopy(status)
            offline_snapshot.avaiaible = False
            if not isinstance(offline_snapshot.latency, deque):
                offline_snapshot.latency = deque(
                    list(offline_snapshot.latency or []),
                    maxlen=LATENCY_DEQUE_LEN,
                )
            self._offline_nodes[node_url] = offline_snapshot

        if was_available:
            logger.warning(
                '节点 {} 因后端容量耗尽被临时标记为不可用: {}',
                node_url,
                reason or '未知原因',
            )

        self._persist_node_reason(node_url, reason)
        return was_available

    def restore_backend_node_availability(
        self,
        node_url: str,
    ) -> bool:
        """Restore a temporarily disabled backend node to available state.

        This is typically called when the node's API key has been updated
        (e.g. after recharging), making the previous capacity-exhaustion
        reason no longer applicable.

        In multi-instance deployments, this method also restores the
        ``avaiaible`` flag in ``ProxyNodeStatus`` for ALL proxy instances,
        ensuring other workers pick up the restored state on their next
        ``_refresh_nodes_from_database`` cycle.

        Args:
            node_url: The URL of the node to restore.

        Returns:
            bool: True if the node was previously unavailable and is now restored.
        """

        if not node_url:
            return False

        with self._lock:
            status = self.snode.get(node_url)
            if status is None:
                return False

            # 仅在当前处于不可用状态时执行恢复
            if status.avaiaible and node_url in self.nodes:
                return False

            status.avaiaible = True
            if status.models:
                self.nodes[node_url] = status
            self._offline_nodes.pop(node_url, None)

        logger.info(
            '节点 {} 因配置变更（如API Key更新）被重新启用',
            node_url,
        )

        # 清除持久化的不可用原因
        self._persist_node_reason(node_url, None)

        # 跨实例同步：将该节点在所有代理实例中的 avaiaible 恢复为 True，
        # 确保其他 worker 在下一轮 _refresh_nodes_from_database 时能读取到可用状态
        self._restore_all_instance_node_status(node_url)
        return True

    def _restore_all_instance_node_status(self, node_url: str) -> None:
        """将该节点在所有代理实例中的 ProxyNodeStatus.avaiaible 恢复为 True。

        多实例部署时，每个 worker 持有独立的 proxy_instance_id，
        仅修改当前实例内存状态不足以让其他实例恢复节点。
        此方法通过数据库批量更新，确保所有实例在下一轮配置刷新时
        读取到 avaiaible=True，从而自动恢复节点。

        Args:
            node_url: 需要恢复的节点 URL。
        """
        if not node_url:
            return

        async def _restore_status() -> None:
            async with async_session_scope() as session:
                restored_count = await restore_proxy_node_status_availability_by_node_url(
                    session=session,
                    node_url=node_url,
                )
                if restored_count > 0:
                    logger.info(
                        '节点 {} 已跨实例恢复 {} 条代理节点状态记录',
                        node_url,
                        restored_count,
                    )

        try:
            run_until_complete(_restore_status())
        except Exception:  # noqa: BLE001
            logger.exception('跨实例恢复节点 {} 状态失败', node_url)

    def _persist_node_reason(
        self,
        node_url: str,
        reason: Optional[str],
    ) -> None:
        """Persist the current transient node unavailability reason."""

        if not node_url:
            return

        async def _update_reason() -> None:
            async with async_session_scope() as session:
                await update_node_reason_by_url(
                    session=session,
                    node_url=node_url,
                    reason=reason,
                    updated_at=datetime.now(current_timezone()),
                )

        try:
            run_until_complete(_update_reason())
        except Exception:  # noqa: BLE001
            logger.exception('更新节点 {} 的不可用原因失败', node_url)

    def _finalize_backend_capacity_exhausted_attempt(
        self,
        node_url: str,
        context: _RequestContext,
    ) -> None:
        """Finalize logging and node quota bookkeeping for a failed backend attempt."""

        elapsed = max(time.time() - context.start_time, 0.0)
        if context.response_tokens is None:
            context.response_tokens = 0
        if context.total_tokens is None or context.total_tokens < 0:
            context.total_tokens = self._resolve_total_tokens(context)
        self._finalize_request_log(node_url, context, elapsed)
        if not context.lease_reclaimed:
            self._apply_node_model_quota(node_url, context)
        self._refresh_node_metrics(node_url)

    def cleanup_backend_capacity_exhausted_attempt(
        self,
        node_url: str,
        context: _RequestContext,
        payload: Any,
        *,
        reason: Optional[str] = None,
    ) -> None:
        """Mark a quota-exhausted backend unavailable and clean up the failed attempt.

        密钥级故障隔离：若节点配置了独立密钥且触发限额的密钥被处置后
        仍有健康密钥可用，则保持节点可用，仅由后续密钥后处理冻结/禁用
        触发密钥；无独立密钥或健康密钥耗尽时维持原有踢节点行为。
        """

        self._release_request_lease(context)

        message = reason or self.describe_backend_capacity_exhausted_error(
            payload)
        context.backend_capacity_exhausted = True
        context.error = True
        if not context.error_message:
            context.error_message = message
        serialized_payload = self._serialize_backend_payload(payload)
        if serialized_payload and not context.response_data:
            context.response_data = serialized_payload

        # 记录本请求内已失败的密钥，供同节点换密钥重试时排除
        failed_entry = context.node_api_key_entry
        if failed_entry is not None:
            context.attempted_api_key_ids.add(failed_entry.api_key_id)

        if self._node_has_healthy_api_keys(node_url, exclude_entry=failed_entry):
            logger.warning(
                '节点 {} 密钥限额但仍有健康密钥，保持节点可用: {}',
                node_url, message,
            )
        else:
            self.mark_backend_node_unavailable(node_url, reason=message)

        rollback_error: Optional[NorthboundQuotaProcessingError] = None
        try:
            self._rollback_northbound_quota(context)
        except NorthboundQuotaProcessingError as exc:
            rollback_error = exc
            self._mark_quota_processing_error(context, exc)

        self._finalize_backend_capacity_exhausted_attempt(node_url, context)

        if rollback_error is not None:
            raise rollback_error

    def _node_has_healthy_api_keys(
        self,
        node_url: str,
        *,
        exclude_entry: Optional[NodeApiKeyEntry] = None,
    ) -> bool:
        """判断节点在排除指定密钥后是否仍有健康密钥可用。

        Args:
            node_url: 目标节点 URL。
            exclude_entry: 需要排除的密钥条目（通常是刚触发限额的密钥）。

        Returns:
            bool: 节点未配置独立密钥时返回 False（key级处置无意义，
            调用方应维持踢节点现状）；否则返回排除后是否仍有可用密钥。
        """
        with self._lock:
            status = self.snode.get(node_url)
            if status is None:
                status = self.nodes.get(node_url)
            if status is None:
                status = self._offline_nodes.get(node_url)
            if status is None or not status.api_keys:
                return False
            excluded_id = exclude_entry.api_key_id if exclude_entry is not None else None
            candidates = [
                entry for entry in status.api_keys
                if entry.priority > 0
                and entry.api_key_id != excluded_id
                and (entry.max_tokens is None or entry.tokens_used < entry.max_tokens)
            ]
            return bool(candidates)

    @staticmethod
    def _average_latency(latency_values: Deque[float]) -> float:
        if not latency_values:
            return 0.0
        return float(sum(latency_values) / len(latency_values))

    @staticmethod
    def _normalize_model_type(model_type: Optional[Any]) -> Optional[str]:
        if model_type is None:
            return ModelType.chat.value
        if hasattr(model_type, 'value'):
            model_type = getattr(model_type, 'value')
        return str(model_type).lower()

    @staticmethod
    def _status_supports_model(status: Status, model_name: str, model_type: Optional[str]) -> bool:
        models = status.models or []
        if model_name not in models:
            return False
        if model_type is None:
            return True
        status_types = status.types or []
        return any(isinstance(item, str) and item.lower() == model_type for item in status_types)

    def _resolve_node_model_id(
        self,
        *,
        node_url: str,
        model_name: Optional[str],
        model_type: Optional[str],
    ) -> Optional[UUID]:
        if not node_url or not model_name:
            return None
        normalized_name = model_name.lower()
        normalized_type = (model_type or ModelType.chat.value).lower()
        with self._lock:
            meta = self._node_metadata.get(node_url)
            if meta is None or not meta.model_index:
                return None
            return meta.model_index.get((normalized_name, normalized_type))

    def _reserve_northbound_quota(
        self,
        *,
        context: _RequestContext,
        request_action: RequestAction,
        estimated_total_tokens: Optional[int],
    ) -> None:
        """预占北向配额（API Key 配额 + 应用配额），双层必须同时通过。"""
        api_key_id = context.api_key_id
        ownerapp_id = context.ownerapp_id
        if not api_key_id and not ownerapp_id:
            return

        api_key_uuid: Optional[UUID] = None
        if api_key_id:
            try:
                api_key_uuid = UUID(api_key_id)
            except (ValueError, AttributeError):
                api_key_uuid = None

        try:
            ak_result, app_result = run_until_complete(
                reserve_northbound_quotas_transactionally(
                    api_key_id=api_key_uuid,
                    ownerapp_id=ownerapp_id,
                    proxy_id=self.proxy_instance_id,
                    model_name=context.model_name,
                    request_action=request_action,
                    estimated_total_tokens=estimated_total_tokens,
                )
            )
            if ak_result is not None:
                context.apikey_quota_id = ak_result[0]
                context.apikey_quota_usage_id = ak_result[1]
            if app_result is not None:
                context.app_quota_id = app_result[0]
                context.app_quota_usage_id = app_result[1]
        except (ApiKeyQuotaExceeded, AppQuotaExceeded):
            raise
        except Exception:  # noqa: BLE001
            logger.exception('北向配额预占失败 (api_key={}, app={})',
                             api_key_id, ownerapp_id)
            raise NorthboundQuotaProcessingError('北向配额预占失败，请稍后重试')

    @staticmethod
    def _has_active_quota_reservations(context: _RequestContext) -> bool:
        """判断请求上下文中是否存在需要租约保护的预占状态。"""
        return any(
            value is not None
            for value in (
                context.apikey_quota_id,
                context.app_quota_id,
                context.quota_id,
            )
        )

    def _compute_request_lease_expiry(
        self,
        context: _RequestContext,
        *,
        observed_at: Optional[float] = None,
    ) -> float:
        """根据请求形态与最近活动时间计算租约过期时间。"""
        if context.stream:
            if context.last_activity_time is None:
                base_time = context.start_time
                timeout_seconds = max(
                    int(getattr(self, '_proxy_stream_connect_timeout',
                        STREAM_CONNECT_TIMEOUT)),
                    1,
                )
            else:
                base_time = observed_at if observed_at is not None else context.last_activity_time
                timeout_seconds = max(
                    int(getattr(self, '_proxy_stream_read_timeout', STREAM_READ_TIMEOUT)),
                    1,
                )
        else:
            base_time = observed_at if observed_at is not None else context.start_time
            timeout_seconds = max(
                int(getattr(self, '_proxy_request_timeout', API_READ_TIMEOUT)),
                1,
            )

        return float(base_time + timeout_seconds + REQUEST_LEASE_GRACE_SECONDS)

    def _register_request_lease(self, node_url: str, context: _RequestContext) -> None:
        """为完成预占的请求注册进程内租约。"""
        if not node_url or not self._has_active_quota_reservations(context):
            return

        expires_at = self._compute_request_lease_expiry(context)
        context.lease_expires_at = expires_at
        lease = _ActiveRequestLease(
            request_id=context.request_id,
            node_url=node_url,
            expires_at=expires_at,
            context=context,
        )
        with self._lock:
            self._active_request_leases[context.request_id] = lease

    def touch_request_lease(
        self,
        context: _RequestContext,
        *,
        observed_at: Optional[float] = None,
    ) -> None:
        """在流式响应仍有活动时延长租约。"""
        observed_ts = observed_at if observed_at is not None else time.time()
        context.last_activity_time = observed_ts
        expires_at = self._compute_request_lease_expiry(
            context, observed_at=observed_ts)
        context.lease_expires_at = expires_at

        with self._lock:
            lease = self._active_request_leases.get(context.request_id)
            if lease is None:
                return
            lease.expires_at = expires_at

    def _release_request_lease(self, context: _RequestContext) -> None:
        """在请求结束或失败清理接管后释放租约。"""
        with self._lock:
            self._active_request_leases.pop(context.request_id, None)
        context.lease_expires_at = None

    def _collect_expired_request_leases(self) -> list[_ActiveRequestLease]:
        """提取所有已过期的活动请求租约。"""
        now_ts = time.time()
        expired_request_ids: list[UUID] = []

        # 测试可能用 object.__new__ 绕过 __init__ 构造实例，属性缺失时跳过回收
        active_leases = getattr(self, '_active_request_leases', None)
        if not isinstance(active_leases, dict):
            return []

        with self._lock:
            for request_id, lease in active_leases.items():
                if lease.expires_at <= now_ts:
                    expired_request_ids.append(request_id)
            expired_leases = [
                active_leases.pop(request_id)
                for request_id in expired_request_ids
                if request_id in active_leases
            ]

        return expired_leases

    def _rollback_node_model_quota(self, context: _RequestContext) -> None:
        """在请求超时回收时回滚节点模型预占。"""
        quota_id = context.quota_id
        usage_id = context.quota_usage_id
        if quota_id is None:
            return

        async def _rollback() -> None:
            async with async_session_scope() as session:
                await rollback_node_model_quota_usage(
                    session=session,
                    quota_id=quota_id,
                    usage_id=usage_id,
                )

        try:
            run_until_complete(_rollback())
        except Exception:  # noqa: BLE001
            logger.exception(
                '回滚节点模型预占失败 (quota_id={}, usage_id={})', quota_id, usage_id)
            raise

        context.quota_id = None
        context.quota_usage_id = None

    def _reclaim_expired_request_leases(self) -> None:
        """回收租约已过期请求的预占状态，避免异常长请求长期挂占。"""
        expired_leases = self._collect_expired_request_leases()
        if not expired_leases:
            return

        for lease in expired_leases:
            context = lease.context
            context.abort = True
            context.error = True
            context.lease_reclaimed = True
            context.lease_expires_at = None
            if not context.error_message:
                context.error_message = '请求租约超时，已回收预占配额'

            logger.warning(
                '请求租约超时，开始回收预占 (request_id={}, node_url={})',
                context.request_id,
                lease.node_url,
            )

            try:
                self._rollback_node_model_quota(context)
            except Exception:  # noqa: BLE001
                logger.exception('回收节点模型预占失败 (request_id={})',
                                 context.request_id)

            try:
                self._rollback_northbound_quota(context)
            except NorthboundQuotaProcessingError:
                logger.exception('回收北向预占失败 (request_id={})',
                                 context.request_id)

    def _rollback_northbound_quota(self, context: _RequestContext) -> None:
        """南向配额失败时回滚北向配额预占。"""
        ak_quota_id = context.apikey_quota_id
        ak_usage_id = context.apikey_quota_usage_id
        app_quota_id = context.app_quota_id
        app_usage_id = context.app_quota_usage_id
        if not ak_quota_id and not app_quota_id:
            return

        try:
            run_until_complete(
                rollback_northbound_quotas_transactionally(
                    apikey_quota_id=ak_quota_id,
                    apikey_usage_id=ak_usage_id,
                    app_quota_id=app_quota_id,
                    app_usage_id=app_usage_id,
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                '回滚北向配额失败 (apikey_quota={}, app_quota={})', ak_quota_id, app_quota_id)
            raise NorthboundQuotaProcessingError('北向配额回滚失败，请稍后重试')

        context.apikey_quota_id = None
        context.apikey_quota_usage_id = None
        context.app_quota_id = None
        context.app_quota_usage_id = None

    def _apply_northbound_quota(self, context: _RequestContext) -> None:
        """请求完成后更新北向配额的 token 使用数据。"""
        total_tokens = max(int(context.total_tokens or 0), 0)

        api_key_uuid: Optional[UUID] = None
        if context.api_key_id:
            try:
                api_key_uuid = UUID(context.api_key_id)
            except (ValueError, AttributeError):
                pass

        try:
            run_until_complete(
                finalize_northbound_quotas_transactionally(
                    api_key_id=api_key_uuid,
                    ownerapp_id=context.ownerapp_id,
                    apikey_quota_id=context.apikey_quota_id,
                    apikey_usage_id=context.apikey_quota_usage_id,
                    app_quota_id=context.app_quota_id,
                    app_usage_id=context.app_quota_usage_id,
                    total_tokens=total_tokens,
                    model_name=context.model_name,
                    request_action=context.request_action,
                    log_id=context.log_id,
                )
            )
        except (ApiKeyQuotaExceeded, AppQuotaExceeded):
            raise
        except Exception:  # noqa: BLE001
            logger.exception(
                '更新北向配额token使用失败 (api_key={}, app={})',
                context.api_key_id,
                context.ownerapp_id,
            )
            raise NorthboundQuotaProcessingError('北向配额结算失败，请尽快核查')

    @staticmethod
    def _mark_quota_processing_error(
        context: _RequestContext,
        exc: BaseException,
    ) -> None:
        """将配额处理异常回写到请求上下文，便于更新日志。"""
        detail = getattr(exc, 'detail', None)
        message = detail or str(exc) or exc.__class__.__name__
        context.error = True
        if not context.error_message:
            context.error_message = message
        if not context.error_stack:
            context.error_stack = traceback.format_exc()

    def _reserve_node_model_quota(
        self,
        *,
        context: _RequestContext,
        node_url: str,
        node_model_id: UUID,
        model_name: Optional[str],
        model_type: Optional[str],
        ownerapp_id: Optional[str],
        request_action: RequestAction,
        estimated_request_tokens: Optional[int],
    ) -> Optional[_QuotaReservation]:
        node_id: Optional[UUID] = None
        with self._lock:
            meta = self._node_metadata.get(node_url)
            if meta is not None:
                node_id = meta.node_id

        if node_id is None:
            logger.debug('节点 {} 未找到对应的元数据，跳过配额预占', node_url)
            return None

        context.node_id = node_id

        async def _reserve() -> Optional[tuple[UUID, UUID]]:
            async with async_session_scope() as session:
                return await reserve_node_model_quota(
                    session=session,
                    node_id=node_id,
                    node_model_id=node_model_id,
                    proxy_id=self.proxy_instance_id,
                    model_name=model_name,
                    model_type=model_type,
                    ownerapp_id=ownerapp_id,
                    request_action=request_action,
                    estimated_request_tokens=estimated_request_tokens,
                )

        try:
            reservation = run_until_complete(_reserve())
            if reservation is None:
                return None
            quota_id, usage_id = reservation
            return _QuotaReservation(quota_id=quota_id, usage_id=usage_id)
        except NodeModelQuotaExceeded as exc:
            detail = getattr(exc, 'detail', None) or model_name or str(
                node_model_id)
            logger.warning('节点 {} 的模型 {} 配额不足', node_url, detail)
            raise
        except Exception:  # noqa: BLE001
            logger.exception('节点 {} 预占模型 {} 配额失败', node_url,
                             model_name or node_model_id)
            return None

    def _build_quota_marker_key(
        self,
        *,
        model_name: Optional[str],
        model_type: Optional[str],
    ) -> Optional[tuple[str, str]]:
        if not model_name:
            return None
        normalized_name = model_name.strip().lower()
        if not normalized_name:
            return None
        normalized_type = (model_type or ModelType.chat.value).strip().lower()
        return normalized_name, normalized_type

    @staticmethod
    def _format_model_detail(
        model_name: Optional[str],
        model_type: Optional[str],
    ) -> str:
        if model_name and model_type:
            return f'{model_name} ({model_type})'
        if model_name:
            return model_name
        return model_type or ''

    @staticmethod
    def _quota_entry_has_capacity(
        quota: 'NodeModelQuota',
        *,
        current_time: datetime,
    ) -> bool:
        if quota.expired_at is not None and quota.expired_at <= current_time:
            return False
        if quota.call_limit is not None and quota.call_used >= quota.call_limit:
            return False
        if quota.prompt_tokens_limit is not None and quota.prompt_tokens_used >= quota.prompt_tokens_limit:
            return False
        if quota.completion_tokens_limit is not None and quota.completion_tokens_used >= quota.completion_tokens_limit:
            return False
        if quota.total_tokens_limit is not None and quota.total_tokens_used >= quota.total_tokens_limit:
            return False
        return True

    @classmethod
    def _evaluate_node_model_quota_state(
        cls,
        quotas: list['NodeModelQuota'],
        *,
        current_time: datetime,
    ) -> tuple[bool, bool]:
        if not quotas:
            return True, False

        has_active_quota = False
        for quota in quotas:
            if quota.expired_at is not None and quota.expired_at <= current_time:
                continue
            has_active_quota = True
            if cls._quota_entry_has_capacity(quota, current_time=current_time):
                return True, True

        if has_active_quota:
            return False, True

        # All quota records are expired or inactive but still tracked
        return False, True

    def _mark_node_model_quota_exhausted(
        self,
        node_url: str,
        *,
        model_name: Optional[str],
        model_type: Optional[str],
        detail: Optional[str] = None,
    ) -> None:
        key = self._build_quota_marker_key(
            model_name=model_name,
            model_type=model_type,
        )
        if not node_url or key is None:
            return
        now_ts = time.time()
        expires_at = now_ts + float(self._quota_exhaustion_ttl)
        with self._lock:
            marks = self._quota_exhausted_models.setdefault(node_url, {})
            previous = marks.get(key)
            marks[key] = expires_at
        if previous is not None and previous > now_ts:
            return
        hint = detail or self._format_model_detail(
            model_name, model_type) or 'unknown'
        logger.info('节点 {} 的模型配额已标记为耗尽: {}', node_url, hint)

    def _clear_node_model_quota_mark(
        self,
        node_url: str,
        *,
        model_name: Optional[str],
        model_type: Optional[str],
    ) -> None:
        key = self._build_quota_marker_key(
            model_name=model_name,
            model_type=model_type,
        )
        if not node_url or key is None:
            return
        with self._lock:
            marks = self._quota_exhausted_models.get(node_url)
            if not marks:
                return
            marks.pop(key, None)
            if not marks:
                self._quota_exhausted_models.pop(node_url, None)

    def _is_node_model_quota_exhausted(
        self,
        node_url: str,
        *,
        model_name: Optional[str],
        model_type: Optional[str],
    ) -> bool:
        key = self._build_quota_marker_key(
            model_name=model_name,
            model_type=model_type,
        )
        if not node_url or key is None:
            return False
        now_ts = time.time()
        with self._lock:
            marks = self._quota_exhausted_models.get(node_url)
            if not marks:
                return False
            expires_at = marks.get(key)
            if expires_at is None:
                return False
            if expires_at <= now_ts:
                marks.pop(key, None)
                if not marks:
                    self._quota_exhausted_models.pop(node_url, None)
                return False
            return True

    def _purge_quota_exhaustion_marks(
        self,
        *,
        current_urls: set[str],
        removed_urls: set[str],
        config_changed: set[str],
    ) -> None:
        with self._lock:
            for url in list(self._quota_exhausted_models.keys()):
                if url in removed_urls or url not in current_urls or url in config_changed:
                    self._quota_exhausted_models.pop(url, None)

    def _apply_node_model_quota(self, node_url: str, context: _RequestContext) -> None:
        if (
            context.node_model_id is None
            or context.node_id is None
            or context.quota_id is None
            or context.quota_usage_id is None
        ):
            return
        request_tokens = max(int(context.request_tokens or 0), 0)
        response_tokens = max(int(context.response_tokens or 0), 0)
        # 始终通过 request + response 计算，保证恒等式
        total_tokens = request_tokens + response_tokens

        context.request_tokens = request_tokens
        context.response_tokens = response_tokens
        context.total_tokens = total_tokens

        async def _finalize() -> None:
            async with async_session_scope() as session:
                await finalize_node_model_quota_usage(
                    session=session,
                    node_id=context.node_id,
                    node_model_id=context.node_model_id,
                    proxy_id=self.proxy_instance_id,
                    primary_quota_id=context.quota_id,
                    primary_quota_usage_id=context.quota_usage_id,
                    model_name=context.model_name,
                    request_tokens=request_tokens,
                    response_tokens=response_tokens,
                    total_tokens=total_tokens,
                    ownerapp_id=context.ownerapp_id,
                    request_action=context.request_action,
                    log_id=context.log_id,
                )

        try:
            run_until_complete(_finalize())
        except NodeModelQuotaExceeded as exc:
            detail = getattr(exc, 'detail', None) or context.model_name or str(
                context.node_model_id)
            logger.warning('节点 {} 的模型 {} 配额不足，无法完整记录token消耗', node_url, detail)
            self._mark_node_model_quota_exhausted(
                node_url,
                model_name=context.model_name,
                model_type=context.model_type,
                detail=detail,
            )
        except Exception:  # noqa: BLE001
            logger.exception('节点 {} 更新模型配额失败', node_url)

    @staticmethod
    def _resolve_total_tokens(context: _RequestContext) -> int:
        """始终通过 request_tokens + response_tokens 计算 total_tokens。

        保证 total_tokens = request_tokens + response_tokens 恒等式成立，
        不再优先使用上游返回的 total_tokens 值。

        Args:
            context: 请求上下文，包含 request_tokens 和 response_tokens。

        Returns:
            计算后的 total_tokens 值，非负整数。
        """
        request_value = context.request_tokens if isinstance(
            context.request_tokens, int) else 0
        response_value = context.response_tokens if isinstance(
            context.response_tokens, int) else 0
        total = request_value + response_value
        return total if total >= 0 else 0

    def _record_request_start(self, node_url: str, context: _RequestContext) -> None:
        meta = self._node_metadata.get(node_url)
        if meta is None or meta.node_id is None or meta.removed:
            return

        try:
            log_id = run_until_complete(
                self._record_request_start_async(meta, context)
            )
            context.log_id = log_id
        except Exception:  # noqa: BLE001
            logger.exception('记录节点 {} 的请求起始信息失败', node_url)

    async def _record_request_start_async(
        self,
        meta: _NodeMetadata,
        context: _RequestContext,
    ) -> Optional[UUID]:
        async with async_session_scope() as session:
            status_row = await get_or_create_proxy_node_status(
                session=session,
                node_id=meta.node_id,
                proxy_id=self.proxy_instance_id,
                status_id=meta.status_id,
            )
            if status_row is None:
                return None
            meta.status_id = status_row.id

            try:
                start_at = datetime.fromtimestamp(
                    context.start_time, tz=current_timezone())
            except (OSError, OverflowError, ValueError):  # pragma: no cover - defensive
                start_at = datetime.now(tz=current_timezone())

            log_entry = await create_proxy_node_status_log_entry(
                session=session,
                node_id=meta.node_id,
                proxy_id=self.proxy_instance_id,
                status_id=status_row.id,
                ownerapp_id=context.ownerapp_id,
                request_protocol=context.request_protocol,
                model_name=context.model_name,
                action=context.request_action,
                start_at=start_at,
                end_at=None,
                latency=0.0,
                request_tokens=int(context.request_tokens or 0),
                response_tokens=0,
                total_tokens=self._resolve_total_tokens(context),
                cached_tokens=int(context.cached_tokens or 0),
                stream=context.stream,
                error=context.error,
                error_message=context.error_message,
                error_stack=context.error_stack,
                request_data=context.request_data,
                response_data=context.response_data,
                client_ip=context.client_ip,
                abort=context.abort,
                node_api_key_id=context.node_api_key_id,
            )
            return log_entry.id

    def _finalize_request_log(self, node_url: str, context: _RequestContext, elapsed: float) -> None:
        try:
            run_until_complete(
                self._finalize_request_log_async(node_url, context, elapsed)
            )
        except Exception:  # noqa: BLE001
            logger.exception('更新节点 {} 的请求日志失败', node_url)

    async def _finalize_request_log_async(
        self,
        node_url: str,
        context: _RequestContext,
        elapsed: float,
    ) -> None:
        meta = self._node_metadata.get(node_url)
        if meta is None or meta.node_id is None or meta.removed:
            return

        try:
            start_at = datetime.fromtimestamp(
                context.start_time, tz=current_timezone())
        except (OSError, OverflowError, ValueError):  # pragma: no cover - defensive
            start_at = datetime.now(tz=current_timezone()) - \
                timedelta(seconds=elapsed)
        end_at = start_at + timedelta(seconds=elapsed)

        try:
            first_response_at = (
                datetime.fromtimestamp(
                    context.first_response_time, tz=current_timezone())
                if context.first_response_time is not None else None
            )
        except (OSError, OverflowError, ValueError):  # pragma: no cover - defensive
            first_response_at = None
        if first_response_at is None:
            first_response_at = end_at

        async with async_session_scope() as session:
            status_row = await get_or_create_proxy_node_status(
                session=session,
                node_id=meta.node_id,
                proxy_id=self.proxy_instance_id,
                status_id=meta.status_id,
            )
            if status_row is None:
                return
            meta.status_id = status_row.id

            if context.log_id is None:
                await create_proxy_node_status_log_entry(
                    session=session,
                    node_id=meta.node_id,
                    proxy_id=self.proxy_instance_id,
                    status_id=status_row.id,
                    ownerapp_id=context.ownerapp_id,
                    request_protocol=context.request_protocol,
                    model_name=context.model_name,
                    action=context.request_action,
                    start_at=start_at,
                    end_at=end_at,
                    first_response_at=first_response_at,
                    latency=float(elapsed),
                    request_tokens=int(context.request_tokens or 0),
                    response_tokens=int(context.response_tokens or 0),
                    total_tokens=self._resolve_total_tokens(context),
                    cached_tokens=int(context.cached_tokens or 0),
                    stream=context.stream,
                    error=context.error,
                    error_message=context.error_message,
                    error_stack=context.error_stack,
                    request_data=context.request_data,
                    response_data=context.response_data,
                    client_ip=context.client_ip,
                    abort=context.abort,
                    node_api_key_id=context.node_api_key_id,
                )
            else:
                await update_proxy_node_status_log_entry(
                    session=session,
                    log_id=context.log_id,
                    end_at=end_at,
                    first_response_at=first_response_at,
                    latency=float(elapsed),
                    request_tokens=int(context.request_tokens or 0),
                    response_tokens=int(context.response_tokens or 0),
                    total_tokens=self._resolve_total_tokens(context),
                    cached_tokens=int(context.cached_tokens or 0),
                    error=context.error,
                    error_message=context.error_message,
                    error_stack=context.error_stack,
                    request_data=context.request_data,
                    response_data=context.response_data,
                    abort=context.abort,
                    node_api_key_id=context.node_api_key_id,
                )

    def _refresh_node_metrics(self, node_url: str) -> None:
        meta = self._node_metadata.get(node_url)
        status = self.snode.get(node_url)
        if meta is None or status is None or meta.node_id is None or meta.removed:
            return

        try:
            metrics = run_until_complete(
                self._refresh_node_metrics_async(meta, status)
            )
        except Exception:  # noqa: BLE001
            logger.exception('刷新节点 {} 指标数据失败', node_url)
            return

        latency_samples = metrics.latency_samples
        with self._lock:
            status.unfinished = metrics.unfinished
            status.latency = deque(latency_samples, maxlen=LATENCY_DEQUE_LEN)
            status.speed = metrics.speed if metrics.speed is not None else None
            if status.avaiaible and status.models:
                self.nodes[node_url] = status
            else:
                self.nodes.pop(node_url, None)

    async def _refresh_node_metrics_async(
        self,
        meta: _NodeMetadata,
        status: Status,
    ) -> _NodeMetrics:
        async with async_session_scope() as session:
            unfinished, average_latency, speed, latency_samples = await fetch_proxy_node_metrics(
                session=session,
                node_id=meta.node_id,
                proxy_id=self.proxy_instance_id,
                history_limit=LATENCY_DEQUE_LEN,
            )

            status_row = await get_or_create_proxy_node_status(
                session=session,
                node_id=meta.node_id,
                proxy_id=self.proxy_instance_id,
                status_id=meta.status_id,
            )
            if status_row is not None:
                meta.status_id = status_row.id

            if status_row is not None and not latency_samples and status_row.latency and status_row.latency > 0:
                latency_samples = [float(status_row.latency)]

            computed_speed = speed
            if computed_speed is None and average_latency and average_latency > 0:
                computed_speed = 1.0 / average_latency

            status_row = await upsert_proxy_node_status(
                session=session,
                node_id=meta.node_id,
                proxy_id=self.proxy_instance_id,
                status_id=meta.status_id,
                unfinished=int(unfinished),
                latency=float(average_latency or 0.0),
                speed=float(
                    computed_speed if computed_speed is not None else -1.0),
                avaiaible=bool(status.avaiaible),
            )
            if status_row is not None:
                meta.status_id = status_row.id

        return _NodeMetrics(
            unfinished=unfinished,
            latency_samples=latency_samples,
            average_latency=average_latency,
            speed=computed_speed,
        )

    def refresh_all_node_metrics(self) -> None:
        for node_url in list(self.snode.keys()):
            self._refresh_node_metrics(node_url)

    def remove_stale_nodes_by_expiration(self) -> None:
        expiration_cutoff = datetime.now(tz=current_timezone(
        )) - timedelta(seconds=self.health_internval)

        removed = run_until_complete(
            self._remove_stale_nodes_by_expiration_async(expiration_cutoff)
        )
        if removed:
            logger.info(
                '已删除 {} 条超过 {} 秒的过期节点状态记录',
                removed, self.health_internval
            )

    async def _remove_stale_nodes_by_expiration_async(self, expiration_cutoff: datetime) -> int:
        async with async_session_scope() as session:
            try:
                stale_rows = await select_stale_proxy_node_status(
                    session=session,
                    expiration_cutoff=expiration_cutoff,
                    exclude_proxy_id=self.proxy_instance_id,
                )
                if not stale_rows:
                    return 0

                now_ts = datetime.now(tz=current_timezone())
                for row in stale_rows:
                    proxy_id = row.proxy_id or self.proxy_instance_id
                    start_at = row.updated_at or now_ts
                    latency_value = max(
                        0.0, (now_ts - start_at).total_seconds())
                    try:
                        await create_proxy_node_status_log_entry(
                            session=session,
                            node_id=row.node_id,
                            proxy_id=proxy_id,
                            status_id=row.id,
                            ownerapp_id=None,
                            request_protocol=ProtocolType.openai,
                            model_name=None,
                            action=RequestAction.healthcheck,
                            start_at=start_at,
                            end_at=now_ts,
                            latency=latency_value,
                            request_tokens=0,
                            response_tokens=0,
                            total_tokens=0,
                        )
                    except Exception:  # noqa: BLE001
                        stack = traceback.format_exc()
                        logger.exception('记录节点 {} 的健康检查结果失败', row.node_id)
                        try:
                            await create_proxy_node_status_log_entry(
                                session=session,
                                node_id=row.node_id,
                                proxy_id=proxy_id,
                                status_id=row.id,
                                ownerapp_id=None,
                                request_protocol=ProtocolType.openai,
                                model_name=None,
                                action=RequestAction.healthcheck,
                                start_at=start_at,
                                end_at=now_ts,
                                latency=latency_value,
                                request_tokens=0,
                                response_tokens=0,
                                total_tokens=0,
                                error=True,
                                error_message='Heartbeat log persistence failed',
                                error_stack=stack,
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                '二次记录节点 {} 的健康检查错误信息失败', row.node_id)

                return await delete_proxy_node_status_by_ids(
                    session=session,
                    status_ids=[row.id for row in stale_rows],
                )
            except Exception:
                raise

    async def check_request_model(
        self,
        model_name: str,
        model_type: Optional[str] = None,
        *,
        request_protocol: ProtocolType = ProtocolType.openai,
        allow_cross_protocol: bool = False,
        effective_allowed_models: Optional[list[str]] = None,
    ) -> Optional[JSONResponse]:
        """Check if a request is valid."""
        if not self.is_model_allowed(model_name, effective_allowed_models):
            return create_error_response(
                HTTPStatus.FORBIDDEN,
                f'Access to model `{model_name}` is denied by access policy.',
                error_type='permission_error',
            )
        if self.supports_model(
            model_name,
            model_type,
            request_protocol=request_protocol,
            allow_cross_protocol=allow_cross_protocol,
        ):
            return None
        normalized_type = self._normalize_model_type(model_type)
        if normalized_type:
            message = f'The model `{model_name}` with type `{normalized_type}` does not exist.'
        else:
            message = f'The model `{model_name}` does not exist.'
        ret = create_error_response(HTTPStatus.NOT_FOUND, message)
        return ret

    def handle_unavailable_model(self, model_name: str, model_type: Optional[str] = None):
        """Handle unavailable model.

        Args:
            model_name (str): the model in the request.
        """
        normalized_type = self._normalize_model_type(model_type)
        detail = f'{model_name}' if not normalized_type else f'{model_name} ({normalized_type})'
        logger.warning('请求的模型不可用: {}', detail)
        ret = {
            'error_code': ErrorCodes.MODEL_NOT_FOUND,
            'text': err_msg[ErrorCodes.MODEL_NOT_FOUND],
        }
        return ret

    def handle_api_timeout(self, node_url):
        """Handle the api time out."""
        logger.warning(f'接口调用超时: {node_url}')
        return self._build_api_timeout_payload()

    def _build_api_timeout_payload(self) -> bytes:
        """Build the backend timeout payload.

        Returns:
            bytes: Serialized timeout payload terminated with a newline.
        """
        ret = {
            'error_code': ErrorCodes.API_TIMEOUT.value,
            'text': err_msg[ErrorCodes.API_TIMEOUT],
        }
        return orjson.dumps(ret) + b'\n'

    def _build_service_unavailable_payload(self) -> bytes:
        """Build the backend service unavailable payload.

        Returns:
            bytes: Serialized service unavailable payload terminated with a newline.
        """
        ret = {
            'error_code': ErrorCodes.SERVICE_UNAVAILABLE.value,
            'text': err_msg[ErrorCodes.SERVICE_UNAVAILABLE],
        }
        return orjson.dumps(ret) + b'\n'

    def _handle_api_request_failure(self, node_url: str, exc: BaseException) -> bytes:
        """Build a fallback payload for backend request failures.

        Args:
            node_url (str): The backend node URL.
            exc (BaseException): The original request failure.

        Returns:
            bytes: Serialized fallback payload.
        """
        logger.warning('接口调用失败: {} {}', node_url, exc)
        return self._build_service_unavailable_payload()

    def stream_generate(
        self,
        request: Optional[Dict[str, Any]],
        node_url: str,
        endpoint: str,
        api_key: Optional[str] = None,
        *,
        request_context: Optional[_RequestContext] = None,
        protocol_type: ProtocolType = ProtocolType.openai,
        request_proxy_url: Optional[str] = None,
        request_content: Optional[bytes] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ):
        """Return a generator to handle the input request.

        Args:
            request (Optional[Dict[str, Any]]): the JSON request body.
            node_url (str): the node url.
            endpoint (str): the endpoint. Such as `/v1/chat/completions`.
        """
        try:
            headers = self._merge_backend_headers(
                base_headers=self._build_backend_headers(
                    api_key=api_key,
                    protocol_type=protocol_type,
                ),
                extra_headers=extra_headers,
            )
            request_kwargs: dict[str, Any] = {}
            if request_content is not None:
                request_kwargs['data'] = request_content
            else:
                request_kwargs['json'] = request
            proxies = self._build_backend_proxy_mapping(request_proxy_url)
            target_url = self._build_backend_request_url(
                node_url,
                endpoint,
                auto_v1_api=self._resolve_node_auto_v1_api(node_url),
            )
            with requests.post(
                target_url,
                headers=headers,
                proxies=proxies,
                stream=True,
                timeout=(
                    getattr(self, '_proxy_stream_connect_timeout',
                            STREAM_CONNECT_TIMEOUT),
                    getattr(self, '_proxy_stream_read_timeout',
                            STREAM_READ_TIMEOUT),
                ),
                **request_kwargs,
            ) as response:
                for chunk in response.iter_lines(
                    decode_unicode=False,
                    delimiter=b'\n\n'
                ):
                    if chunk:
                        if request_context is not None:
                            self.touch_request_lease(request_context)
                        yield chunk + b'\n\n'
        except GeneratorExit:
            logger.info('流式请求已终止: {}', node_url)
            raise
        except requests.Timeout:
            yield self.handle_api_timeout(node_url)
        except requests.RequestException as exc:
            yield self._handle_api_request_failure(node_url, exc)
        except Exception:  # noqa: BLE001
            logger.exception('流式接口处理异常: {}', node_url)
            yield self._build_api_timeout_payload()

    async def generate(
        self,
        request: Optional[Dict[str, Any]],
        node_url: str,
        endpoint: str,
        api_key: Optional[str] = None,
        *,
        protocol_type: ProtocolType = ProtocolType.openai,
        request_proxy_url: Optional[str] = None,
        request_content: Optional[bytes] = None,
        extra_headers: Optional[dict[str, str]] = None,
        method: str = 'POST',
        response_mode: str = 'text',
    ):
        """Return a the response of the input request.

        Args:
            request (Optional[Dict[str, Any]]): the JSON request body.
            node_url (str): the node url.
            endpoint (str): the endpoint. Such as `/v1/chat/completions`.
        """
        try:
            import httpx
            async with httpx.AsyncClient(proxy=request_proxy_url) as client:
                headers = self._merge_backend_headers(
                    base_headers=self._build_backend_headers(
                        api_key=api_key,
                        protocol_type=protocol_type,
                    ),
                    extra_headers=extra_headers,
                )
                target_url = self._build_backend_request_url(
                    node_url,
                    endpoint,
                    auto_v1_api=self._resolve_node_auto_v1_api(node_url),
                )
                request_kwargs: dict[str, Any] = {
                    'headers': headers,
                    'timeout': getattr(self, '_proxy_request_timeout', API_READ_TIMEOUT),
                }
                normalized_method = method.upper()
                if request_content is not None:
                    request_kwargs['content'] = request_content
                elif normalized_method not in {'GET', 'DELETE'}:
                    request_kwargs['json'] = request
                response = await client.request(
                    normalized_method,
                    target_url,
                    **request_kwargs,
                )
                if response_mode == 'bytes':
                    return response.content
                return response.text
        except asyncio.CancelledError:
            logger.info('非流式请求已取消: {}', node_url)
            raise
        except httpx.TimeoutException:
            return self.handle_api_timeout(node_url)
        except httpx.HTTPError as exc:
            return self._handle_api_request_failure(node_url, exc)
        except Exception:  # noqa: BLE001
            logger.exception('非流式接口处理异常: {}', node_url)
            return self._build_api_timeout_payload()

    def post_call(self, node_url: str, context: _RequestContext):
        """Finalize bookkeeping after a request completes."""
        self._release_request_lease(context)
        elapsed = time.time() - context.start_time
        if context.response_tokens is None:
            context.response_tokens = 0
        if context.total_tokens is None or context.total_tokens < 0:
            context.total_tokens = self._resolve_total_tokens(context)
        self._finalize_request_log(node_url, context, elapsed)
        if not context.lease_reclaimed:
            self._apply_node_model_quota(node_url, context)
        try:
            if not context.lease_reclaimed:
                self._apply_northbound_quota(context)
        except (ApiKeyQuotaExceeded, AppQuotaExceeded, NorthboundQuotaProcessingError) as exc:
            logger.exception('北向配额结算失败')
            self._mark_quota_processing_error(context, exc)
            self._finalize_request_log(node_url, context, elapsed)
        # 节点独立API密钥后处理：累计Tokens用量并检测限额触发自动禁用
        try:
            self._post_process_api_key_usage(context)
        except Exception:  # noqa: BLE001
            logger.exception('节点API密钥用量后处理失败')
        self._refresh_node_metrics(node_url)

    def create_background_tasks(self, url: str, start: _RequestContext):
        """Create a background task to finalize bookkeeping for streaming responses."""
        background_tasks = BackgroundTasks()
        background_tasks.add_task(self.post_call, url, start)
        return background_tasks

    async def teardown(self) -> None:
        self._stop_event.set()
        if getattr(self, 'config_refresh_thread', None) and self.config_refresh_thread.is_alive():
            self.config_refresh_thread.join(timeout=1)
        if self.heart_beat_thread.is_alive():
            self.heart_beat_thread.join(timeout=1)
        try:
            self.refresh_all_node_metrics()
        except Exception:  # noqa: BLE001
            logger.exception(
                '服务停止时刷新节点指标失败')
        await super().teardown()

    def cleanup_runtime_state_task(self) -> None:
        """Flush cached runtime data and prune stale records."""
        try:
            self._reclaim_expired_request_leases()
        except Exception:  # noqa: BLE001
            logger.exception('清理任务回收超时请求租约失败')

        try:
            self.refresh_all_node_metrics()
        except Exception:  # noqa: BLE001
            logger.exception('清理任务刷新运行时指标失败')

        try:
            self.remove_stale_nodes_by_expiration()
        except Exception:  # noqa: BLE001
            logger.exception(
                '清理任务移除过期节点状态失败')

    async def _failed_notin_proccessing_node_status_logs(self) -> int:
        return await failed_notin_proccessing_node_status_logs_transactionally()

    def cleanup_node_status_task(self) -> None:
        """Retry failed node status logs that are not in processing state."""
        try:
            logger.debug("开始清理失败节点状态...")
            failed_count = run_until_complete(
                self._failed_notin_proccessing_node_status_logs()
            )
            if failed_count > 0:
                logger.info(
                    '已设置 {} 条非处理中状态的失败节点状态日志记录',
                    failed_count
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                '设置非处理中状态的失败节点状态日志记录失败'
            )

    @staticmethod
    def _month_start(value: datetime) -> datetime:
        """Normalize a datetime to month start (00:00:00 on day 1)."""

        return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _day_start(value: datetime) -> datetime:
        """Normalize a datetime to day start (00:00:00)."""

        return value.replace(hour=0, minute=0, second=0, microsecond=0)

    @classmethod
    def _week_start(cls, value: datetime) -> datetime:
        """Normalize a datetime to week start (Monday 00:00:00)."""

        return cls._day_start(value) - timedelta(days=value.weekday())

    @staticmethod
    def _subtract_months(value: datetime, months: int) -> datetime:
        """Subtract months from datetime while keeping timezone info."""

        safe_months = max(int(months), 0)
        year = value.year
        month = value.month - safe_months
        while month <= 0:
            month += 12
            year -= 1
        return value.replace(year=year, month=month)

    def _get_log_cutoff_by_days(self) -> datetime:
        """Calculate day-based cutoff for node logs retention."""

        now = datetime.now(tz=current_timezone())
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        hold_days = max(int(self.nodelogs_hold_days or 0), 0)
        return start_of_today - timedelta(days=hold_days)

    async def _remove_expired_node_status_logs(self) -> int:
        cutoff = self._get_log_cutoff_by_days()
        return await delete_proxy_node_status_logs_before_transactionally(
            before=cutoff,
        )

    def remove_expired_logs_task(self) -> None:
        """Remove expired node status logs."""
        try:
            logger.debug("开始清理过期的节点状态日志记录...")
            removed_count = run_until_complete(
                self._remove_expired_node_status_logs()
            )
            if removed_count > 0:
                logger.info(
                    '已删除 {} 条过期的节点状态日志记录',
                    removed_count
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                '删除过期的节点状态日志记录失败'
            )

    async def _rollup_previous_month_usage(self) -> int | None:
        """Aggregate previous month usage by ownerapp_id and model_name."""

        owner_token = await self._acquire_rollup_task_lock(
            task_name='monthly_usage_rollup',
            task_label='上月应用模型用量汇总',
        )
        if owner_token is None:
            return None

        now = datetime.now(tz=current_timezone())
        current_month_start = self._month_start(now)
        previous_month_start = self._subtract_months(current_month_start, 1)

        try:
            return await rollup_previous_month_usage_transactionally(
                previous_month_start=previous_month_start,
                current_month_start=current_month_start,
            )
        finally:
            await self._release_rollup_task_lock(
                task_name='monthly_usage_rollup',
                task_label='上月应用模型用量汇总',
                owner_token=owner_token,
            )

    async def _rollup_previous_day_usage(self) -> int | None:
        """Aggregate previous day usage by ownerapp_id and model_name."""

        owner_token = await self._acquire_rollup_task_lock(
            task_name='daily_usage_rollup',
            task_label='昨日应用模型用量汇总',
        )
        if owner_token is None:
            return None

        now = datetime.now(tz=current_timezone())
        current_day_start = self._day_start(now)
        previous_day_start = current_day_start - timedelta(days=1)

        try:
            return await rollup_previous_day_usage_transactionally(
                previous_day_start=previous_day_start,
                current_day_start=current_day_start,
            )
        finally:
            await self._release_rollup_task_lock(
                task_name='daily_usage_rollup',
                task_label='昨日应用模型用量汇总',
                owner_token=owner_token,
            )

    async def _rollup_previous_week_usage(self) -> int | None:
        """Aggregate previous week usage by ownerapp_id and model_name."""

        owner_token = await self._acquire_rollup_task_lock(
            task_name='weekly_usage_rollup',
            task_label='上周应用模型用量汇总',
        )
        if owner_token is None:
            return None

        now = datetime.now(tz=current_timezone())
        current_week_start = self._week_start(now)
        previous_week_start = current_week_start - timedelta(days=7)

        try:
            return await rollup_previous_week_usage_transactionally(
                previous_week_start=previous_week_start,
                current_week_start=current_week_start,
            )
        finally:
            await self._release_rollup_task_lock(
                task_name='weekly_usage_rollup',
                task_label='上周应用模型用量汇总',
                owner_token=owner_token,
            )

    def monthly_usage_rollup_task(self) -> None:
        """Run previous-month usage rollup task."""

        try:
            logger.debug("开始汇总上月应用模型用量...")
            upserted_count = run_until_complete(
                self._rollup_previous_month_usage())
            if upserted_count is not None:
                logger.info('上月应用模型用量汇总完成，记录数: {}', upserted_count)
        except Exception:  # noqa: BLE001
            logger.exception('汇总上月应用模型用量失败')

    def daily_usage_rollup_task(self) -> None:
        """Run previous-day usage rollup task."""

        try:
            logger.debug("开始汇总昨日应用模型用量...")
            upserted_count = run_until_complete(
                self._rollup_previous_day_usage())
            if upserted_count is not None:
                logger.info('昨日应用模型用量汇总完成，记录数: {}', upserted_count)
        except Exception:  # noqa: BLE001
            logger.exception('汇总昨日应用模型用量失败')

    def weekly_usage_rollup_task(self) -> None:
        """Run previous-week usage rollup task."""

        try:
            logger.debug("开始汇总上周应用模型用量...")
            upserted_count = run_until_complete(
                self._rollup_previous_week_usage())
            if upserted_count is not None:
                logger.info('上周应用模型用量汇总完成，记录数: {}', upserted_count)
        except Exception:  # noqa: BLE001
            logger.exception('汇总上周应用模型用量失败')
