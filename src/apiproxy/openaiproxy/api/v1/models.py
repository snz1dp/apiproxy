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

from typing import Any
from fastapi import APIRouter, Depends
from openaiproxy.api.utils import check_api_key
from openaiproxy.api.schemas import (
    ModelCard, ModelList, ModelPermission
)
from openaiproxy.services.deps import get_node_manager
from openaiproxy.services.nodemanager.service import NodeManager

router = APIRouter(tags=["可用模型列表"])

@router.get('/models', dependencies=[Depends(check_api_key)])
def available_models(
    node_manager: NodeManager = Depends(get_node_manager)
) -> ModelList:
    """Show available models."""
    model_cards = []
    for model_name in node_manager.model_list:
        model_cards.append(
            ModelCard(
                id=model_name,
                root=model_name,
                permission=[ModelPermission()]
            )
        )
    return ModelList(data=model_cards)
