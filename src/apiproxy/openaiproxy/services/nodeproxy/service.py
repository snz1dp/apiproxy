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
import threading
import time
from typing import Deque, Dict, Optional, Set
from uuid import UUID

from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse
import numpy as np
from openaiproxy.logging import logger
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
    delete_stale_proxy_node_status,
)
from openaiproxy.services.nodeproxy.schemas import ErrorResponse
from openaiproxy.services.nodeproxy.constants import (
    API_READ_TIMEOUT, LATENCY_DEQUE_LEN,
    ErrorCodes, Strategy, err_msg
)
from openaiproxy.services.nodeproxy.schemas import Status
from openaiproxy.services.database.service import DatabaseService
from openaiproxy.services.database.models import ProxyNodeStatus
from openaiproxy.services.database.models.proxy.model import Action
from openaiproxy.utils.timezone import current_timezone

CONTROLLER_HEART_BEAT_EXPIRATION = int(
    os.getenv('LMDEPLOY_CONTROLLER_HEART_BEAT_EXPIRATION', 90)
)


def heart_beat_controller(proxy_controller, stop_event: threading.Event):
    while not stop_event.wait(CONTROLLER_HEART_BEAT_EXPIRATION):
        logger.info('Start heart beat check')
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
    return JSONResponse(ErrorResponse(message=message,
                                      type=error_type,
                                      code=status.value).model_dump(),
                        status_code=status.value)


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
    token_count: Optional[int] = None


