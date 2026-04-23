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

"""应用配额 CRUD 查询函数。"""

from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID

from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from openaiproxy.services.database.models.app.model import AppQuota, AppQuotaUsage
from openaiproxy.utils.sqlalchemy import parse_orderby_column


async def select_app_quota_by_id(
    id: UUID,
    *,
    session: AsyncSession,
) -> Optional[AppQuota]:
    """通过ID查询应用配额。"""
    smts = select(AppQuota).where(AppQuota.id == id)
    result = await session.exec(smts)
    return result.first()


async def create_app_quota_record(
    *,
    session: AsyncSession,
    quota_payload: dict[str, Any],
) -> AppQuota:
    """创建应用配额并刷新返回。"""
    quota = AppQuota.model_validate(quota_payload)
    session.add(quota)
    await session.commit()
    await session.refresh(quota)
    return quota


async def update_app_quota_record(
    *,
    session: AsyncSession,
    quota: AppQuota,
    update_payload: dict[str, Any],
    updated_at: datetime,
) -> AppQuota:
    """更新应用配额并刷新返回。"""
    for field, value in update_payload.items():
        setattr(quota, field, value)
    quota.updated_at = updated_at
    session.add(quota)
    await session.commit()
    await session.refresh(quota)
    return quota


async def expire_app_quota_record(
    *,
    session: AsyncSession,
    quota: AppQuota,
    expired_at: datetime,
) -> AppQuota:
    """软删除应用配额并刷新返回。"""
    quota.expired_at = quota.expired_at or expired_at
    quota.updated_at = expired_at
    session.add(quota)
    await session.commit()
    await session.refresh(quota)
    return quota


async def select_app_quota_by_unique(
    *,
    ownerapp_id: str,
    order_id: Optional[str],
    session: AsyncSession,
) -> Optional[AppQuota]:
    """通过唯一键 (ownerapp_id, order_id) 查询配额。"""
    smts = select(AppQuota).where(AppQuota.ownerapp_id == ownerapp_id)
    if order_id is None:
        smts = smts.where(AppQuota.order_id.is_(None))
    else:
        smts = smts.where(AppQuota.order_id == order_id)
    result = await session.exec(smts)
    return result.first()


async def select_app_quotas(
    ownerapp_ids: list[str] | None = None,
    order_id: Optional[str] = None,
    expired: Optional[bool] = None,
    orderby: Optional[str] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    *,
    session: AsyncSession,
) -> List[AppQuota]:
    """分页查询应用配额列表。"""
    smts = select(AppQuota)
    if ownerapp_ids is not None:
        smts = smts.where(AppQuota.ownerapp_id.in_(ownerapp_ids))
    if order_id is not None:
        smts = smts.where(AppQuota.order_id == order_id)
    if expired is not None:
        if expired:
            smts = smts.where(AppQuota.expired_at.__le__(func.current_timestamp()))
        else:
            smts = smts.where(
                AppQuota.expired_at.__gt__(func.current_timestamp()) | AppQuota.expired_at.is_(None)
            )
    if orderby is not None:
        orderby_column = parse_orderby_column(AppQuota, orderby)
        if orderby_column is not None:
            smts = smts.order_by(orderby_column)
    else:
        smts = smts.order_by(AppQuota.created_at.desc())
    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)
    result = await session.exec(smts)
    return result.all()


async def count_app_quotas(
    ownerapp_ids: list[str] | None = None,
    order_id: Optional[str] = None,
    expired: Optional[bool] = None,
    *,
    session: AsyncSession,
):
    """统计应用配额数量。"""
    smts = select(func.count(AppQuota.id))
    if ownerapp_ids is not None:
        smts = smts.where(AppQuota.ownerapp_id.in_(ownerapp_ids))
    if order_id is not None:
        smts = smts.where(AppQuota.order_id == order_id)
    if expired is not None:
        if expired:
            smts = smts.where(AppQuota.expired_at.__le__(func.current_timestamp()))
        else:
            smts = smts.where(
                AppQuota.expired_at.__gt__(func.current_timestamp()) | AppQuota.expired_at.is_(None)
            )
    result = await session.exec(smts)
    return result.one()


async def select_app_quota_usages(
    quota_ids: list[UUID] | None = None,
    ownerapp_ids: list[str] | None = None,
    api_key_id: Optional[UUID] = None,
    request_action: Optional[str] = None,
    orderby: Optional[str] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    *,
    session: AsyncSession,
) -> List[AppQuotaUsage]:
    """分页查询应用配额使用记录。"""
    smts = select(AppQuotaUsage)
    if quota_ids is not None:
        smts = smts.where(AppQuotaUsage.quota_id.in_(quota_ids))
    if ownerapp_ids is not None:
        smts = smts.where(AppQuotaUsage.ownerapp_id.in_(ownerapp_ids))
    if api_key_id is not None:
        smts = smts.where(AppQuotaUsage.api_key_id == api_key_id)
    if request_action is not None:
        smts = smts.where(AppQuotaUsage.request_action == request_action)
    if orderby is not None:
        orderby_column = parse_orderby_column(AppQuotaUsage, orderby)
        if orderby_column is not None:
            smts = smts.order_by(orderby_column)
    else:
        smts = smts.order_by(AppQuotaUsage.created_at.desc())
    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)
    result = await session.exec(smts)
    return result.all()


async def count_app_quota_usages(
    quota_ids: list[UUID] | None = None,
    ownerapp_ids: list[str] | None = None,
    api_key_id: Optional[UUID] = None,
    request_action: Optional[str] = None,
    *,
    session: AsyncSession,
):
    """统计应用配额使用记录数量。"""
    smts = select(func.count(AppQuotaUsage.id))
    if quota_ids is not None:
        smts = smts.where(AppQuotaUsage.quota_id.in_(quota_ids))
    if ownerapp_ids is not None:
        smts = smts.where(AppQuotaUsage.ownerapp_id.in_(ownerapp_ids))
    if api_key_id is not None:
        smts = smts.where(AppQuotaUsage.api_key_id == api_key_id)
    if request_action is not None:
        smts = smts.where(AppQuotaUsage.request_action == request_action)
    result = await session.exec(smts)
    return result.one()
