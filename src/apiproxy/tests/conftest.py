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

import pytest
from openaiproxy.services.utils import initialize_services
from openaiproxy.utils.async_helpers import run_until_complete
from typing import AsyncGenerator
from sqlmodel.ext.asyncio.session import AsyncSession
from openaiproxy.services.deps import get_db_service
from openaiproxy.services.database.service import DatabaseService

@pytest.fixture(scope="session", autouse=True)
def initialize_test():
    run_until_complete(initialize_services())

@pytest.fixture(scope="function")
async def session() -> AsyncGenerator[AsyncSession, None]:
    db_service: DatabaseService = get_db_service()
    async with db_service.with_async_session() as session:
        yield session
