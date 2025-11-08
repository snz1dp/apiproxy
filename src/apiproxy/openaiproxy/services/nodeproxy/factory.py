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

from openaiproxy.services.factory import ServiceFactory
from openaiproxy.services.nodeproxy.service import NodeProxyService
from openaiproxy.services.database.service import DatabaseService
from openaiproxy.services.settings.service import SettingsService

class NodeProxyServiceFactory(ServiceFactory):
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        super().__init__(NodeProxyService)

    def create(self, settings_service: SettingsService, database_service: DatabaseService):
        from os import getenv
        settings = settings_service.settings
        strategy = settings.proxy_strategy
        refresh_interval = settings.refresh_interval
        proxy_instance_id = settings.instance_id

        return NodeProxyService(
            strategy=strategy,
            database_service=database_service,
            refresh_interval=refresh_interval,
            proxy_instance_id=proxy_instance_id,
        )
