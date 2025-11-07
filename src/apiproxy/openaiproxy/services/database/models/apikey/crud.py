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
from uuid import UUID

from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from openaiproxy.services.database.models.apikey.model import ApiKey
from openaiproxy.utils.apikey import encrypt_api_key
from openaiproxy.utils.sqlalchemy import parse_orderby_column

async def select_apikey_by_id(
    id: UUID,
    *,
    session: AsyncSession,
):
    """通过ID选择API Key"""
    smts = select(ApiKey).where(ApiKey.id == id)
    result = await session.exec(smts)
    return result.first()

async def select_apikeys(
    name: str | None = None,
    ownerapp_id: str | None = None,
    expired: bool | None = None,
    orderby: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    *,
    session: AsyncSession,
) -> List[ApiKey]:
    """选择API Keys列表"""
    smts = select(ApiKey)
    if ownerapp_id is not None:
        smts = smts.where(ApiKey.ownerapp_id == ownerapp_id)
    if name is not None:
        smts = smts.where(ApiKey.name.__eq__(name))
    if expired is not None:
        if expired:
            smts = smts.where(ApiKey.expires_at.__le__(func.current_timestamp()))  # noqa
        else:
            smts = smts.where(ApiKey.expires_at.__gt__(func.current_timestamp()))  # noqa
    if orderby is not None:
        orderby_column = parse_orderby_column(ApiKey, orderby)
        if orderby_column is not None:
            smts = smts.order_by(orderby_column)
    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)
    result = await session.exec(smts)
    return result.all()

async def count_apikeys(
    name: str | None = None,
    ownerapp_id: str | None = None,
    expired: bool | None = None,
    *,
    session: AsyncSession,
) -> int:
    """统计API Keys数量"""
    smts = select(func.count(ApiKey.id))
    if ownerapp_id is not None:
        smts = smts.where(ApiKey.ownerapp_id == ownerapp_id)
    if name is not None:
        smts = smts.where(ApiKey.name.__eq__(name))
    if expired is not None:
        if expired:
            smts = smts.where(ApiKey.expires_at.__le__(func.current_timestamp()))  # noqa
        else:
            smts = smts.where(ApiKey.expires_at.__gt__(func.current_timestamp()))  # noqa
    result = await session.exec(smts)
    return result.one()


async def select_apikey_by_key(
    ownerapp_id: str,
    key: str,
    *,
    session: AsyncSession,
):
    """通过Key选择API Key"""
    encrypted_key = encrypt_api_key(key)
    smts = select(ApiKey).where(
        ApiKey.ownerapp_id == ownerapp_id,
        ApiKey.key == encrypted_key,
    )
    result = await session.exec(smts)
    return result.first()
