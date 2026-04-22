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

from sqlmodel.ext.asyncio.session import AsyncSession
from uuid import uuid4

async def test_select_nodes(session: AsyncSession):
    from openaiproxy.services.database.models.node.crud import select_nodes
    from openaiproxy.services.database.models.node.model import Node
    nodes = await select_nodes(session=session)
    assert isinstance(nodes, list)
    for node in nodes:
        assert isinstance(node, Node)

async def test_count_nodes(session: AsyncSession):
    from openaiproxy.services.database.models.node.crud import count_nodes
    total = await count_nodes(session=session)
    assert isinstance(total, int)


async def test_node_trusted_without_models_endpoint_defaults_to_false(session: AsyncSession):
    from openaiproxy.services.database.models.node.model import Node

    node = Node(url=f'http://db-node-{uuid4().hex[:8]}.example.com', name='db-node')
    session.add(node)
    await session.commit()
    await session.refresh(node)

    assert node.trusted_without_models_endpoint is False


async def test_select_nodes_returns_configured_node_and_models(session: AsyncSession):
    from openaiproxy.services.database.models.node.crud import select_node_models, select_nodes
    from openaiproxy.services.database.models.node.model import Node, NodeModel

    unique_suffix = uuid4().hex[:8]
    node = Node(
        url=f'http://configured-node-{unique_suffix}.example.com',
        name='configured-node',
        health_check=False,
    )
    session.add(node)
    await session.flush()

    chat_model = NodeModel(
        node_id=node.id,
        model_name='gpt-4o-mini',
        model_type='chat',
    )
    embeddings_model = NodeModel(
        node_id=node.id,
        model_name='text-embedding-3-small',
        model_type='embeddings',
    )
    session.add(chat_model)
    session.add(embeddings_model)
    await session.commit()

    nodes = await select_nodes(session=session)
    configured_node = next(item for item in nodes if item.id == node.id)
    assert configured_node.url == node.url
    assert configured_node.health_check is False

    models = await select_node_models(node_ids=[node.id], session=session)
    assert {(item.model_name, str(item.model_type.value if hasattr(item.model_type, 'value') else item.model_type)) for item in models} == {
        ('gpt-4o-mini', 'chat'),
        ('text-embedding-3-small', 'embeddings'),
    }
