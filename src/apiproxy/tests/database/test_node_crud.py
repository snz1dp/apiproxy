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

async def test_select_nodes(session: AsyncSession):
    from openaiproxy.services.database.models.node.crud import select_nodes
    from openaiproxy.services.database.models.node.model import Node
    nodes = await select_nodes(session=session)
    assert isinstance(nodes, list)
    for node in nodes:
        assert isinstance(node, Node)

async def test_count_nodes(session: AsyncSession):
    from openaiproxy.services.database.models.node.crud import count_nodes
    total = await count_nodes(session=session)
    assert isinstance(total, int)
