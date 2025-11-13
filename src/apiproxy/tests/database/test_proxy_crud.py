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

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from openaiproxy.services.database.models.node.model import Node
from openaiproxy.services.database.models.proxy.model import (
    ProxyInstance,
    ProxyNodeStatus,
    ProxyNodeStatusLog,
)
from openaiproxy.services.database.models.proxy.crud import (
    count_proxy_instances,
    delete_proxy_node_status_logs_before,
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

    remaining = await session.exec(select(ProxyNodeStatusLog))
    logs = remaining.all()
    assert len(logs) == 1
    assert logs[0].id == recent_log_id

    second_pass = await delete_proxy_node_status_logs_before(session=session, before=cutoff)
    await session.commit()
    assert second_pass == 0
