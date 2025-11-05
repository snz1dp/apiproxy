

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
