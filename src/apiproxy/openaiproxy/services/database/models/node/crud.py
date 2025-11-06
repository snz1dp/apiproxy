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

from typing import List
from openaiproxy.utils.sqlalchemy import parse_orderby_column
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession
from openaiproxy.services.database.models.node.model import Node

async def select_nodes(
    enabled: bool | None = None,
    orderby: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    *,
    session: AsyncSession
) -> List[Node]:
    """查询所有节点"""
    smts = select(Node)
    if enabled is not None:
        smts = smts.where(Node.enabled == True)  # noqa: E712

    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)

    smts = smts.order_by(parse_orderby_column(
        Node, orderby, Node.created_at.asc()
    ))
    result = await session.exec(smts)
    return result.all()

async def count_nodes(
    enabled: bool | None = None,
    *,
    session: AsyncSession
) -> int:
    """统计节点数量"""
    smts = select(func.count(Node.id))
    if enabled is not None:
        smts = smts.where(Node.enabled == True)  # noqa: E712
    result = await session.exec(smts)
    return result.one()
