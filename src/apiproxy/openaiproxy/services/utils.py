from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from openaiproxy.logging import logger
from sqlalchemy import delete
from sqlalchemy import exc as sqlalchemy_exc
from sqlmodel import col, select

from openaiproxy.services.database.utils import initialize_database
from openaiproxy.services.schema import ServiceType

from .deps import get_db_service, get_service, get_settings_service

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from openaiproxy.services.settings.manager import SettingsService

async def teardown_superuser(settings_service, session: AsyncSession) -> None:
    """Teardown the superuser."""
    pass

async def teardown_services() -> None:
    """Teardown all the services."""
    try:
        async with get_db_service().with_async_session() as session:
            await teardown_superuser(get_settings_service(), session)
    except Exception as exc:  # noqa: BLE001
        logger.exception(exc)
    try:
        from openaiproxy.services.manager import service_manager

        await service_manager.teardown()
    except Exception as exc:  # noqa: BLE001
        logger.exception(exc)


def initialize_settings_service() -> None:
    """Initialize the settings manager."""
    from openaiproxy.services.settings import factory as settings_factory

    get_service(ServiceType.SETTINGS_SERVICE, settings_factory.SettingsServiceFactory())

async def clean_old_data(
    settings_service: SettingsService, session: AsyncSession
) -> None:
    pass

async def initialize_services(*, fix_migration: bool = False, clean_old_data: bool = False) -> None:
    """Initialize all the services needed."""
    # Setup the superuser
    await asyncio.to_thread(initialize_database, fix_migration=fix_migration)
    async with get_db_service().with_async_session() as session:
        settings_service = get_service(ServiceType.SETTINGS_SERVICE)
        if clean_old_data:
            await clean_old_data(settings_service, session)
