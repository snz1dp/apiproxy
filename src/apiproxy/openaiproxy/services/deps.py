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
from typing import TYPE_CHECKING

from openaiproxy.logging import logger

from openaiproxy.services.schema import ServiceType

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

    from sqlmodel import Session
    from sqlmodel.ext.asyncio.session import AsyncSession

    from openaiproxy.services.database.service import DatabaseService
    from openaiproxy.services.settings.service import SettingsService
    from openaiproxy.services.nodeproxy.service import NodeProxyService

def get_service(service_type: ServiceType, default=None):
    """Retrieves the service instance for the given service type.

    Args:
        service_type (ServiceType): The type of service to retrieve.
        default (ServiceFactory, optional): The default ServiceFactory to use if the service is not found.
            Defaults to None.

    Returns:
        Any: The service instance.

    """
    from openaiproxy.services.manager import service_manager

    if not service_manager.factories:
        # ! This is a workaround to ensure that the service manager is initialized
        # ! Not optimal, but it works for now
        service_manager.register_factories()
    return service_manager.get(service_type, default)

def get_settings_service() -> SettingsService:
    """Retrieves the SettingsService instance.

    If the service is not yet initialized, it will be initialized before returning.

    Returns:
        The SettingsService instance.

    Raises:
        ValueError: If the service cannot be retrieved or initialized.
    """
    from openaiproxy.services.settings.factory import SettingsServiceFactory

    return get_service(ServiceType.SETTINGS_SERVICE, SettingsServiceFactory())


def get_db_service() -> DatabaseService:
    """Retrieves the DatabaseService instance from the service manager.

    Returns:
        The DatabaseService instance.

    """
    from openaiproxy.services.database.factory import DatabaseServiceFactory

    return get_service(ServiceType.DATABASE_SERVICE, DatabaseServiceFactory())

def get_node_proxy_service() -> NodeProxyService:
    """Retrieves the NodeManager instance from the service manager.

    Returns:
        The NodeManager instance.

    """
    from openaiproxy.services.nodeproxy.factory import NodeProxyServiceFactory

    return get_service(ServiceType.NODEPROXY_SERVICE, NodeProxyServiceFactory())

def get_session() -> Generator[Session, None, None]:
    """Retrieves a session from the database service.

    Yields:
        Session: A session object.

    """
    with get_db_service().with_session() as session:
        yield session


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Retrieves an async session from the database service.

    Yields:
        Session: An async session object.

    """
    async with get_db_service().with_async_session() as session:
        yield session


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Context manager for managing a session scope.

    This context manager is used to manage a session scope for database operations.
    It ensures that the session is properly committed if no exceptions occur,
    and rolled back if an exception is raised.

    Yields:
        session: The session object.

    Raises:
        Exception: If an error occurs during the session scope.

    """
    db_service = get_db_service()
    with db_service.with_session() as session:
        try:
            yield session
            session.commit()
        except Exception:
            logger.exception("会话作用域执行过程中发生异常")
            session.rollback()
            raise


@asynccontextmanager
async def async_session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for managing an async session scope.

    This context manager is used to manage an async session scope for database operations.
    It ensures that the session is properly committed if no exceptions occur,
    and rolled back if an exception is raised.

    Yields:
        session: The async session object.

    Raises:
        Exception: If an error occurs during the session scope.

    """
    db_service = get_db_service()
    async with db_service.with_async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            logger.exception("异步会话作用域执行过程中发生异常")
            await session.rollback()
            raise
