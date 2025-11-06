

import pytest
from openaiproxy.services.utils import initialize_services
from openaiproxy.utils.async_helpers import run_until_complete
from typing import AsyncGenerator
from sqlmodel.ext.asyncio.session import AsyncSession
from openaiproxy.services.deps import get_db_service
from openaiproxy.services.database.service import DatabaseService

@pytest.fixture(scope="session", autouse=True)
def initialize_test():
    run_until_complete(initialize_services())

@pytest.fixture(scope="function")
async def session() -> AsyncGenerator[AsyncSession, None]:
    db_service: DatabaseService = get_db_service()
    async with db_service.with_async_session() as session:
        yield session
