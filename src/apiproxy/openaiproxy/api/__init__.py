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

from openaiproxy.api.health_check import router as health_check_router
from openaiproxy.api.openai_docs import router as openai_docs_router
from openaiproxy.api.node_manager import router as node_manager_router
from openaiproxy.api.apikey_manager import router as apikey_manager_router

from openaiproxy.api.router import v1_router as apiproxy_v1_router

__all__ = [
  "health_check_router",
  "openai_docs_router",
  "node_manager_router",
  "apikey_manager_router",
  "apiproxy_v1_router",
]
