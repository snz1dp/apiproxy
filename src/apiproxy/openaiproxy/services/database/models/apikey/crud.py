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
from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from openaiproxy.services.database.models.apikey.model import ApiKey, ApiKeyQuota, ApiKeyQuotaUsage
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
    enabled: bool | None = None,
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
        smts = smts.where(ApiKey.name.ilike(f'%{name}%'))
    if enabled is not None:
        smts = smts.where(ApiKey.enabled.__eq__(enabled))
    if expired is not None:
        if expired:
            smts = smts.where(ApiKey.expires_at.__le__(func.current_timestamp()))  # noqa
        else:
            smts = smts.where(ApiKey.expires_at.__gt__(func.current_timestamp()) | ApiKey.expires_at.is_(None))  # noqa
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
    enabled: bool | None = None,
    expired: bool | None = None,
    *,
    session: AsyncSession,
) -> int:
    """统计API Keys数量"""
    smts = select(func.count(ApiKey.id))
    if ownerapp_id is not None:
        smts = smts.where(ApiKey.ownerapp_id == ownerapp_id)
    if name is not None:
        smts = smts.where(ApiKey.name.ilike(f'%{name}%'))
    if enabled is not None:
        smts = smts.where(ApiKey.enabled == enabled)
    if expired is not None:
        if expired:
            smts = smts.where(ApiKey.expires_at.__le__(func.current_timestamp()))  # noqa
        else:
            smts = smts.where(ApiKey.expires_at.__gt__(func.current_timestamp()) | ApiKey.expires_at.is_(None))  # noqa
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
        ApiKey.key.is_not(None),
        ApiKey.key == encrypted_key,
    )
    result = await session.exec(smts)
    return result.first()


async def select_apikey_by_hash(
    ownerapp_id: str,
    key_hash: str,
    *,
    session: AsyncSession,
):
    """通过 ownerapp_id + key_hash 选择 API Key。"""

    smts = select(ApiKey).where(
        ApiKey.ownerapp_id == ownerapp_id,
        ApiKey.key_hash == key_hash,
    )
    result = await session.exec(smts)
    return result.first()


async def create_apikey_record(
    *,
    session: AsyncSession,
    apikey_payload: dict[str, Any],
) -> ApiKey:
    """创建 API Key 并刷新返回。"""
    api_key = ApiKey.model_validate(apikey_payload)
    session.add(api_key)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise
    await session.refresh(api_key)
    return api_key


async def update_apikey_record(
    *,
    session: AsyncSession,
    api_key: ApiKey,
    update_payload: dict[str, Any],
) -> ApiKey:
    """更新 API Key 并刷新返回。"""
    for field, value in update_payload.items():
        setattr(api_key, field, value)
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return api_key


async def delete_apikey_record(
    *,
    session: AsyncSession,
    api_key: ApiKey,
) -> None:
    """删除 API Key。"""
    await session.delete(api_key)
    await session.commit()


# ── ApiKeyQuota CRUD ──────────────────────────────────────────────

async def select_apikey_quota_by_id(
    id: UUID,
    *,
    session: AsyncSession,
) -> Optional[ApiKeyQuota]:
    """通过ID查询 API 密钥配额。"""
    smts = select(ApiKeyQuota).where(ApiKeyQuota.id == id)
    result = await session.exec(smts)
    return result.first()


async def create_apikey_quota_record(
    *,
    session: AsyncSession,
    quota_payload: dict[str, Any],
) -> ApiKeyQuota:
    """创建 API 密钥配额并刷新返回。"""
    quota = ApiKeyQuota.model_validate(quota_payload)
    session.add(quota)
    await session.commit()
    await session.refresh(quota)
    return quota


async def update_apikey_quota_record(
    *,
    session: AsyncSession,
    quota: ApiKeyQuota,
    update_payload: dict[str, Any],
    updated_at: datetime,
) -> ApiKeyQuota:
    """更新 API 密钥配额并刷新返回。"""
    for field, value in update_payload.items():
        setattr(quota, field, value)
    quota.updated_at = updated_at
    session.add(quota)
    await session.commit()
    await session.refresh(quota)
    return quota


async def expire_apikey_quota_record(
    *,
    session: AsyncSession,
    quota: ApiKeyQuota,
    expired_at: datetime,
) -> ApiKeyQuota:
    """软删除 API 密钥配额并刷新返回。"""
    quota.expired_at = quota.expired_at or expired_at
    quota.updated_at = expired_at
    session.add(quota)
    await session.commit()
    await session.refresh(quota)
    return quota


