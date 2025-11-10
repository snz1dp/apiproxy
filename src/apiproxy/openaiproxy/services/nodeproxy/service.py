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
from collections import deque, defaultdict
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus
import json
import os
import random
import socket
import threading
import time
import traceback
from typing import Any, Deque, Dict, Optional, TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse
import numpy as np
from openaiproxy.logging import logger
from openaiproxy.services.database.models.node.model import ModelType
from openaiproxy.services.database.models.node.utils import get_db_process_id
from openaiproxy.utils.async_helpers import run_until_complete
import requests

from openaiproxy.services.base import Service
from openaiproxy.services.database.models.node.crud import (
    select_node_models, select_nodes
)
from openaiproxy.services.database.models.proxy.crud import (
    select_proxy_node_status,
    get_or_create_proxy_node_status,
    upsert_proxy_node_status,
    create_proxy_node_status_log_entry,
    update_proxy_node_status_log_entry,
    fetch_proxy_node_metrics,
    upsert_proxy_instance,
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
from sqlalchemy import or_
from sqlmodel import select

if TYPE_CHECKING:
    from openaiproxy.services.settings.service import SettingsService
    from openaiproxy.services.database.service import DatabaseService
    from openaiproxy.services.database.models.proxy.model import ProxyInstance

CONTROLLER_HEART_BEAT_EXPIRATION = int(
    os.getenv('LMDEPLOY_CONTROLLER_HEART_BEAT_EXPIRATION', 90)
)


def heart_beat_controller(proxy_controller, stop_event: threading.Event):
    while not stop_event.wait(CONTROLLER_HEART_BEAT_EXPIRATION):
        logger.debug('开始执行心跳检查')
        proxy_controller.remove_stale_nodes_by_expiration()


def create_error_response(status: HTTPStatus,
                          message: str,
                          error_type='invalid_request_error'):
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


@dataclass
class _RequestContext:
    start_time: float
    model_name: Optional[str] = None
    ownerapp_id: Optional[str] = None
    request_action: RequestAction = RequestAction.completions
    request_tokens: Optional[int] = None
    response_tokens: Optional[int] = None
    log_id: Optional[UUID] = None
    error: bool = False
    error_message: Optional[str] = None
    error_stack: Optional[str] = None


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
        database_service: "DatabaseService",
    ) -> None:
        self._lock = threading.RLock()
        self.nodes = dict()
        self.snode = dict()
        settings = settings_service.settings
        self.strategy = Strategy.from_str(settings.proxy_strategy)
        self.database_service = database_service
        self._settings_service = settings_service
        self._stop_event = threading.Event()
        self._refresh_interval = settings.refresh_interval
        self.proxy_instance_id = settings.instance_id
        self._cleanup_interval = settings.cleanup_interval
        self._node_metadata: Dict[str, _NodeMetadata] = {}
        self._offline_nodes: Dict[str, Status] = {}
        self._instance_name: Optional[str] = None
        self._instance_ip: Optional[str] = None
        self._instance_process_id: Optional[str] = None
        self._proxy_instance_registered = False

        if self.database_service is not None:
            try:
                self._ensure_proxy_instance_registration()
            except Exception:  # noqa: BLE001
                logger.exception(
                    '初始化时注册代理实例失败')

        if self.database_service is not None:
            try:
                run_until_complete(
                    self._refresh_nodes_from_database(initial_load=True))
            except Exception:  # noqa: BLE001
                logger.exception(
                    '初始化时从数据库加载节点配置失败')

        if self.database_service is not None and self._refresh_interval is not None:
            self.config_refresh_thread = threading.Thread(
                target=self._refresh_loop,
                name='node-manager-refresh',
                daemon=True,
            )
            self.config_refresh_thread.start()
        else:
            self.config_refresh_thread = None

        self.heart_beat_thread = threading.Thread(
            target=heart_beat_controller,
            args=(self, self._stop_event),
            daemon=True
        )
        self.heart_beat_thread.start()

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

    def _ensure_proxy_instance_registration(self) -> None:
        if self.database_service is None:
            return

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
        if self.database_service is None:
            return None

        async with self.database_service.with_async_session() as session:
            try:
                db_process_id = await get_db_process_id(session=session)
                self._instance_process_id = db_process_id
                proxy_row = await upsert_proxy_instance(
                    session=session,
                    instance_id=desired_id,
                    instance_name=instance_name,
                    instance_ip=instance_ip,
                    process_id=db_process_id,
                )
                await session.commit()
                self._settings_service.settings.instance_id = str(proxy_row.id)
                return proxy_row
            except Exception:
                await session.rollback()
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
        if self.database_service is None:
            return

        with self._lock:
            previous_nodes = {
                url: copy.deepcopy(status) for url, status in self.snode.items()
            }
            previous_metadata = dict(self._node_metadata)

        async with self.database_service.with_async_session() as session:
            db_nodes = await select_nodes(enabled=True, session=session)
            new_nodes: Dict[str, Status] = {}
            new_snode: Dict[str, Status] = {}
            new_metadata: Dict[str, _NodeMetadata] = {}
            config_changed: set[str] = set()

            if db_nodes:
                node_ids = [
                    node.id for node in db_nodes if node.id is not None
                ]
                models_map: dict[UUID, list[str]] = defaultdict(list)
                types_map: dict[UUID, set[str]] = defaultdict(set)
                if node_ids:
                    db_models = await select_node_models(node_ids=node_ids, session=session)
                    for model in db_models:
                        if model.enabled is False:
                            continue
                        model_name = model.model_name
                        if not model_name:
                            continue
                        models_map[model.node_id].append(model_name)
                        model_type = model.model_type.value if hasattr(
                            model.model_type, 'value'
                        ) else str(model.model_type)
                        model_type = model_type if model_type else ModelType.chat
                        types_map[model.node_id].add(str(model_type).lower())

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

                for db_node in db_nodes:
                    node_url = db_node.url
                    if not node_url:
                        continue

                    status_row = status_map.get(db_node.id) if db_node.id else None

                    models = sorted(
                        set(models_map.get(db_node.id, []))
                    ) if db_node.id else []

                    enabled_flag = db_node.enabled if db_node.enabled is not None else True
                    available_flag = bool(enabled_flag)
                    if status_row and status_row.avaiaible is not None:
                        available_flag = available_flag and bool(status_row.avaiaible)

                    type_candidates = types_map.get(
                        db_node.id, set()
                    ) if db_node.id else set()
                    status_types = sorted(type_candidates)
                    if not status_types and db_node.name:
                        status_types = ["chat"]

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

                    latency_deque = deque(latency_samples, maxlen=LATENCY_DEQUE_LEN)

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
                            speed=float(speed_value if speed_value is not None else -1.0),
                            avaiaible=bool(available_flag),
                        )
                        status_id = status_entry.id

                    status_obj = Status(
                        models=models,
                        types=status_types,
                        unfinished=int(unfinished),
                        latency=latency_deque,
                        speed=speed_value,
                        avaiaible=available_flag,
                        api_key=db_node.api_key,
                        health_check=db_node.health_check,
                    )

                    new_snode[node_url] = status_obj
                    if status_obj.avaiaible and status_obj.models:
                        new_nodes[node_url] = status_obj

                    config_version = self._build_config_version(db_node, models)
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
                    )

                await session.commit()

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
                        health_check=None,
                    )
                else:
                    offline_status = copy.deepcopy(offline_status)
                    offline_status.avaiaible = False
                    offline_status.unfinished = 0
                    if not isinstance(offline_status.latency, deque):
                        offline_status.latency = deque(
                            list(offline_status.latency), maxlen=LATENCY_DEQUE_LEN)
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
        if added or removed:
            logger.info(
                '节点配置已更新，新增节点: {}，移除节点: {}',
                sorted(added),
                sorted(removed),
            )

        if initial_load and not new_nodes:
            logger.warning(
                '初始化时未从数据库加载到可用节点')

    @property
    def model_list(self):
        """Supported model list."""
        model_names = []
        with self._lock:
            for node_url, node_status in self.snode.items():
                model_names.extend(node_status.models)
        return model_names

    def supports_model(self, model_name: str, model_type: Optional[str] = None) -> bool:
        """Return whether any node supports the requested model and optional type."""
        normalized_type = self._normalize_model_type(model_type)
        with self._lock:
            for node_status in self.snode.values():
                if self._status_supports_model(node_status, model_name, normalized_type):
                    return True
        return False

    @property
    def cleanup_interval(self) -> int:
        """Return the preferred cleanup interval in seconds."""
        return self._cleanup_interval

    @property
    def status(self):
        """Return the status."""
        noderet = dict()
        with self._lock:
            for node_url, node_status in self.snode.items():
                if node_url in self.nodes.keys():
                    noderet[node_url] = copy.deepcopy(self.nodes[node_url])
                    noderet[node_url].avaiaible = True
                else:
                    noderet[node_url] = copy.deepcopy(node_status)
                    noderet[node_url].avaiaible = False

        return noderet

    def get_node_url(self, model_name: str, model_type: Optional[str] = None):
        """Select a node that can serve the requested model and type.

        Args:
            model_name (str): Model identifier requested by the client.
            model_type (Optional[str]): Optional model type hint (e.g. ``chat``).

        Returns:
            Optional[str]: The selected node URL, or ``None`` if unavailable.
        """

        normalized_type = self._normalize_model_type(model_type)

        with self._lock:
            def get_matched_urls():
                urls_with_speeds, speeds, urls_without_speeds = [], [], []
                for node_url, node_status in self.nodes.items():
                    if not self._status_supports_model(node_status, model_name, normalized_type):
                        continue
                    if node_status.speed is not None:
                        urls_with_speeds.append(node_url)
                        speeds.append(node_status.speed)
                    else:
                        urls_without_speeds.append(node_url)
                all_matched_urls = urls_with_speeds + urls_without_speeds
                if len(all_matched_urls) == 0:
                    return None
                # Some nodes do not record speed; approximate with average speed of known nodes.
                average_speed = sum(speeds) / len(speeds) if len(speeds) else 1
                all_the_speeds = speeds + \
                    [average_speed] * len(urls_without_speeds)
                return all_matched_urls, all_the_speeds

            if self.strategy == Strategy.RANDOM:
                result = get_matched_urls()
                if result is None:
                    return None
                all_matched_urls, all_the_speeds = result
                if len(all_matched_urls) == 0:
                    return None
                speed_sum = sum(all_the_speeds)
                if speed_sum <= 0:
                    weights = [1 / len(all_the_speeds)] * len(all_the_speeds)
                else:
                    weights = [speed / speed_sum for speed in all_the_speeds]
                index = random.choices(
                    range(len(all_matched_urls)), weights=weights)[0]
                url = all_matched_urls[index]
                return url
            elif self.strategy == Strategy.MIN_EXPECTED_LATENCY:
                result = get_matched_urls()
                if result is None:
                    return None
                all_matched_urls, all_the_speeds = result
                if len(all_matched_urls) == 0:
                    return None
                min_latency = float('inf')
                min_index = 0
                # Randomly traverse nodes for low concurrency situations.
                all_indexes = [i for i in range(len(all_the_speeds))]
                random.shuffle(all_indexes)
                for index in all_indexes:
                    node_url = all_matched_urls[index]
                    unfinished = self.nodes[node_url].unfinished
                    speed = all_the_speeds[index] or 1
                    latency = unfinished / speed
                    if min_latency > latency:
                        min_latency = latency
                        min_index = index
                url = all_matched_urls[min_index]
                return url
            elif self.strategy == Strategy.MIN_OBSERVED_LATENCY:
                all_matched_urls, latencies = [], []
                for node_url, node_status in self.nodes.items():
                    if not self._status_supports_model(node_status, model_name, normalized_type):
                        continue
                    if len(node_status.latency):
                        latencies.append(
                            np.mean(np.array(node_status.latency)))
                    else:
                        latencies.append(float('inf'))
                    all_matched_urls.append(node_url)
                if len(all_matched_urls) == 0:
                    return None
                index = int(np.argmin(np.array(latencies)))
                return all_matched_urls[index]
            else:
                raise ValueError(f'Invalid strategy: {self.strategy}')

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

    def _record_request_start(self, node_url: str, context: _RequestContext) -> None:
        if self.database_service is None or self.proxy_instance_id is None:
            return

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
        if self.database_service is None or self.proxy_instance_id is None:
            return None

        async with self.database_service.with_async_session() as session:
            status_row = await get_or_create_proxy_node_status(
                session=session,
                node_id=meta.node_id,
                proxy_id=self.proxy_instance_id,
                status_id=meta.status_id,
            )
            meta.status_id = status_row.id

            try:
                start_at = datetime.fromtimestamp(context.start_time, tz=current_timezone())
            except (OSError, OverflowError, ValueError):  # pragma: no cover - defensive
                start_at = datetime.now(tz=current_timezone())

            log_entry = await create_proxy_node_status_log_entry(
                session=session,
                node_id=meta.node_id,
                proxy_id=self.proxy_instance_id,
                status_id=status_row.id,
                ownerapp_id=context.ownerapp_id,
                model_name=context.model_name,
                action=context.request_action,
                start_at=start_at,
                end_at=None,
                latency=0.0,
                request_tokens=int(context.request_tokens or 0),
                response_tokens=0,
                error=context.error,
                error_message=context.error_message,
                error_stack=context.error_stack,
            )

            await session.commit()
            return log_entry.id

    def _finalize_request_log(self, node_url: str, context: _RequestContext, elapsed: float) -> None:
        if self.database_service is None or self.proxy_instance_id is None:
            return

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
        if self.database_service is None or self.proxy_instance_id is None:
            return

        meta = self._node_metadata.get(node_url)
        if meta is None or meta.node_id is None or meta.removed:
            return

        try:
            start_at = datetime.fromtimestamp(context.start_time, tz=current_timezone())
        except (OSError, OverflowError, ValueError):  # pragma: no cover - defensive
            start_at = datetime.now(tz=current_timezone()) - timedelta(seconds=elapsed)
        end_at = start_at + timedelta(seconds=elapsed)

        async with self.database_service.with_async_session() as session:
            status_row = await get_or_create_proxy_node_status(
                session=session,
                node_id=meta.node_id,
                proxy_id=self.proxy_instance_id,
                status_id=meta.status_id,
            )
            meta.status_id = status_row.id

            if context.log_id is None:
                await create_proxy_node_status_log_entry(
                    session=session,
                    node_id=meta.node_id,
                    proxy_id=self.proxy_instance_id,
                    status_id=status_row.id,
                    ownerapp_id=context.ownerapp_id,
                    model_name=context.model_name,
                    action=context.request_action,
                    start_at=start_at,
                    end_at=end_at,
                    latency=float(elapsed),
                    request_tokens=int(context.request_tokens or 0),
                    response_tokens=int(context.response_tokens or 0),
                    error=context.error,
                    error_message=context.error_message,
                    error_stack=context.error_stack,
                )
            else:
                await update_proxy_node_status_log_entry(
                    session=session,
                    log_id=context.log_id,
                    end_at=end_at,
                    latency=float(elapsed),
                    request_tokens=int(context.request_tokens or 0),
                    response_tokens=int(context.response_tokens or 0),
                    error=context.error,
                    error_message=context.error_message,
                    error_stack=context.error_stack,
                )

            await session.commit()

    def _refresh_node_metrics(self, node_url: str) -> None:
        if self.database_service is None or self.proxy_instance_id is None:
            return

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
        if self.database_service is None or self.proxy_instance_id is None:
            return _NodeMetrics(unfinished=0, latency_samples=[], average_latency=None, speed=None)

        async with self.database_service.with_async_session() as session:
            status_row = await get_or_create_proxy_node_status(
                session=session,
                node_id=meta.node_id,
                proxy_id=self.proxy_instance_id,
                status_id=meta.status_id,
            )
            meta.status_id = status_row.id

            unfinished, average_latency, speed, latency_samples = await fetch_proxy_node_metrics(
                session=session,
                node_id=meta.node_id,
                proxy_id=self.proxy_instance_id,
                history_limit=LATENCY_DEQUE_LEN,
            )

            if not latency_samples and status_row.latency and status_row.latency > 0:
                latency_samples = [float(status_row.latency)]

            computed_speed = speed
            if computed_speed is None and average_latency and average_latency > 0:
                computed_speed = 1.0 / average_latency

            status_row.unfinished = unfinished
            status_row.latency = float(average_latency or 0.0)
            status_row.speed = float(computed_speed if computed_speed is not None else -1.0)
            status_row.avaiaible = bool(status.avaiaible)
            session.add(status_row)

            await session.commit()

        return _NodeMetrics(
            unfinished=unfinished,
            latency_samples=latency_samples,
            average_latency=average_latency,
            speed=computed_speed,
        )

    def refresh_all_node_metrics(self) -> None:
        if self.database_service is None or self.proxy_instance_id is None:
            return

        for node_url in list(self.snode.keys()):
            self._refresh_node_metrics(node_url)

    def remove_stale_nodes_by_expiration(self) -> None:
        expiration_cutoff = datetime.now(tz=current_timezone(
        )) - timedelta(seconds=CONTROLLER_HEART_BEAT_EXPIRATION)
        if self.database_service is None:
            return

        removed = run_until_complete(
            self._remove_stale_nodes_by_expiration_async(expiration_cutoff)
        )
        if removed:
            logger.info('已删除 {} 条超过 {} 秒的过期节点状态记录',
                        removed, CONTROLLER_HEART_BEAT_EXPIRATION)

    async def _remove_stale_nodes_by_expiration_async(self, expiration_cutoff: datetime) -> int:
        if self.database_service is None:
            return 0

        async with self.database_service.with_async_session() as session:
            try:
                stmt = select(ProxyNodeStatus).where(ProxyNodeStatus.updated_at < expiration_cutoff)
                if self.proxy_instance_id is None:
                    stmt = stmt.where(ProxyNodeStatus.proxy_id.is_not(None))
                else:
                    stmt = stmt.where(
                        or_(
                            ProxyNodeStatus.proxy_id.is_(None),
                            ProxyNodeStatus.proxy_id != self.proxy_instance_id,
                        )
                    )

                result = await session.exec(stmt)
                stale_rows = result.all()
                if not stale_rows:
                    await session.commit()
                    return 0

                now_ts = datetime.now(tz=current_timezone())
                for row in stale_rows:
                    proxy_id = row.proxy_id or self.proxy_instance_id
                    if proxy_id is None:
                        continue
                    start_at = row.updated_at or now_ts
                    latency_value = max(0.0, (now_ts - start_at).total_seconds())
                    try:
                        await create_proxy_node_status_log_entry(
                            session=session,
                            node_id=row.node_id,
                            proxy_id=proxy_id,
                            status_id=row.id,
                            ownerapp_id=None,
                            model_name=None,
                            action=RequestAction.healthcheck,
                            start_at=start_at,
                            end_at=now_ts,
                            latency=latency_value,
                            request_tokens=0,
                            response_tokens=0,
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
                                model_name=None,
                                action=RequestAction.healthcheck,
                                start_at=start_at,
                                end_at=now_ts,
                                latency=latency_value,
                                request_tokens=0,
                                response_tokens=0,
                                error=True,
                                error_message='Heartbeat log persistence failed',
                                error_stack=stack,
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception('二次记录节点 {} 的健康检查错误信息失败', row.node_id)

                for row in stale_rows:
                    await session.delete(row)

                await session.commit()
                return len(stale_rows)
            except Exception:
                await session.rollback()
                raise

    async def check_request_model(self, model_name: str, model_type: Optional[str] = None) -> Optional[JSONResponse]:
        """Check if a request is valid."""
        if self.supports_model(model_name, model_type):
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
        return json.dumps(ret).encode() + b'\n'

    def handle_api_timeout(self, node_url):
        """Handle the api time out."""
        logger.warning(f'接口调用超时: {node_url}')
        ret = {
            'error_code': ErrorCodes.API_TIMEOUT.value,
            'text': err_msg[ErrorCodes.API_TIMEOUT],
        }
        return json.dumps(ret).encode() + b'\n'

    def stream_generate(self, request: Dict, node_url: str, endpoint: str, api_key: Optional[str] = None):
        """Return a generator to handle the input request.

        Args:
            request (Dict): the input request.
            node_url (str): the node url.
            endpoint (str): the endpoint. Such as `/v1/chat/completions`.
        """
        try:
            headers = None
            if api_key is not None:
                headers = {'Authorization': f'Bearer {api_key}'}
            response = requests.post(
                node_url + endpoint,
                json=request,
                headers=headers,
                stream=True,
                timeout=(60, API_READ_TIMEOUT),
            )
            for chunk in response.iter_lines(
                decode_unicode=False,
                delimiter=b'\n'
            ):
                if chunk:
                    yield chunk + b'\n\n'
        except (Exception, GeneratorExit, requests.RequestException) as e:  # noqa
            logger.error(f'捕获到异常: {e}')
            # exception happened, reduce unfinished num
            yield self.handle_api_timeout(node_url)

    async def generate(self, request: Dict, node_url: str, endpoint: str, api_key: Optional[str] = None):
        """Return a the response of the input request.

        Args:
            request (Dict): the input request.
            node_url (str): the node url.
            endpoint (str): the endpoint. Such as `/v1/chat/completions`.
        """
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                headers = None
                if api_key is not None:
                    headers = {'Authorization': f'Bearer {api_key}'}
                response = await client.post(
                    node_url + endpoint,
                    json=request,
                    headers=headers,
                    timeout=API_READ_TIMEOUT
                )
                return response.text
        except (Exception, GeneratorExit, requests.RequestException, asyncio.CancelledError) as e:  # noqa  # yapf: disable
            logger.error(f'捕获到异常: {e}')
            return self.handle_api_timeout(node_url)

    def pre_call(
        self,
        node_url: str,
        request_action: RequestAction,
        *,
        model_name: Optional[str] = None,
        ownerapp_id: Optional[str] = None,
        request_count: Optional[int] = None,
    ) -> _RequestContext:
        """Prepare runtime bookkeeping before dispatching a request."""

        context = _RequestContext(
            start_time=time.time(),
            model_name=model_name,
            ownerapp_id=ownerapp_id,
            request_tokens=request_count,
            request_action=request_action,
        )
        self._record_request_start(node_url, context)
        self._refresh_node_metrics(node_url)
        return context

    def post_call(self, node_url: str, context: _RequestContext):
        """Finalize bookkeeping after a request completes."""
        elapsed = time.time() - context.start_time
        if context.response_tokens is None:
            context.response_tokens = 0
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

    def cleanup_runtime_state(self) -> None:
        """Flush cached runtime data and prune stale records."""

        if self.database_service is None:
            logger.debug(
                '跳过节点代理清理：未配置数据库服务')
            return

        try:
            self.refresh_all_node_metrics()
        except Exception:  # noqa: BLE001
            logger.exception(
                '清理任务刷新运行时指标失败')

        try:
            self.remove_stale_nodes_by_expiration()
        except Exception:  # noqa: BLE001
            logger.exception(
                '清理任务移除过期节点状态失败')
