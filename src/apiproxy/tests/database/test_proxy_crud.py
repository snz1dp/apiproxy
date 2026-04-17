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

from datetime import datetime, timedelta
from uuid import uuid4

from sqlmodel import delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from openaiproxy.services.deps import get_db_service
from openaiproxy.services.database.models.node.model import Node
from openaiproxy.services.database.models.proxy.model import (
    ProxyInstance,
    ProxyNodeStatus,
    ProxyNodeStatusLog,
)
from openaiproxy.services.database.models.proxy.crud import (
    count_proxy_instances,
    delete_stale_proxy_node_status,
    delete_proxy_node_status_logs_before,
    upsert_proxy_node_status,
)
from openaiproxy.utils.timezone import current_timezone

async def test_count_proxy_instances(session: AsyncSession):
    total = await count_proxy_instances(session=session)
    assert isinstance(total, int)


async def test_delete_proxy_node_status_logs_before(session: AsyncSession):
    now = datetime.now(tz=current_timezone())

    node = Node(url=f"http://cleanup-node-{uuid4()}", name="cleanup-node")
    session.add(node)
    await session.flush()

    proxy = ProxyInstance(instance_name=f"cleanup-proxy-{uuid4()}", instance_ip="127.0.0.1")
    session.add(proxy)
    await session.flush()

    status = ProxyNodeStatus(node_id=node.id, proxy_id=proxy.id)
    session.add(status)
    await session.flush()
    status_id = status.id

    old_log = ProxyNodeStatusLog(
        node_id=node.id,
        proxy_id=proxy.id,
        status_id=status.id,
        start_at=now - timedelta(days=120),
        end_at=now - timedelta(days=119, hours=23),
        latency=1.2,
        request_tokens=10,
        response_tokens=4,
        total_tokens=14,
    )
    recent_log = ProxyNodeStatusLog(
        node_id=node.id,
        proxy_id=proxy.id,
        status_id=status.id,
        start_at=now - timedelta(days=10),
        end_at=now - timedelta(days=10) + timedelta(seconds=1),
        latency=0.5,
        request_tokens=8,
        response_tokens=5,
        total_tokens=13,
    )
    session.add(old_log)
    session.add(recent_log)
    await session.commit()

    recent_log_id = recent_log.id
    cutoff = now - timedelta(days=90)

    deleted = await delete_proxy_node_status_logs_before(session=session, before=cutoff)
    await session.commit()

    assert deleted == 1

    remaining = await session.exec(
        select(ProxyNodeStatusLog).where(ProxyNodeStatusLog.status_id == status_id)
    )
    logs = remaining.all()
    assert len(logs) == 1
    assert logs[0].id == recent_log_id

    second_pass = await delete_proxy_node_status_logs_before(session=session, before=cutoff)
    await session.commit()
    assert second_pass == 0


async def test_delete_stale_proxy_node_status(session: AsyncSession):
    now = datetime.now(tz=current_timezone())

    await session.exec(delete(ProxyNodeStatusLog))
    await session.exec(delete(ProxyNodeStatus))
    await session.commit()

    node = Node(url=f"http://stale-status-node-{uuid4()}", name="stale-status-node")
    session.add(node)
    await session.flush()

    active_proxy = ProxyInstance(
        instance_name=f"active-proxy-{uuid4()}",
        instance_ip="127.0.0.1",
    )
    stale_proxy = ProxyInstance(
        instance_name=f"stale-proxy-{uuid4()}",
        instance_ip="127.0.0.2",
    )
    session.add(active_proxy)
    session.add(stale_proxy)
    await session.flush()

    stale_status = ProxyNodeStatus(
        node_id=node.id,
        proxy_id=stale_proxy.id,
        updated_at=now - timedelta(minutes=10),
    )
    active_status = ProxyNodeStatus(
        node_id=node.id,
        proxy_id=active_proxy.id,
        updated_at=now,
    )
    session.add(stale_status)
    session.add(active_status)
    await session.commit()

    deleted = await delete_stale_proxy_node_status(
        session=session,
        before=now - timedelta(minutes=1),
        exclude_proxy_id=active_proxy.id,
    )
    await session.commit()

    assert deleted == 1
    db_service = get_db_service()
    async with db_service.with_async_session() as verify_session:
        assert await verify_session.get(ProxyNodeStatus, stale_status.id) is None
        assert await verify_session.get(ProxyNodeStatus, active_status.id) is not None

    second_pass = await delete_stale_proxy_node_status(
        session=session,
        before=now - timedelta(minutes=1),
        exclude_proxy_id=active_proxy.id,
    )
    await session.commit()

    assert second_pass == 0


async def test_upsert_proxy_node_status_recovers_after_external_delete(session: AsyncSession):
    node = Node(url=f"http://status-upsert-node-{uuid4()}", name="status-upsert-node")
    session.add(node)
    await session.flush()
    node_id = node.id

    proxy = ProxyInstance(instance_name=f"status-upsert-proxy-{uuid4()}", instance_ip="127.0.0.1")
    session.add(proxy)
    await session.flush()
    proxy_id = proxy.id

    original = await upsert_proxy_node_status(
        session=session,
        node_id=node_id,
        proxy_id=proxy_id,
        status_id=None,
        unfinished=1,
        latency=0.5,
        speed=2.0,
        avaiaible=True,
    )
    await session.commit()

    db_service = get_db_service()
    async with db_service.with_async_session() as delete_session:
        stale_row = await delete_session.get(ProxyNodeStatus, original.id)
        assert stale_row is not None
        await delete_session.delete(stale_row)
        await delete_session.commit()

    recovered = await upsert_proxy_node_status(
        session=session,
        node_id=node_id,
        proxy_id=proxy_id,
        status_id=original.id,
        unfinished=3,
        latency=1.25,
        speed=0.8,
        avaiaible=False,
    )
    await session.commit()
    refreshed = await session.get(ProxyNodeStatus, recovered.id)

    assert refreshed is not None
    assert refreshed.id == original.id
    assert refreshed.unfinished == 3
    assert refreshed.latency == 1.25
    assert refreshed.speed == 0.8
    assert refreshed.avaiaible is False

    status_rows = (
        await session.exec(
            select(ProxyNodeStatus).where(
                ProxyNodeStatus.node_id == node_id,
                ProxyNodeStatus.proxy_id == proxy_id,
            )
        )
    ).all()
    assert len(status_rows) == 1
