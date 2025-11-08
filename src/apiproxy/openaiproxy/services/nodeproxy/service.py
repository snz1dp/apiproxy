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
from http import HTTPStatus
import json
import os
import random
import threading
import time
from typing import Dict, Optional
from uuid import UUID

from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse
import numpy as np
from openaiproxy.logging import logger
import requests

from openaiproxy.services.base import Service
from openaiproxy.services.database.models.node.crud import (
    select_node_models, select_nodes
)
from openaiproxy.services.database.models.proxy.crud import (
    select_proxy_node_status
)
from openaiproxy.services.nodeproxy.schemas import ErrorResponse
from openaiproxy.services.nodeproxy.constants import (
    API_READ_TIMEOUT, LATENCY_DEQUE_LEN,
    ErrorCodes, Strategy, err_msg
)
from openaiproxy.services.nodeproxy.schemas import Status
from openaiproxy.services.database.service import DatabaseService
from openaiproxy.services.database.models import ProxyNodeStatus

CONTROLLER_HEART_BEAT_EXPIRATION = int(
    os.getenv('LMDEPLOY_CONTROLLER_HEART_BEAT_EXPIRATION', 90))


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

        if self.database_service is not None:
            try:
                self._refresh_nodes_from_database(initial_load=True)
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

    def _refresh_loop(self):
        while not self._stop_event.is_set():
            try:
                self._refresh_nodes_from_database()
            except Exception:  # noqa: BLE001
                logger.exception('Failed to refresh node configuration from database')
            finally:
                if not self._stop_event.wait(self._refresh_interval or 60):
                    continue
                break

    def _refresh_nodes_from_database(self, *, initial_load: bool = False) -> None:
        if self.database_service is None:
            return

        with self._lock:
            previous_nodes = {
                url: copy.deepcopy(status) for url, status in self.snode.items()
            }

        with self.database_service.with_session() as session:
            db_nodes = select_nodes(enabled=True, session=session)
            if not db_nodes:
                new_nodes: Dict[str, Status] = {}
                new_snode: Dict[str, Status] = {}
            else:
                node_ids = [node.id for node in db_nodes if node.id is not None]
                models_map: dict[UUID, list[str]] = defaultdict(list)
                types_map: dict[UUID, set[str]] = defaultdict(set)
                if node_ids:
                    db_models = select_node_models(node_ids=node_ids, session=session)
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
                    db_statuses = select_proxy_node_status(
                        proxy_instance_ids=[self.proxy_instance_id] if self.proxy_instance_id else None,
                        node_ids=node_ids, session=session
                    )
                    for status_row in db_statuses:
                        current = status_map.get(status_row.node_id)
                        if current is None:
                            status_map[status_row.node_id] = status_row
                        elif current.updated_at and status_row.updated_at and status_row.updated_at > current.updated_at:
                            status_map[status_row.node_id] = status_row

                new_nodes = {}
                new_snode = {}
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

        with self._lock:
            prev_urls = set(self.snode.keys())
            self.snode = new_snode
            self.nodes = new_nodes

        added = set(new_snode.keys()) - prev_urls
        removed = prev_urls - set(new_snode.keys())
        if added or removed:
            logger.info(
                'Node configuration changed. Added: %s, Removed: %s',
                sorted(added),
                sorted(removed),
            )

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

    def pre_call(self, node_url):
        """Preprocess before the request get processed.

        Args:
            node_url (str): the node url.
        """
        with self._lock:
            if node_url in self.nodes:
                self.nodes[node_url].unfinished += 1
        return time.time()

    def post_call(self, node_url: str, start: int):
        """Post process after the response finished.

        Args:
            node_url (str): the node url.
            start (int): the start time point. time.time()
        """
        elapsed = time.time() - start
        with self._lock:
            if node_url in self.nodes:
                self.nodes[node_url].unfinished = max(
                    0, self.nodes[node_url].unfinished - 1)
                self.nodes[node_url].latency.append(elapsed)

    def create_background_tasks(self, url: str, start: int):
        """To create a background task.

        Args:
            node_url (str): the node url.
            start (int): the start time point. time.time()
        """
        background_tasks = BackgroundTasks()
        background_tasks.add_task(self.post_call, url, start)
        return background_tasks

    async def teardown(self) -> None:
        self._stop_event.set()
        if getattr(self, 'config_refresh_thread', None) and self.config_refresh_thread.is_alive():
            self.config_refresh_thread.join(timeout=1)
        if self.heart_beat_thread.is_alive():
            self.heart_beat_thread.join(timeout=1)
        await super().teardown()
