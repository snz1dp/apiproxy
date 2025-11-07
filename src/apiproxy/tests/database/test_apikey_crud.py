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

from sqlmodel.ext.asyncio.session import AsyncSession

async def test_count_apikeys(session: AsyncSession):
    from openaiproxy.services.database.models.apikey.crud import count_apikeys
    total = await count_apikeys(session=session)
    assert isinstance(total, int)

async def test_select_apikeys(session: AsyncSession):
    from openaiproxy.services.database.models.apikey.crud import select_apikeys
    from openaiproxy.services.database.models.apikey.model import ApiKey
    apikeys = await select_apikeys(session=session)
    assert isinstance(apikeys, list)
    for apikey in apikeys:
        assert isinstance(apikey, ApiKey)
