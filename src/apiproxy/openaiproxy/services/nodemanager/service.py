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
from collections import deque
import copy
from http import HTTPStatus
import json
import os
import os.path as path
import random
import threading
import time
from typing import Dict, Optional

from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse
import numpy as np
from openaiproxy.logging import logger

from openaiproxy.services.base import Service
from openaiproxy.services.nodemanager.schemas import ErrorResponse
from openaiproxy.services.nodemanager.constants import (
    API_READ_TIMEOUT, LATENCY_DEQUE_LEN,
    ErrorCodes, Strategy, err_msg
)
from openaiproxy.services.nodemanager.schemas import Status
import requests
import yaml


CONTROLLER_HEART_BEAT_EXPIRATION = int(
    os.getenv('LMDEPLOY_CONTROLLER_HEART_BEAT_EXPIRATION', 90))


def heart_beat_controller(proxy_controller):
    while True:
        time.sleep(CONTROLLER_HEART_BEAT_EXPIRATION)
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


class NodeManager(Service):

    name = "nodemanager_service"

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
        config_path: Optional[str] = None,
        strategy: str = 'min_expected_latency'
    ) -> None:
        self.nodes = dict()
        self.snode = dict()
        self.strategy = Strategy.from_str(strategy)
        self.latencies = dict()
        self.config_path = path.join(
            path.dirname(path.realpath(__file__)),
            'proxy_config.yml'
        )
        if config_path is not None:
            self.config_path = config_path

        if path.exists(self.config_path):
            with open(self.config_path, 'r') as config_file:
                self.nodes = yaml.safe_load(config_file)['nodes']
                self.snode = copy.deepcopy(self.nodes)
                for url, status in self.nodes.items():
                    latency = deque(status.get('latency', []),
                                    maxlen=LATENCY_DEQUE_LEN)
                    status['latency'] = latency
                    status['available'] = True
                    status['health_check'] = status.get('health_check', True)
                    self.nodes[url] = Status(**status)
                    self.snode[url] = Status(**status)

        self.heart_beat_thread = threading.Thread(
            target=heart_beat_controller,
            args=(self, ),
            daemon=True
        )
        self.heart_beat_thread.start()

    def update_config_file(self):
        """Update the config file."""
        nodes = copy.deepcopy(self.snode)
        for url, status in nodes.items():
            nodes[url] = status.model_dump()
            nodes[url]['latency'] = list(status.latency)[-LATENCY_DEQUE_LEN:]
        with open(self.config_path, 'w') as config_file:  # update cfg yml
            yaml.dump(dict(nodes=nodes), config_file)

    def add(self, node_url: str, status: Optional[Status] = None):
        """Add a node to the manager.

        Args:
            node_url (str): A http url. Can be the url generated by
                `lmdeploy serve api_server`.
            description (Dict): The description of the node. An example:
                {'http://0.0.0.0:23333': {models: ['internlm-chat-7b]},
                speed: -1}. The speed here can be RPM or other metric. All the
                values of nodes should be the same metric.
        """
        if status is None:
            status = self.snode.get(node_url, Status())
        if status.models != []:  # force register directly
            self.nodes[node_url] = status
            if node_url not in self.snode.keys():
                self.snode[node_url] = status
            self.update_config_file()
            return
        try:
            from openaiproxy.utils.api_client import APIClient
            client = APIClient(api_server_url=node_url, api_key=status.api_key)
            status.models = client.available_models
            self.nodes[node_url] = status
            if node_url not in self.snode.keys():
                self.snode[node_url] = status
        except requests.exceptions.RequestException as e:  # noqa
            return self.handle_api_timeout(node_url)
        self.update_config_file()

    def remove(self, node_url: str):
        """Remove a node."""
        if node_url in self.nodes.keys():
            self.snode[node_url] = self.nodes.pop(node_url)
            self.update_config_file()

    def remove_stale_nodes_by_expiration(self):
        try:
            """remove stale nodes."""
            to_be_deleted = []
            to_be_append = []
            for node_url in self.snode.keys():
                node_status = self.snode[node_url]
                if node_status.health_check is not None and not node_status.health_check:
                    continue
                url = f'{node_url}/v1/models'
                headers = {'accept': 'application/json'}
                try:
                    response = requests.get(url, headers=headers)
                    if response.status_code != 200:
                        to_be_deleted.append(node_url)
                    elif node_url not in self.nodes.keys():
                        to_be_append.append(node_url)
                except:  # noqa
                    to_be_deleted.append(node_url)
            for node_url in to_be_deleted:
                self.remove(node_url)
                logger.info(f'Removed node_url: {node_url} '
                            'due to heart beat expiration')
            for node_url in to_be_append:
                self.add(node_url, self.snode[node_url])
                logger.info(f'Added node_url: {node_url} '
                            'avaiaible to heart beat expiration')
        except:  # noqa
            logger.error('Failed to remove stale nodes by expiration')

    @property
    def model_list(self):
        """Supported model list."""
        model_names = []
        for node_url, node_status in self.snode.items():
            model_names.extend(node_status.models)
        return model_names

    @property
    def status(self):
        """Return the status."""
        noderet = dict()
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
            all_matched_urls, all_the_speeds = get_matched_urls()
            if len(all_matched_urls) == 0:
                return None
            speed_sum = sum(all_the_speeds)
            weights = [speed / speed_sum for speed in all_the_speeds]
            index = random.choices(range(len(all_matched_urls)),
                                   weights=weights)[0]
            url = all_matched_urls[index]
            return url
        elif self.strategy == Strategy.MIN_EXPECTED_LATENCY:
            all_matched_urls, all_the_speeds = get_matched_urls()
            if len(all_matched_urls) == 0:
                return None
            min_latency = float('inf')
            min_index = 0
            # random traverse nodes for low concurrency situation
            all_indexes = [i for i in range(len(all_the_speeds))]
            random.shuffle(all_indexes)
            for index in all_indexes:
                latency = self.nodes[
                    all_matched_urls[index]].unfinished / all_the_speeds[index]
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
            index = np.argmin(np.array(latencies))
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
        self.nodes[node_url].unfinished += 1
        return time.time()

    def post_call(self, node_url: str, start: int):
        """Post process after the response finished.

        Args:
            node_url (str): the node url.
            start (int): the start time point. time.time()
        """
        self.nodes[node_url].unfinished -= 1
        self.nodes[node_url].latency.append(time.time() - start)

    def create_background_tasks(self, url: str, start: int):
        """To create a background task.

        Args:
            node_url (str): the node url.
            start (int): the start time point. time.time()
        """
        background_tasks = BackgroundTasks()
        background_tasks.add_task(self.post_call, url, start)
        return background_tasks
