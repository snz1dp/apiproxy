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

from openaiproxy.services.base import Service
from openaiproxy.services.settings.base import Settings


class SettingsService(Service):
    name = "settings_service"

    def __init__(self, settings: Settings):
        super().__init__()
        self.settings: Settings = settings

    @classmethod
    def initialize(cls) -> SettingsService:
        # Check if a string is a valid path or a file name

        settings = Settings()
        if not settings.config_dir:
            msg = "CONFIG_DIR must be set in settings"
            raise ValueError(msg)
        return cls(settings)

    def set(self, key, value):
        setattr(self.settings, key, value)
        return self.settings
