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

from __future__ import annotations

from typing import TYPE_CHECKING

from openaiproxy.services.database.service import DatabaseService
from openaiproxy.services.factory import ServiceFactory

if TYPE_CHECKING:
    from openaiproxy.services.settings.service import SettingsService


class DatabaseServiceFactory(ServiceFactory):
    def __init__(self) -> None:
        super().__init__(DatabaseService)

    def create(self, settings_service: SettingsService):
        # Here you would have logic to create and configure a DatabaseService
        if not settings_service.settings.database_url:
            msg = "No database URL provided"
            raise ValueError(msg)
        return DatabaseService(settings_service)
