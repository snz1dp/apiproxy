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

from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from alembic.util.exc import CommandError
from openaiproxy.logging import logger
from sqlmodel import Session, text
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
        session = Session(db_service.engine)
        yield session
    except Exception:
        logger.exception("Session rollback because of exception")
        session.rollback()
        raise
    finally:
        session.close()


@asynccontextmanager
async def async_session_getter(db_service: DatabaseService):
    try:
        session = AsyncSession(db_service.async_engine)
        yield session
    except Exception:
        logger.exception("Session rollback because of exception")
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