@dataclass
class _RequestLogEntry:
    node_url: str
    node_id: UUID
    model_name: Optional[str]
    ownerapp_id: Optional[str]
    start_at: datetime
    end_at: datetime
    latency: float
    token_count: int

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
        strategy: str = 'min_expected_latency',
        database_service: Optional[DatabaseService] = None,
        refresh_interval: Optional[int] = None,
        proxy_instance_id: Optional[str | UUID] = None,
    ) -> None:
        self._lock = threading.RLock()
        self.nodes = dict()
        self.snode = dict()
        self.strategy = Strategy.from_str(strategy)
        self.latencies = dict()
        self.database_service = database_service
        self._stop_event = threading.Event()
        self._refresh_interval = self._coerce_interval(refresh_interval)
        self.proxy_instance_id = self._coerce_proxy_id(proxy_instance_id)
        self._node_metadata: Dict[str, _NodeMetadata] = {}
        self._offline_nodes: Dict[str, Status] = {}
        self._dirty_nodes: Set[str] = set()
        self._pending_request_logs: Deque[_RequestLogEntry] = deque()
        self._log_lock = threading.Lock()
        self._state_flush_interval = 5
        self._state_flush_event = threading.Event()

        if self.database_service is not None:
            try:
                run_until_complete(self._refresh_nodes_from_database(initial_load=True))
            except Exception:  # noqa: BLE001
                logger.exception('Failed to load node configuration from database during initialization')

        if self.database_service is not None and self._refresh_interval is not None:
            self.config_refresh_thread = threading.Thread(
                target=self._refresh_loop,
                name='node-manager-refresh',
                daemon=True,
            )
            self.config_refresh_thread.start()
        else:
            self.config_refresh_thread = None

        if self.database_service is not None and self.proxy_instance_id is not None:
            self._state_flush_thread = threading.Thread(
                target=self._state_flush_loop,
                name='node-manager-state-flush',
                daemon=True,
            )
            self._state_flush_thread.start()
        else:
            self._state_flush_thread = None

        self.heart_beat_thread = threading.Thread(
            target=heart_beat_controller,
            args=(self, self._stop_event),
            daemon=True
        )
        self.heart_beat_thread.start()

    def _coerce_interval(self, interval: Optional[int]) -> Optional[int]:
        default_interval = 60
        if interval is None:
            return default_interval
        try:
            coerced = int(interval)
            if coerced <= 0:
                logger.info('Refresh interval %s disables periodic refresh', interval)
                return None
            return coerced
        except (TypeError, ValueError):
            logger.warning('Invalid refresh interval provided: %s, using default %s seconds', interval, default_interval)
            return default_interval

    def _coerce_proxy_id(self, proxy_instance_id: Optional[str | UUID]) -> Optional[UUID]:
        if proxy_instance_id is None:
            return None
        try:
            return proxy_instance_id if isinstance(proxy_instance_id, UUID) else UUID(str(proxy_instance_id))
        except (TypeError, ValueError):
            logger.warning('Invalid proxy_instance_id provided, ignoring value: %s', proxy_instance_id)
            return None

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
                logger.exception('Failed to refresh node configuration from database')
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
            config_changed: Set[str] = set()

            if db_nodes:
                node_ids = [node.id for node in db_nodes if node.id is not None]
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
                        model_type = model.model_type.value if hasattr(model.model_type, 'value') else str(model.model_type)
                        types_map[model.node_id].add(model_type)

                status_map: dict[UUID, ProxyNodeStatus] = {}
                if node_ids:
                    db_statuses = await select_proxy_node_status(
                        proxy_instance_ids=[self.proxy_instance_id] if self.proxy_instance_id else None,
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

                    prev_status = previous_nodes.get(node_url)
                    prev_latency = list(prev_status.latency) if prev_status else []
                    latency_deque = deque(prev_latency, maxlen=LATENCY_DEQUE_LEN)

                    status_row = status_map.get(db_node.id) if db_node.id else None
                    if status_row and status_row.latency is not None:
                        latency_deque.append(status_row.latency)

                    models = sorted(set(models_map.get(db_node.id, []))) if db_node.id else []
                    unfinished = status_row.unfinished if status_row else (prev_status.unfinished if prev_status else 0)
                    speed = status_row.speed if status_row and status_row.speed is not None else (prev_status.speed if prev_status else None)
                    enabled_flag = db_node.enabled if db_node.enabled is not None else True
                    available_flag = bool(enabled_flag)
                    if status_row and status_row.avaiaible is not None:
                        available_flag = available_flag and bool(status_row.avaiaible)

                    status_type = db_node.name
                    if not status_type:
                        type_candidates = types_map.get(db_node.id, set()) if db_node.id else set()
                        if len(type_candidates) == 1:
                            status_type = next(iter(type_candidates))

                    status_obj = Status(
                        models=models,
                        type=status_type,
                        unfinished=unfinished,
                        latency=latency_deque,
                        speed=speed,
                        avaiaible=available_flag,
                        api_key=db_node.api_key,
                        health_check=db_node.health_check,
                    )

                    new_snode[node_url] = status_obj
                    if status_obj.avaiaible and status_obj.models:
                        new_nodes[node_url] = status_obj

                    config_version = self._build_config_version(db_node, models)
                    prev_meta = previous_metadata.get(node_url)
                    status_id = status_row.id if status_row else (prev_meta.status_id if prev_meta else None)
                    last_snapshot = None
                    if prev_meta and prev_meta.config_version == config_version:
                        last_snapshot = prev_meta.last_snapshot
                    else:
                        config_changed.add(node_url)

                    new_metadata[node_url] = _NodeMetadata(
                        node_id=db_node.id,
                        config_version=config_version,
                        status_id=status_id,
                        last_snapshot=last_snapshot,
                        removed=False,
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
                        type=None,
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
                        offline_status.latency = deque(list(offline_status.latency), maxlen=LATENCY_DEQUE_LEN)
                self._offline_nodes[url] = offline_status
                self._mark_node_dirty(url)

            added_urls = current_urls - prev_urls
            for url in added_urls:
                self._offline_nodes.pop(url, None)

            for url in config_changed:
                if url in metadata:
                    metadata[url].last_snapshot = None
                    self._mark_node_dirty(url)

            self._node_metadata = metadata

        added = current_urls - prev_urls
        removed = prev_urls - current_urls
        if added or removed:
            logger.info(
                'Node configuration changed. Added: %s, Removed: %s',
                sorted(added),
                sorted(removed),
            )

        if (added or removed or config_changed) and self.database_service is not None and self.proxy_instance_id is not None:
            self._state_flush_event.set()

        if initial_load and not new_nodes:
            logger.warning('No active nodes loaded from database during initialization')

    @property
    def model_list(self):
        """Supported model list."""
        model_names = []
        with self._lock:
            for node_url, node_status in self.snode.items():
                model_names.extend(node_status.models)
        return model_names

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

    def get_node_url(self, model_name: str):
        """Add a node to the manager.

        Args:
            model_name (str): A http url. Can be the url generated by
                `lmdeploy serve api_server`.
        Return:
            A node url or None.
        """

        with self._lock:
            def get_matched_urls():
                urls_with_speeds, speeds, urls_without_speeds = [], [], []
                for node_url, node_status in self.nodes.items():
                    if model_name in node_status.models:
                        if node_status.speed is not None:
                            urls_with_speeds.append(node_url)
                            speeds.append(node_status.speed)
                        else:
                            urls_without_speeds.append(node_url)
                all_matched_urls = urls_with_speeds + urls_without_speeds
                if len(all_matched_urls) == 0:
                    return None
                # some nodes does not contain speed
                # we can set them the average speed value
                average_speed = sum(speeds) / len(speeds) if len(speeds) else 1
                all_the_speeds = speeds + [average_speed
                                           ] * len(urls_without_speeds)
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
                index = random.choices(range(len(all_matched_urls)),
                                       weights=weights)[0]
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
                # random traverse nodes for low concurrency situation
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
                    if model_name in node_status.models:
                        if len(node_status.latency):
                            latencies.append(np.mean(np.array(
                                node_status.latency)))
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

    def _mark_node_dirty(self, node_url: str) -> None:
        with self._lock:
            if node_url in self._node_metadata:
                self._dirty_nodes.add(node_url)
        self._state_flush_event.set()

    def _enqueue_request_log(self, node_url: str, context: _RequestContext, elapsed: float) -> None:
        if self.database_service is None or self.proxy_instance_id is None:
            return
        meta = self._node_metadata.get(node_url)
        if meta is None or meta.node_id is None or meta.removed:
            return

        try:
            start_at = datetime.fromtimestamp(context.start_time, tz=current_timezone())
        except (OSError, OverflowError, ValueError):  # pragma: no cover - defensive
            start_at = datetime.now(tz=current_timezone())
        end_at = start_at + timedelta(seconds=elapsed)

        entry = _RequestLogEntry(
            node_url=node_url,
            node_id=meta.node_id,
            model_name=context.model_name,
            ownerapp_id=context.ownerapp_id,
            start_at=start_at,
            end_at=end_at,
            latency=float(elapsed),
            token_count=int(context.token_count or 0),
        )

        with self._log_lock:
            self._pending_request_logs.append(entry)
        self._state_flush_event.set()

    def _state_flush_loop(self) -> None:
        while not self._stop_event.is_set():
            self._state_flush_event.wait(self._state_flush_interval)
            self._state_flush_event.clear()
            try:
                self._flush_runtime_state()
            except Exception:  # noqa: BLE001
                logger.exception('Failed to persist node runtime state')

        try:
            self._flush_runtime_state(force=True)
        except Exception:  # noqa: BLE001
            logger.exception('Failed to persist node runtime state during shutdown')

    def _collect_state_snapshots(self, *, force: bool = False):
        snapshots: Dict[str, tuple[_NodeMetadata, tuple[int, float, float, bool]]] = {}
        with self._lock:
            if force:
                target_urls = set(self._node_metadata.keys())
            else:
                target_urls = set(self._dirty_nodes)
                self._dirty_nodes.difference_update(target_urls)

            for url in target_urls:
                meta = self._node_metadata.get(url)
                if meta is None:
                    continue

                if meta.removed:
                    snapshot = (0, 0.0, -1.0, False)
                else:
                    status = self.snode.get(url)
                    if status is None:
                        continue
                    unfinished = int(status.unfinished)
                    latency_value = self._average_latency(status.latency)
                    speed_value = status.speed if status.speed is not None else (1.0 / latency_value if latency_value > 0 else -1.0)
                    available = bool(status.avaiaible)
                    snapshot = (unfinished, latency_value, speed_value, available)

                if not force and meta.last_snapshot == snapshot and not meta.removed:
                    continue

                meta.last_snapshot = snapshot
                snapshots[url] = (meta, snapshot)

        return snapshots

    def _drain_log_entries(self) -> list[_RequestLogEntry]:
        with self._log_lock:
            if not self._pending_request_logs:
                return []
            entries = list(self._pending_request_logs)
            self._pending_request_logs.clear()
            return entries

    def _flush_runtime_state(self, *, force: bool = False) -> None:
        snapshots = self._collect_state_snapshots(force=force)
        log_entries = self._drain_log_entries()

        if not snapshots and not log_entries:
            return

        if self.database_service is None or self.proxy_instance_id is None:
            return

        run_until_complete(self._flush_runtime_state_async(snapshots, log_entries))

        removed_urls = [url for url, (meta, _) in snapshots.items() if meta.removed]
        if removed_urls:
            with self._lock:
                for url in removed_urls:
                    self._node_metadata.pop(url, None)
                    self._offline_nodes.pop(url, None)

    async def _flush_runtime_state_async(
        self,
        snapshots: Dict[str, tuple[_NodeMetadata, tuple[int, float, float, bool]]],
        log_entries: list[_RequestLogEntry],
    ) -> None:
        if self.database_service is None or self.proxy_instance_id is None:
            return

        async with self.database_service.with_async_session() as session:
            status_cache: Dict[str, ProxyNodeStatus] = {}
            try:
                for url, (meta, snapshot) in snapshots.items():
                    if meta.node_id is None:
                        continue
                    unfinished, latency_value, speed_value, available = snapshot
                    status_row = await upsert_proxy_node_status(
                        session=session,
                        node_id=meta.node_id,
                        proxy_id=self.proxy_instance_id,
                        status_id=meta.status_id,
                        unfinished=int(unfinished),
                        latency=float(latency_value),
                        speed=float(speed_value),
                        avaiaible=bool(available),
                    )
                    status_cache[url] = status_row
                    meta.status_id = status_row.id

                for entry in log_entries:
                    meta = self._node_metadata.get(entry.node_url)
                    if meta is None or meta.removed or meta.node_id is None:
                        continue
                    status_row = status_cache.get(entry.node_url)
                    if status_row is None:
                        status_row = await get_or_create_proxy_node_status(
                            session=session,
                            node_id=meta.node_id,
                            proxy_id=self.proxy_instance_id,
                            status_id=meta.status_id,
                        )
                        status_cache[entry.node_url] = status_row
                        meta.status_id = status_row.id
                    await create_proxy_node_status_log_entry(
                        session=session,
                        node_id=entry.node_id,
                        proxy_id=self.proxy_instance_id,
                        status_id=status_row.id,
                        ownerapp_id=entry.ownerapp_id,
                        model_name=entry.model_name,
                        action=Action.request,
                        start_at=entry.start_at,
                        end_at=entry.end_at,
                        latency=entry.latency,
                        token_count=entry.token_count,
                    )

                await session.commit()
            except Exception:
                await session.rollback()
                raise

    def remove_stale_nodes_by_expiration(self) -> None:
        expiration_cutoff = datetime.now(tz=current_timezone()) - timedelta(seconds=CONTROLLER_HEART_BEAT_EXPIRATION)
        if self.database_service is None:
            return

        removed = run_until_complete(
            self._remove_stale_nodes_by_expiration_async(expiration_cutoff)
        )
        if removed:
            logger.info('Removed %s stale node status records older than %s seconds', removed, CONTROLLER_HEART_BEAT_EXPIRATION)

    async def _remove_stale_nodes_by_expiration_async(self, expiration_cutoff: datetime) -> int:
        if self.database_service is None:
            return 0

        async with self.database_service.with_async_session() as session:
            try:
                removed = await delete_stale_proxy_node_status(
                    session=session,
                    before=expiration_cutoff,
                    exclude_proxy_id=self.proxy_instance_id,
                )
                await session.commit()
                return removed
            except Exception:
                await session.rollback()
                raise

    async def check_request_model(self, model_name) -> Optional[JSONResponse]:
        """Check if a request is valid."""
        if model_name in self.model_list:
            return
        ret = create_error_response(
            HTTPStatus.NOT_FOUND, f'The model `{model_name}` does not exist.')
        return ret

    def handle_unavailable_model(self, model_name):
        """Handle unavailable model.

        Args:
            model_name (str): the model in the request.
        """
        logger.warning(f'no model name: {model_name}')
        ret = {
            'error_code': ErrorCodes.MODEL_NOT_FOUND,
            'text': err_msg[ErrorCodes.MODEL_NOT_FOUND],
        }
        return json.dumps(ret).encode() + b'\n'

    def handle_api_timeout(self, node_url):
        """Handle the api time out."""
        logger.warning(f'api timeout: {node_url}')
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
            for chunk in response.iter_lines(decode_unicode=False,
                                             delimiter=b'\n'):
                if chunk:
                    yield chunk + b'\n\n'
        except (Exception, GeneratorExit, requests.RequestException) as e:  # noqa
            logger.error(f'catched an exception: {e}')
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
                response = await client.post(node_url + endpoint,
                                             json=request,
                                             headers=headers,
                                             timeout=API_READ_TIMEOUT)
                return response.text
        except (Exception, GeneratorExit, requests.RequestException, asyncio.CancelledError) as e:  # noqa  # yapf: disable
            logger.error(f'catched an exception: {e}')
            return self.handle_api_timeout(node_url)

    def pre_call(
        self,
        node_url: str,
        *,
        model_name: Optional[str] = None,
        ownerapp_id: Optional[str] = None,
        token_count: Optional[int] = None,
    ) -> _RequestContext:
        """Prepare runtime bookkeeping before dispatching a request."""

        context = _RequestContext(
            start_time=time.time(),
            model_name=model_name,
            ownerapp_id=ownerapp_id,
            token_count=token_count,
        )

        with self._lock:
            status = self.nodes.get(node_url)
            if status is not None:
                status.unfinished += 1
            fallback_status = self.snode.get(node_url)
            if fallback_status is not None and fallback_status is not status:
                fallback_status.unfinished += 1

        self._mark_node_dirty(node_url)
        return context

    def post_call(self, node_url: str, start: _RequestContext | float):
        """Finalize bookkeeping after a request completes."""

        if isinstance(start, _RequestContext):
            context = start
        else:
            context = _RequestContext(start_time=float(start))

        elapsed = time.time() - context.start_time

        with self._lock:
            primary_status = self.nodes.get(node_url)
            if primary_status is not None:
                primary_status.unfinished = max(0, primary_status.unfinished - 1)
                primary_status.latency.append(elapsed)
                average_latency = self._average_latency(primary_status.latency)
                if primary_status.speed is None and average_latency > 0:
                    primary_status.speed = 1.0 / average_latency
            fallback_status = self.snode.get(node_url)
            if fallback_status is not None and fallback_status is not primary_status:
                fallback_status.unfinished = max(0, fallback_status.unfinished - 1)
                fallback_status.latency.append(elapsed)

        self._mark_node_dirty(node_url)
        self._enqueue_request_log(node_url, context, elapsed)

    def create_background_tasks(self, url: str, start: _RequestContext | float):
        """Create a background task to finalize bookkeeping for streaming responses."""
        background_tasks = BackgroundTasks()
        background_tasks.add_task(self.post_call, url, start)
        return background_tasks

    async def teardown(self) -> None:
        self._stop_event.set()
        self._state_flush_event.set()
        if getattr(self, '_state_flush_thread', None) and self._state_flush_thread.is_alive():
            self._state_flush_thread.join(timeout=1)
        if getattr(self, 'config_refresh_thread', None) and self.config_refresh_thread.is_alive():
            self.config_refresh_thread.join(timeout=1)
        if self.heart_beat_thread.is_alive():
            self.heart_beat_thread.join(timeout=1)
        try:
            self._flush_runtime_state(force=True)
        except Exception:  # noqa: BLE001
            logger.exception('Failed to flush node runtime state during teardown')
        await super().teardown()
