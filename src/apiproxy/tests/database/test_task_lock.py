from __future__ import annotations

import pytest
from sqlmodel import delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from openaiproxy.services.database.models.proxy.crud import (
    acquire_database_task_lock,
    release_database_task_lock,
)
from openaiproxy.services.database.models.proxy.model import DatabaseTaskLock


@pytest.fixture
async def clean_task_lock_session(session: AsyncSession):
    await session.exec(delete(DatabaseTaskLock))
    await session.commit()
    try:
        yield session
    finally:
        await session.rollback()
        await session.exec(delete(DatabaseTaskLock))
        await session.commit()


@pytest.mark.asyncio
async def test_database_task_lock_blocks_second_runner(clean_task_lock_session: AsyncSession):
    first_acquired = await acquire_database_task_lock(
        task_name='daily_usage_rollup',
        owner_token='worker-a',
        lease_seconds=300,
        session=clean_task_lock_session,
    )
    await clean_task_lock_session.commit()

    second_acquired = await acquire_database_task_lock(
        task_name='daily_usage_rollup',
        owner_token='worker-b',
        lease_seconds=300,
        session=clean_task_lock_session,
    )
    await clean_task_lock_session.commit()

    saved_rows = (await clean_task_lock_session.exec(select(DatabaseTaskLock))).all()
    assert first_acquired is True
    assert second_acquired is False
    assert len(saved_rows) == 1
    assert saved_rows[0].owner_token == 'worker-a'


@pytest.mark.asyncio
async def test_database_task_lock_can_be_reacquired_after_release(clean_task_lock_session: AsyncSession):
    first_acquired = await acquire_database_task_lock(
        task_name='weekly_usage_rollup',
        owner_token='worker-a',
        lease_seconds=300,
        session=clean_task_lock_session,
    )
    await clean_task_lock_session.commit()

    released = await release_database_task_lock(
        task_name='weekly_usage_rollup',
        owner_token='worker-a',
        session=clean_task_lock_session,
    )
    await clean_task_lock_session.commit()

    reacquired = await acquire_database_task_lock(
        task_name='weekly_usage_rollup',
        owner_token='worker-b',
        lease_seconds=300,
        session=clean_task_lock_session,
    )
    await clean_task_lock_session.commit()

    saved_rows = (await clean_task_lock_session.exec(select(DatabaseTaskLock))).all()
    assert first_acquired is True
    assert released is True
    assert reacquired is True
    assert len(saved_rows) == 1
    assert saved_rows[0].owner_token == 'worker-b'