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

import json
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openaiproxy.api.schemas import ChatCompletionRequest, CompletionRequest
from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.logging import logger
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.services.deps import get_node_proxy_service
from openaiproxy.services.database.models.node.model import ModelType
from openaiproxy.services.nodeproxy.service import NodeProxyService

router = APIRouter(tags=["OpenAI兼容接口"])


@router.post('/chat/completions')
async def chat_completions_v1(
    request: ChatCompletionRequest,
    raw_request: Request = None,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Completion API similar to OpenAI's API.

    Refer to  `https://platform.openai.com/docs/api-reference/chat/create`
    for the API specification.

    The request should be a JSON object with the following fields:
    - model: model name. Available from /v1/models.
    - messages: string prompt or chat history in OpenAI format. Chat history
        example: `[{"role": "user", "content": "hi"}]`.
    - temperature (float): to modulate the next token probability
    - top_p (float): If set to float < 1, only the smallest set of most
        probable tokens with probabilities that add up to top_p or higher
        are kept for generation.
    - n (int): How many chat completion choices to generate for each input
        message. **Only support one here**.
    - stream: whether to stream the results or not. Default to false.
    - max_tokens (int | None): output token nums. Default to None.
    - repetition_penalty (float): The parameter for repetition penalty.
        1.0 means no penalty
    - stop (str | List[str] | None): To stop generating further
        tokens. Only accept stop words that's encoded to one token idex.
    - response_format (Dict | None): Only pytorch backend support formatting
        response. Examples: `{"type": "json_schema", "json_schema": {"name":
        "test","schema": {"properties": {"name": {"type": "string"}},
        "required": ["name"], "type": "object"}}}`
        or `{"type": "regex_schema", "regex_schema": "call me [A-Za-z]{1,10}"}`
    - logit_bias (Dict): Bias to logits. Only supported in pytorch engine.
    - tools (List): A list of tools the model may call. Currently, only
        internlm2 functions are supported as a tool. Use this to specify a
        list of functions for which the model can generate JSON inputs.
    - tool_choice (str | object): Controls which (if any) tool is called by
        the model. `none` means the model will not call any tool and instead
        generates a message. Specifying a particular tool via {"type":
        "function", "function": {"name": "my_function"}} forces the model to
        call that tool. `auto` or `required` will put all the tools information
        to the model.

    Additional arguments supported by LMDeploy:
    - top_k (int): The number of the highest probability vocabulary
        tokens to keep for top-k-filtering
    - ignore_eos (bool): indicator for ignoring eos
    - skip_special_tokens (bool): Whether or not to remove special tokens
        in the decoding. Default to be True.
    - min_new_tokens (int): To generate at least numbers of tokens.
    - min_p (float): Minimum token probability, which will be scaled by the
        probability of the most likely token. It must be a value between
        0 and 1. Typical values are in the 0.01-0.2 range, comparably
        selective as setting `top_p` in the 0.99-0.8 range (use the
        opposite of normal `top_p` values)

    Currently we do not support the following features:
    - presence_penalty (replaced with repetition_penalty)
    - frequency_penalty (replaced with repetition_penalty)
    """
    model_type = ModelType.chat.value
    check_response = await nodeproxy_service.check_request_model(request.model, model_type)
    if check_response is not None:
        return check_response
    node_url = nodeproxy_service.get_node_url(request.model, model_type)
    if not node_url:
        return nodeproxy_service.handle_unavailable_model(request.model, model_type)

    logger.info('Owner %s dispatched request to %s', access_ctx.ownerapp_id, node_url)
    request_dict = request.model_dump(exclude_none=True)
    request_ctx = nodeproxy_service.pre_call(
        node_url,
        model_name=request.model,
        ownerapp_id=access_ctx.ownerapp_id,
        request_action=RequestAction.completions,
    )
    if request.stream is True:
        response = nodeproxy_service.stream_generate(
            request_dict, node_url,
            '/v1/chat/completions',
            nodeproxy_service.status[node_url].api_key
        )
        background_task = nodeproxy_service.create_background_tasks(node_url, request_ctx)
        return StreamingResponse(response, background=background_task)
    else:
        response = await nodeproxy_service.generate(
            request_dict, node_url,
            '/v1/chat/completions',
            nodeproxy_service.status[node_url].api_key
        )
        nodeproxy_service.post_call(node_url, request_ctx)
        return JSONResponse(json.loads(response))


@router.post('/completions')
async def completions_v1(
    request: CompletionRequest,
    raw_request: Request = None,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Completion API similar to OpenAI's API.

    Go to `https://platform.openai.com/docs/api-reference/completions/create`
    for the API specification.

    The request should be a JSON object with the following fields:
    - model (str): model name. Available from /v1/models.
    - prompt (str): the input prompt.
    - suffix (str): The suffix that comes after a completion of inserted text.
    - max_tokens (int): output token nums. Default to 16.
    - temperature (float): to modulate the next token probability
    - top_p (float): If set to float < 1, only the smallest set of most
        probable tokens with probabilities that add up to top_p or higher
        are kept for generation.
    - n (int): How many chat completion choices to generate for each input
        message. **Only support one here**.
    - stream: whether to stream the results or not. Default to false.
    - repetition_penalty (float): The parameter for repetition penalty.
        1.0 means no penalty
    - user (str): A unique identifier representing your end-user.
    - stop (str | List[str] | None): To stop generating further
        tokens. Only accept stop words that's encoded to one token idex.

    Additional arguments supported by LMDeploy:
    - ignore_eos (bool): indicator for ignoring eos
    - skip_special_tokens (bool): Whether or not to remove special tokens
        in the decoding. Default to be True.
    - top_k (int): The number of the highest probability vocabulary
        tokens to keep for top-k-filtering

    Currently we do not support the following features:
    - logprobs (not supported yet)
    - presence_penalty (replaced with repetition_penalty)
    - frequency_penalty (replaced with repetition_penalty)
    """
    model_type = ModelType.chat.value
    check_response = await nodeproxy_service.check_request_model(request.model, model_type)
    if check_response is not None:
        return check_response
    node_url = nodeproxy_service.get_node_url(request.model, model_type)
    if not node_url:
        return nodeproxy_service.handle_unavailable_model(request.model, model_type)

    logger.info('Owner %s dispatched request to %s', access_ctx.ownerapp_id, node_url)
    request_dict = request.model_dump(exclude_none=True)
    request_ctx = nodeproxy_service.pre_call(
        node_url,
        model_name=request.model,
        ownerapp_id=access_ctx.ownerapp_id,
        request_action=RequestAction.completions,
    )
    if request.stream is True:
        response = nodeproxy_service.stream_generate(
            request_dict, node_url,
            '/v1/completions',
            nodeproxy_service.status[node_url].api_key
        )
        background_task = nodeproxy_service.create_background_tasks(node_url, request_ctx)
        return StreamingResponse(response, background=background_task)
    else:
        response = await nodeproxy_service.generate(
            request_dict, node_url,
            '/v1/completions',
            nodeproxy_service.status[node_url].api_key
        )
        nodeproxy_service.post_call(node_url, request_ctx)
        return JSONResponse(json.loads(response))
