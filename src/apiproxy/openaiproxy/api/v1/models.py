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

from fastapi import APIRouter, Depends
from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.api.schemas import (
    ModelCard, ModelList, ModelPermission
)
from openaiproxy.services.deps import get_node_proxy_service
from openaiproxy.services.nodeproxy.service import NodeProxyService
from openaiproxy.logging import logger

router = APIRouter(tags=["可用模型列表"])

@router.get('/models')
def available_models(
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
) -> ModelList:
    """Show available models."""
    logger.debug('应用 {} 请求可用模型列表', access_ctx.ownerapp_id)
    model_cards = []
    for model_name in nodeproxy_service.model_list:
        model_cards.append(
            ModelCard(
                id=model_name,
                root=model_name,
                permission=[ModelPermission()]
            )
        )
    return ModelList(data=model_cards)
