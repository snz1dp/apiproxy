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

from collections import deque
from typing import Optional
from openaiproxy.services.nodemanager.schemas import Status
from openaiproxy.services.nodemanager.constants import LATENCY_DEQUE_LEN

import yaml

def load_nodes_from_file(config_path: str) -> dict:
    """从配置文件加载节点信息。"""
    with open(config_path, 'r') as config_file:
        nodes = yaml.safe_load(config_file)['nodes']
        for url, status in nodes.items():
            latency = deque(
                status.get('latency', []),
                maxlen=LATENCY_DEQUE_LEN
            )
            status['latency'] = latency
            status['available'] = True
            status['health_check'] = status.get('health_check', True)
            nodes[url] = Status(**status)
    return nodes
