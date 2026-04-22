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
from datetime import datetime, timedelta
from http import HTTPStatus
from openaiproxy.services.database.utils import get_db_process_id
import orjson
import os
import random
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
)
from openaiproxy.services.nodeproxy.exceptions import (
    ApiKeyQuotaExceeded,
    AppQuotaExceeded,
    NorthboundQuotaProcessingError,
    NodeModelQuotaExceeded,
)
from openaiproxy.services.deps import async_session_scope
from openaiproxy.utils.apikey import ApiKeyEncryptionError, decrypt_api_key
from openaiproxy.services.database.models.apikey.utils import (
    finalize_apikey_quota_usage,
    reserve_apikey_quota,
)
from openaiproxy.services.database.models.app.utils import (
    finalize_app_quota_usage,
    reserve_app_quota,
)
from openaiproxy.services.database.models.node.utils import (
    finalize_node_model_quota_usage,
    reserve_node_model_quota,
)
from openaiproxy.utils.async_helpers import run_until_complete
import requests

from openaiproxy.services.base import Service
from openaiproxy.services.database.models.node.crud import (
    aggregate_daily_model_usage,
    aggregate_monthly_model_usage,
    aggregate_weekly_model_usage,
    select_node_model_quotas,
    select_node_models,
    select_nodes,
    upsert_app_daily_model_usage,
    upsert_app_monthly_model_usage,
    upsert_app_weekly_model_usage,
)
from openaiproxy.services.database.models.proxy.crud import (
    acquire_database_task_lock,
    delete_proxy_node_status_logs_before,
    failed_notin_proccessing_node_status_logs,
    create_proxy_node_status_log_entry,
    fetch_proxy_node_metrics,
    get_or_create_proxy_node_status,
    release_database_task_lock,
    select_proxy_node_status,
    update_proxy_node_status_log_entry,
    upsert_proxy_node_status,
    upsert_proxy_instance,
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
from openaiproxy.services.nodeproxy.schemas import Status
from openaiproxy.services.database.models import ProxyNodeStatus
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.utils.timezone import current_timezone

if TYPE_CHECKING:
    from openaiproxy.services.settings.service import SettingsService
    from openaiproxy.services.database.models.proxy.model import ProxyInstance

NODE_HEALTH_CHECK_ENDPOINT = '/v1/models'
NODE_HEALTH_CHECK_TIMEOUT = (5, 15)
QUOTA_EXHAUSTION_BACKOFF_SECONDS = 300
ROLLUP_TASK_LOCK_SECONDS = 60 * 60


def heart_beat_controller(
    proxy_controller, stop_event: threading.Event
):
    while not stop_event.wait(proxy_controller.health_internval):
        logger.debug('开始执行心跳检查')
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


@dataclass
class _RequestContext:
    start_time: float
    first_response_time: Optional[float] = None
    model_name: Optional[str] = None
    model_type: Optional[str] = None
    request_protocol: ProtocolType = ProtocolType.openai
    ownerapp_id: Optional[str] = None
    request_action: RequestAction = RequestAction.completions
    request_tokens: Optional[int] = None
    response_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    stream: bool = False
    log_id: Optional[UUID] = None
    error: bool = False
    error_message: Optional[str] = None
    error_stack: Optional[str] = None
    request_data: Optional[str] = None
    response_data: Optional[str] = None
    abort: bool = False
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


@dataclass
class _QuotaReservation:
    quota_id: UUID
    usage_id: UUID


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
        self._node_metadata: Dict[str, _NodeMetadata] = {}
        self._offline_nodes: Dict[str, Status] = {}
        self._instance_name: Optional[str] = None
        self._instance_ip: Optional[str] = None
        self._instance_process_id: Optional[str] = None
        self._proxy_instance_registered = False
        self._quota_exhausted_models: Dict[str, Dict[tuple[str, str], float]] = {}
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
    ) -> _RequestContext:
        """Prepare runtime bookkeeping before dispatching a request."""

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
                detail = getattr(exc, 'detail', None) or self._format_model_detail(model_name, normalized_type)
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
        instance_id = str(self.proxy_instance_id) if self.proxy_instance_id else ""
        return f'{instance_id}:{instance_name}:{instance_ip}:{process_id}'

    async def _acquire_rollup_task_lock(self, *, task_name: str, task_label: str) -> str | None:
        """尝试获取报表任务锁，失败时返回空值并记录忽略日志。"""

        owner_token = self._build_rollup_task_owner_token()
        async with async_session_scope() as session:
            try:
                lock_acquired = await acquire_database_task_lock(
                    task_name=task_name,
                    owner_token=owner_token,
                    lease_seconds=ROLLUP_TASK_LOCK_SECONDS,
                    session=session,
                )
                if not lock_acquired:
                    await session.rollback()
                    logger.info('{}已有任务在执行，忽略本次调度', task_label)
                    return None
                await session.commit()
                return owner_token
            except Exception:
                await session.rollback()
                raise

    async def _release_rollup_task_lock(
        self,
        *,
        task_name: str,
        task_label: str,
        owner_token: str,
    ) -> None:
        """释放报表任务锁。"""

        async with async_session_scope() as session:
            try:
                await release_database_task_lock(
                    task_name=task_name,
                    owner_token=owner_token,
                    session=session,
                )
                await session.commit()
            except Exception:
                await session.rollback()
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
        async with async_session_scope() as session:
            try:
                db_process_id = await get_db_process_id(session)
                self._instance_process_id = db_process_id
                proxy_row = await upsert_proxy_instance(
                    session=session,
                    instance_id=desired_id,
                    instance_name=instance_name,
                    instance_ip=instance_ip,
                    process_id=db_process_id,
                )
                self._settings_service.settings.instance_id = str(proxy_row.id)
                return proxy_row
            except Exception:
                raise

    def _build_config_version(self, db_node, models: list[str]) -> str:
        updated_at = getattr(db_node, 'updated_at', None)
        timestamp = updated_at.isoformat() if updated_at else ''
        enabled_flag = getattr(db_node, 'enabled', True)
        models_part = ','.join(models)
        return f'{timestamp}:{int(bool(enabled_flag))}:{models_part}'

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
                model_records_map: dict[UUID, list[NodeModel]] = defaultdict(list)
                model_ids_set: set[UUID] = set()
                if node_ids:
                    db_models = await select_node_models(node_ids=node_ids, session=session)
                    for model in db_models:
                        if model.enabled is False:
                            continue
                        model_records_map[model.node_id].append(model)
                        if model.id is not None:
                            model_ids_set.add(model.id)

                quota_records_map: dict[UUID, list[NodeModelQuota]] = defaultdict(list)
                model_ids = list(model_ids_set)
                if model_ids:
                    quota_records = await select_node_model_quotas(
                        node_model_ids=model_ids,
                        session=session,
                    )
                    for quota in quota_records:
                        quota_records_map[quota.node_model_id].append(quota)

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
                            type_value = model_record.model_type.value if hasattr(model_record.model_type, 'value') else str(model_record.model_type)
                            normalized_type = str(type_value or ModelType.chat.value).lower()
                            type_candidates.add(normalized_type)
                            model_index[(model_name.lower(), normalized_type)] = model_record.id

                            detail_key = self._format_model_detail(model_name, normalized_type)
                            quota_entries = quota_records_map.get(model_record.id, []) if model_record.id is not None else []
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

                    status_obj = Status(
                        models=models,
                        types=status_types,
                        unfinished=int(unfinished),
                        latency=latency_deque,
                        speed=speed_value,
                        avaiaible=available_flag,
                        api_key=stored_api_key,
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
    def _should_probe_status(status: Status) -> bool:
        if status.trusted_without_models_endpoint:
            return False
        if status.health_check is False:
            return False
        return True

    @staticmethod
    def _build_backend_request_url(node_url: str, endpoint: str) -> str:
        """拼接节点请求地址，避免节点地址已带 `/v1` 时重复前缀。"""
        normalized_node_url = node_url.rstrip('/')
        normalized_endpoint = endpoint if endpoint.startswith('/') else f'/{endpoint}'
        if normalized_node_url.endswith('/v1') and normalized_endpoint == '/v1':
            normalized_endpoint = ''
        elif normalized_node_url.endswith('/v1') and normalized_endpoint.startswith('/v1/'):
            normalized_endpoint = normalized_endpoint[3:]
        return f'{normalized_node_url}{normalized_endpoint}'

    @staticmethod
    def _build_models_url(node_url: str) -> str:
        return NodeProxyService._build_backend_request_url(node_url, NODE_HEALTH_CHECK_ENDPOINT)

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

    def perform_node_health_checks(self) -> None:
        node_candidates: list[tuple[str, Optional[str], ProtocolType, Optional[str]]] = []
        with self._lock:
            for node_url, status in self.snode.items():
                if not self._should_probe_status(status):
                    continue
                node_candidates.append((
                    node_url,
                    status.api_key,
                    status.protocol_type,
                    status.request_proxy_url,
                ))

        for node_url, api_key, protocol_type, request_proxy_url in node_candidates:
            self._check_single_node(
                node_url=node_url,
                api_key=api_key,
                protocol_type=protocol_type,
                request_proxy_url=request_proxy_url,
            )

    def _check_single_node(
        self,
        node_url: str,
        api_key: Optional[str],
        protocol_type: ProtocolType,
        request_proxy_url: Optional[str],
    ) -> None:
        if not node_url:
            return

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
                self._build_models_url(node_url),
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
        finally:
            latency = max(time.time() - started_at, 0.0)

        self._apply_health_check_result(
            node_url=node_url,
            available=available,
            latency=latency,
            started_at=started_at,
            error_message=error_message,
        )

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
    ):
        """Select a node that can serve the requested model and type.

        Args:
            model_name (str): Model identifier requested by the client.
            model_type (Optional[str]): Optional model type hint (e.g. ``chat``).

        Returns:
            Optional[str]: The selected node URL, or ``None`` if unavailable.
        """

        normalized_type = self._normalize_model_type(model_type)
        detail = self._format_model_detail(model_name, normalized_type)

        def _select_candidate(
            matched_with_speed: list[tuple[str, float]],
            matched_without_speed: list[str],
            latency_map: dict[str, float],
        ) -> Optional[str]:
            all_matched_urls = [url for url, _ in matched_with_speed] + matched_without_speed
            if not all_matched_urls:
                return None

            speeds = [speed for _, speed in matched_with_speed]
            average_speed = sum(speeds) / len(speeds) if speeds else 1.0
            all_the_speeds = speeds + [average_speed] * len(matched_without_speed)

            if self.strategy == Strategy.RANDOM:
                speed_sum = sum(all_the_speeds)
                if speed_sum <= 0:
                    weights = [1 / len(all_the_speeds)] * len(all_the_speeds)
                else:
                    weights = [speed / speed_sum for speed in all_the_speeds]
                index = random.choices(range(len(all_matched_urls)), weights=weights)[0]
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
                latency_values = [latency_map.get(url, float('inf')) for url in all_matched_urls]
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
                    target_with_speed.append((node_url, float(node_status.speed)))
                else:
                    target_without_speed.append(node_url)
                if len(node_status.latency):
                    target_latency_map[node_url] = float(np.mean(np.array(node_status.latency)))
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

        async def _reserve() -> None:
            async with async_session_scope() as session:
                # API Key 配额
                if api_key_uuid is not None:
                    ak_result = await reserve_apikey_quota(
                        session=session,
                        api_key_id=api_key_uuid,
                        proxy_id=self.proxy_instance_id,
                        ownerapp_id=ownerapp_id,
                        model_name=context.model_name,
                        request_action=request_action,
                        estimated_total_tokens=estimated_total_tokens,
                    )
                    if ak_result is not None:
                        context.apikey_quota_id = ak_result[0]
                        context.apikey_quota_usage_id = ak_result[1]

                # 应用配额
                if ownerapp_id:
                    app_result = await reserve_app_quota(
                        session=session,
                        ownerapp_id=ownerapp_id,
                        api_key_id=api_key_uuid,
                        proxy_id=self.proxy_instance_id,
                        model_name=context.model_name,
                        request_action=request_action,
                        estimated_total_tokens=estimated_total_tokens,
                    )
                    if app_result is not None:
                        context.app_quota_id = app_result[0]
                        context.app_quota_usage_id = app_result[1]

        try:
            run_until_complete(_reserve())
        except (ApiKeyQuotaExceeded, AppQuotaExceeded):
            raise
        except Exception:  # noqa: BLE001
            logger.exception('北向配额预占失败 (api_key={}, app={})', api_key_id, ownerapp_id)
            raise NorthboundQuotaProcessingError('北向配额预占失败，请稍后重试')

    def _rollback_northbound_quota(self, context: _RequestContext) -> None:
        """南向配额失败时回滚北向配额预占。"""
        ak_quota_id = context.apikey_quota_id
        ak_usage_id = context.apikey_quota_usage_id
        app_quota_id = context.app_quota_id
        app_usage_id = context.app_quota_usage_id
        if not ak_quota_id and not app_quota_id:
            return

        api_key_uuid: Optional[UUID] = None
        if context.api_key_id:
            try:
                api_key_uuid = UUID(context.api_key_id)
            except (ValueError, AttributeError):
                pass

        async def _rollback() -> None:
            async with async_session_scope() as session:
                from sqlmodel import select as sql_select
                if ak_quota_id is not None:
                    from openaiproxy.services.database.models.apikey.model import (
                        ApiKeyQuota,
                        ApiKeyQuotaUsage,
                    )
                    stmt = sql_select(ApiKeyQuota).where(ApiKeyQuota.id == ak_quota_id).with_for_update()
                    result = await session.exec(stmt)
                    quota = result.first()
                    if quota is not None and quota.call_used > 0:
                        quota.call_used -= 1
                    if ak_usage_id is not None:
                        usage_stmt = sql_select(ApiKeyQuotaUsage).where(ApiKeyQuotaUsage.id == ak_usage_id)
                        usage_result = await session.exec(usage_stmt)
                        usage = usage_result.first()
                        if usage is not None:
                            await session.delete(usage)

                if app_quota_id is not None:
                    from openaiproxy.services.database.models.app.model import (
                        AppQuota,
                        AppQuotaUsage,
                    )
                    stmt = sql_select(AppQuota).where(AppQuota.id == app_quota_id).with_for_update()
                    result = await session.exec(stmt)
                    quota = result.first()
                    if quota is not None and quota.call_used > 0:
                        quota.call_used -= 1
                    if app_usage_id is not None:
                        usage_stmt = sql_select(AppQuotaUsage).where(AppQuotaUsage.id == app_usage_id)
                        usage_result = await session.exec(usage_stmt)
                        usage = usage_result.first()
                        if usage is not None:
                            await session.delete(usage)

        try:
            run_until_complete(_rollback())
        except Exception:  # noqa: BLE001
            logger.exception('回滚北向配额失败 (apikey_quota={}, app_quota={})', ak_quota_id, app_quota_id)
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

        async def _finalize() -> None:
            async with async_session_scope() as session:
                if (
                    api_key_uuid is not None
                    and context.apikey_quota_id is not None
                    and context.apikey_quota_usage_id is not None
                ):
                    await finalize_apikey_quota_usage(
                        session=session,
                        api_key_id=api_key_uuid,
                        primary_quota_id=context.apikey_quota_id,
                        primary_quota_usage_id=context.apikey_quota_usage_id,
                        total_tokens=total_tokens,
                        ownerapp_id=context.ownerapp_id,
                        model_name=context.model_name,
                        request_action=context.request_action,
                        log_id=context.log_id,
                    )

            async with async_session_scope() as session:
                if (
                    context.ownerapp_id
                    and context.app_quota_id is not None
                    and context.app_quota_usage_id is not None
                ):
                    await finalize_app_quota_usage(
                        session=session,
                        ownerapp_id=context.ownerapp_id,
                        primary_quota_id=context.app_quota_id,
                        primary_quota_usage_id=context.app_quota_usage_id,
                        total_tokens=total_tokens,
                        api_key_id=api_key_uuid,
                        model_name=context.model_name,
                        request_action=context.request_action,
                        log_id=context.log_id,
                    )

        try:
            run_until_complete(_finalize())
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
            detail = getattr(exc, 'detail', None) or model_name or str(node_model_id)
            logger.warning('节点 {} 的模型 {} 配额不足', node_url, detail)
            raise
        except Exception:  # noqa: BLE001
            logger.exception('节点 {} 预占模型 {} 配额失败', node_url, model_name or node_model_id)
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
        hint = detail or self._format_model_detail(model_name, model_type) or 'unknown'
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
        total_tokens = max(int(context.total_tokens or (request_tokens + response_tokens)), 0)

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
            detail = getattr(exc, 'detail', None) or context.model_name or str(context.node_model_id)
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
        if isinstance(context.total_tokens, int) and context.total_tokens >= 0:
            return context.total_tokens
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
                stream=context.stream,
                error=context.error,
                error_message=context.error_message,
                error_stack=context.error_stack,
                request_data=context.request_data,
                response_data=context.response_data,
                client_ip=context.client_ip,
                abort=context.abort,
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
                datetime.fromtimestamp(context.first_response_time, tz=current_timezone())
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
                    stream=context.stream,
                    error=context.error,
                    error_message=context.error_message,
                    error_stack=context.error_stack,
                    request_data=context.request_data,
                    response_data=context.response_data,
                    client_ip=context.client_ip,
                    abort=context.abort,
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
                    error=context.error,
                    error_message=context.error_message,
                    error_stack=context.error_stack,
                    request_data=context.request_data,
                    response_data=context.response_data,
                    abort=context.abort,
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
                speed=float(computed_speed if computed_speed is not None else -1.0),
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
    ) -> Optional[JSONResponse]:
        """Check if a request is valid."""
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
        request: Dict,
        node_url: str,
        endpoint: str,
        api_key: Optional[str] = None,
        *,
        protocol_type: ProtocolType = ProtocolType.openai,
        request_proxy_url: Optional[str] = None,
    ):
        """Return a generator to handle the input request.

        Args:
            request (Dict): the input request.
            node_url (str): the node url.
            endpoint (str): the endpoint. Such as `/v1/chat/completions`.
        """
        try:
            headers = self._build_backend_headers(
                api_key=api_key,
                protocol_type=protocol_type,
            )
            proxies = self._build_backend_proxy_mapping(request_proxy_url)
            target_url = self._build_backend_request_url(node_url, endpoint)
            with requests.post(
                target_url,
                json=request,
                headers=headers,
                proxies=proxies,
                stream=True,
                timeout=(60, API_READ_TIMEOUT),
            ) as response:
                for chunk in response.iter_lines(
                    decode_unicode=False,
                    delimiter=b'\n'
                ):
                    if chunk:
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
        request: Dict,
        node_url: str,
        endpoint: str,
        api_key: Optional[str] = None,
        *,
        protocol_type: ProtocolType = ProtocolType.openai,
        request_proxy_url: Optional[str] = None,
    ):
        """Return a the response of the input request.

        Args:
            request (Dict): the input request.
            node_url (str): the node url.
            endpoint (str): the endpoint. Such as `/v1/chat/completions`.
        """
        try:
            import httpx
            async with httpx.AsyncClient(proxy=request_proxy_url) as client:
                headers = self._build_backend_headers(
                    api_key=api_key,
                    protocol_type=protocol_type,
                )
                target_url = self._build_backend_request_url(node_url, endpoint)
                response = await client.post(
                    target_url,
                    json=request,
                    headers=headers,
                    timeout=API_READ_TIMEOUT
                )
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
        elapsed = time.time() - context.start_time
        if context.response_tokens is None:
            context.response_tokens = 0
        if context.total_tokens is None or context.total_tokens < 0:
            context.total_tokens = self._resolve_total_tokens(context)
        self._finalize_request_log(node_url, context, elapsed)
        self._apply_node_model_quota(node_url, context)
        try:
            self._apply_northbound_quota(context)
        except (ApiKeyQuotaExceeded, AppQuotaExceeded, NorthboundQuotaProcessingError) as exc:
            logger.exception('北向配额结算失败')
            self._mark_quota_processing_error(context, exc)
            self._finalize_request_log(node_url, context, elapsed)
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
            self.refresh_all_node_metrics()
        except Exception:  # noqa: BLE001
            logger.exception('清理任务刷新运行时指标失败')

        try:
            self.remove_stale_nodes_by_expiration()
        except Exception:  # noqa: BLE001
            logger.exception(
                '清理任务移除过期节点状态失败')

    async def _failed_notin_proccessing_node_status_logs(self) -> int:
        async with async_session_scope() as session:
            try:
                failed_count = await failed_notin_proccessing_node_status_logs(
                    session=session,
                )
                return failed_count
            except Exception:
                raise

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
        async with async_session_scope() as session:
            try:
                cutoff = self._get_log_cutoff_by_days()
                removed_count = await delete_proxy_node_status_logs_before(
                    session=session,
                    before=cutoff,
                )
                return removed_count
            except Exception:
                raise

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
            async with async_session_scope() as session:
                try:
                    usage_rows = await aggregate_monthly_model_usage(
                        month_start=previous_month_start,
                        month_end=current_month_start,
                        session=session,
                    )
                    for usage in usage_rows:
                        await upsert_app_monthly_model_usage(
                            month_start=previous_month_start,
                            usage=usage,
                            session=session,
                        )
                    await session.commit()
                    return len(usage_rows)
                except Exception:
                    await session.rollback()
                    raise
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
            async with async_session_scope() as session:
                try:
                    usage_rows = await aggregate_daily_model_usage(
                        day_start=previous_day_start,
                        day_end=current_day_start,
                        session=session,
                    )
                    for usage in usage_rows:
                        await upsert_app_daily_model_usage(
                            day_start=previous_day_start,
                            usage=usage,
                            session=session,
                        )
                    await session.commit()
                    return len(usage_rows)
                except Exception:
                    await session.rollback()
                    raise
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
            async with async_session_scope() as session:
                try:
                    usage_rows = await aggregate_weekly_model_usage(
                        week_start=previous_week_start,
                        week_end=current_week_start,
                        session=session,
                    )
                    for usage in usage_rows:
                        await upsert_app_weekly_model_usage(
                            week_start=previous_week_start,
                            usage=usage,
                            session=session,
                        )
                    await session.commit()
                    return len(usage_rows)
                except Exception:
                    await session.rollback()
                    raise
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
            upserted_count = run_until_complete(self._rollup_previous_month_usage())
            if upserted_count is not None:
                logger.info('上月应用模型用量汇总完成，记录数: {}', upserted_count)
        except Exception:  # noqa: BLE001
            logger.exception('汇总上月应用模型用量失败')

    def daily_usage_rollup_task(self) -> None:
        """Run previous-day usage rollup task."""

        try:
            logger.debug("开始汇总昨日应用模型用量...")
            upserted_count = run_until_complete(self._rollup_previous_day_usage())
            if upserted_count is not None:
                logger.info('昨日应用模型用量汇总完成，记录数: {}', upserted_count)
        except Exception:  # noqa: BLE001
            logger.exception('汇总昨日应用模型用量失败')

    def weekly_usage_rollup_task(self) -> None:
        """Run previous-week usage rollup task."""

        try:
            logger.debug("开始汇总上周应用模型用量...")
            upserted_count = run_until_complete(self._rollup_previous_week_usage())
            if upserted_count is not None:
                logger.info('上周应用模型用量汇总完成，记录数: {}', upserted_count)
        except Exception:  # noqa: BLE001
            logger.exception('汇总上周应用模型用量失败')
