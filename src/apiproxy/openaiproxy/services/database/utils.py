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

import os
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from alembic.util.exc import CommandError
from openaiproxy.logging import logger
from openaiproxy.services.database.models.apikey.utils import (
    finalize_apikey_quota_usage,
    reserve_apikey_quota,
    rollback_apikey_quota_usage,
)
from openaiproxy.services.database.models.app.utils import (
    finalize_app_quota_usage,
    reserve_app_quota,
    rollback_app_quota_usage,
)
from openaiproxy.services.database.models.proxy.model import RequestAction
from sqlmodel import func, select, text
from sqlmodel.ext.asyncio.session import AsyncSession
import asyncio

if TYPE_CHECKING:
    from openaiproxy.services.database.service import DatabaseService
    from openaiproxy.services.settings.service import SettingsService

def initialize_database(*, fix_migration: bool = False) -> None:
    logger.debug("初始化数据库...")
    from openaiproxy.services.deps import get_db_service, get_settings_service

    settings_service: SettingsService = get_settings_service()
    database_service: DatabaseService = get_db_service()
    try:
        database_service.create_db_and_tables()
    except Exception as exc:
        # if the exception involves tables already existing
        # we can ignore it
        if "already exists" not in str(exc):
            msg = "创建数据数据结构失败"
            logger.exception(msg)
            raise RuntimeError(msg) from exc
    try:
        database_service.check_schema_health()
    except Exception as exc:
        msg = "检查数据结构失败"
        logger.exception(msg)
        raise RuntimeError(msg) from exc
    try:
        database_service.run_migrations(fix=fix_migration)
    except CommandError as exc:
        # if "overlaps with other requested revisions" or "Can't locate revision identified by"
        # are not in the exception, we can't handle it
        if "overlaps with other requested revisions" not in str(
            exc
        ) and "Can't locate revision identified by" not in str(exc):
            raise
        # This means there's wrong revision in the DB
        # We need to delete the alembic_version table
        # and run the migrations again
        logger.warning("数据库中存在错误的修订版本，准备删除alembic_version表并重新运行升级")
        with session_getter(database_service) as session:
            session.exec(text(f"DROP TABLE apiproxy_alembic_version"))
        database_service.run_migrations(fix=fix_migration)
    except Exception as exc:
        # if the exception involves tables already existing
        # we can ignore it
        if "already exists" not in str(exc):
            logger.exception(exc)
        raise
    logger.debug("数据库已初始化")

@contextmanager
def session_getter(db_service: DatabaseService):
    try:
        session = db_service.create_session()
        yield session
    except Exception:
        logger.exception("因异常回滚会话")
        session.rollback()
        raise
    finally:
        session.close()


@asynccontextmanager
async def async_session_getter(db_service: DatabaseService):
    try:
        session = db_service.create_async_session()
        yield session
    except Exception:
        logger.exception("因异常回滚异步会话")
        await session.rollback()
        raise
    finally:
        await session.close()


@dataclass
class Result:
    name: str
    type: str
    success: bool


@dataclass
class TableResults:
    table_name: str
    results: list[Result]

async def get_db_process_id(session: AsyncSession):
    """获取数据库进程 ID"""
    if session.bind is None or session.bind.dialect.name == "sqlite":
        return str(os.getpid())

    smts = select(func.pg_backend_pid())
    result = await session.exec(smts)
    return result.first()


async def reserve_northbound_quotas_transactionally(
    *,
    api_key_id: Optional[UUID],
    ownerapp_id: Optional[str],
    proxy_id: Optional[UUID],
    model_name: Optional[str],
    request_action: RequestAction | str | None,
    estimated_total_tokens: Optional[int],
) -> tuple[Optional[tuple[UUID, UUID]], Optional[tuple[UUID, UUID]]]:
    """在数据库事务中预占北向 API Key / 应用配额。"""

    from openaiproxy.services.deps import async_session_scope

    async with async_session_scope() as session:
        apikey_result: Optional[tuple[UUID, UUID]] = None
        app_result: Optional[tuple[UUID, UUID]] = None

        if api_key_id is not None:
            apikey_result = await reserve_apikey_quota(
                session=session,
                api_key_id=api_key_id,
                proxy_id=proxy_id,
                ownerapp_id=ownerapp_id,
                model_name=model_name,
                request_action=request_action,
                estimated_total_tokens=estimated_total_tokens,
            )

        if ownerapp_id:
            app_result = await reserve_app_quota(
                session=session,
                ownerapp_id=ownerapp_id,
                api_key_id=api_key_id,
                proxy_id=proxy_id,
                model_name=model_name,
                request_action=request_action,
                estimated_total_tokens=estimated_total_tokens,
            )

        return apikey_result, app_result


async def rollback_northbound_quotas_transactionally(
    *,
    apikey_quota_id: Optional[UUID],
    apikey_usage_id: Optional[UUID],
    app_quota_id: Optional[UUID],
    app_usage_id: Optional[UUID],
) -> None:
    """在数据库事务中回滚北向 API Key / 应用配额预占。"""

    from openaiproxy.services.deps import async_session_scope

    async with async_session_scope() as session:
        if apikey_quota_id is not None:
            await rollback_apikey_quota_usage(
                session=session,
                quota_id=apikey_quota_id,
                usage_id=apikey_usage_id,
            )

        if app_quota_id is not None:
            await rollback_app_quota_usage(
                session=session,
                quota_id=app_quota_id,
                usage_id=app_usage_id,
            )


async def finalize_northbound_quotas_transactionally(
    *,
    api_key_id: Optional[UUID],
    ownerapp_id: Optional[str],
    apikey_quota_id: Optional[UUID],
    apikey_usage_id: Optional[UUID],
    app_quota_id: Optional[UUID],
    app_usage_id: Optional[UUID],
    total_tokens: int,
    model_name: Optional[str],
    request_action: RequestAction | str | None,
    log_id: Optional[UUID],
) -> None:
    """在数据库事务中结算北向 API Key / 应用配额。"""

    from openaiproxy.services.deps import async_session_scope

    if api_key_id is not None and apikey_quota_id is not None and apikey_usage_id is not None:
        async with async_session_scope() as session:
            await finalize_apikey_quota_usage(
                session=session,
                api_key_id=api_key_id,
                primary_quota_id=apikey_quota_id,
                primary_quota_usage_id=apikey_usage_id,
                total_tokens=total_tokens,
                ownerapp_id=ownerapp_id,
                model_name=model_name,
                request_action=request_action,
                log_id=log_id,
            )

    if ownerapp_id and app_quota_id is not None and app_usage_id is not None:
        async with async_session_scope() as session:
            await finalize_app_quota_usage(
                session=session,
                ownerapp_id=ownerapp_id,
                primary_quota_id=app_quota_id,
                primary_quota_usage_id=app_usage_id,
                total_tokens=total_tokens,
                api_key_id=api_key_id,
                model_name=model_name,
                request_action=request_action,
                log_id=log_id,
            )