async def select_apikey_quota_by_unique(
    *,
    api_key_id: UUID,
    order_id: Optional[str],
    session: AsyncSession,
) -> Optional[ApiKeyQuota]:
    """通过唯一键 (api_key_id, order_id) 查询配额。"""
    smts = select(ApiKeyQuota).where(ApiKeyQuota.api_key_id == api_key_id)
    if order_id is None:
        smts = smts.where(ApiKeyQuota.order_id.is_(None))
    else:
        smts = smts.where(ApiKeyQuota.order_id == order_id)
    result = await session.exec(smts)
    return result.first()


async def select_apikey_quotas(
    api_key_ids: list[UUID] | None = None,
    order_id: Optional[str] = None,
    expired: Optional[bool] = None,
    orderby: Optional[str] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    *,
    session: AsyncSession,
) -> List[ApiKeyQuota]:
    """分页查询 API 密钥配额列表。"""
    smts = select(ApiKeyQuota)
    if api_key_ids is not None:
        smts = smts.where(ApiKeyQuota.api_key_id.in_(api_key_ids))
    if order_id is not None:
        smts = smts.where(ApiKeyQuota.order_id == order_id)
    if expired is not None:
        if expired:
            smts = smts.where(ApiKeyQuota.expired_at.__le__(func.current_timestamp()))
        else:
            smts = smts.where(
                ApiKeyQuota.expired_at.__gt__(func.current_timestamp()) | ApiKeyQuota.expired_at.is_(None)
            )
    if orderby is not None:
        orderby_column = parse_orderby_column(ApiKeyQuota, orderby)
        if orderby_column is not None:
            smts = smts.order_by(orderby_column)
    else:
        smts = smts.order_by(ApiKeyQuota.created_at.desc())
    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)
    result = await session.exec(smts)
    return result.all()


async def count_apikey_quotas(
    api_key_ids: list[UUID] | None = None,
    order_id: Optional[str] = None,
    expired: Optional[bool] = None,
    *,
    session: AsyncSession,
):
    """统计 API 密钥配额数量。"""
    smts = select(func.count(ApiKeyQuota.id))
    if api_key_ids is not None:
        smts = smts.where(ApiKeyQuota.api_key_id.in_(api_key_ids))
    if order_id is not None:
        smts = smts.where(ApiKeyQuota.order_id == order_id)
    if expired is not None:
        if expired:
            smts = smts.where(ApiKeyQuota.expired_at.__le__(func.current_timestamp()))
        else:
            smts = smts.where(
                ApiKeyQuota.expired_at.__gt__(func.current_timestamp()) | ApiKeyQuota.expired_at.is_(None)
            )
    result = await session.exec(smts)
    return result.one()


async def select_apikey_quota_usages(
    quota_ids: list[UUID] | None = None,
    api_key_ids: list[UUID] | None = None,
    ownerapp_id: Optional[str] = None,
    request_action: Optional[str] = None,
    orderby: Optional[str] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    *,
    session: AsyncSession,
) -> List[ApiKeyQuotaUsage]:
    """分页查询 API 密钥配额使用记录。"""
    smts = select(ApiKeyQuotaUsage)
    if quota_ids is not None:
        smts = smts.where(ApiKeyQuotaUsage.quota_id.in_(quota_ids))
    if api_key_ids is not None:
        smts = smts.where(ApiKeyQuotaUsage.api_key_id.in_(api_key_ids))
    if ownerapp_id is not None:
        smts = smts.where(ApiKeyQuotaUsage.ownerapp_id == ownerapp_id)
    if request_action is not None:
        smts = smts.where(ApiKeyQuotaUsage.request_action == request_action)
    if orderby is not None:
        orderby_column = parse_orderby_column(ApiKeyQuotaUsage, orderby)
        if orderby_column is not None:
            smts = smts.order_by(orderby_column)
    else:
        smts = smts.order_by(ApiKeyQuotaUsage.created_at.desc())
    if offset is not None:
        smts = smts.offset(offset)
    if limit is not None:
        smts = smts.limit(limit)
    result = await session.exec(smts)
    return result.all()


async def count_apikey_quota_usages(
    quota_ids: list[UUID] | None = None,
    api_key_ids: list[UUID] | None = None,
    ownerapp_id: Optional[str] = None,
    request_action: Optional[str] = None,
    *,
    session: AsyncSession,
):
    """统计 API 密钥配额使用记录数量。"""
    smts = select(func.count(ApiKeyQuotaUsage.id))
    if quota_ids is not None:
        smts = smts.where(ApiKeyQuotaUsage.quota_id.in_(quota_ids))
    if api_key_ids is not None:
        smts = smts.where(ApiKeyQuotaUsage.api_key_id.in_(api_key_ids))
    if ownerapp_id is not None:
        smts = smts.where(ApiKeyQuotaUsage.ownerapp_id == ownerapp_id)
    if request_action is not None:
        smts = smts.where(ApiKeyQuotaUsage.request_action == request_action)
    result = await session.exec(smts)
    return result.one()
