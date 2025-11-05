
import json
from typing import Any
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openaiproxy.api.schemas import ChatCompletionRequest, CompletionRequest
from openaiproxy.api.utils import check_api_key
from openaiproxy.logging import logger

router = APIRouter(tags=["OpenAI兼容接口"])


@router.post('/chat/completions', dependencies=[Depends(check_api_key)])
async def chat_completions_v1(
    request: ChatCompletionRequest,
    raw_request: Request = None,
    node_manager: Any = None
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
    check_response = await node_manager.check_request_model(request.model)
    if check_response is not None:
        return check_response
    node_url = node_manager.get_node_url(request.model)
    if not node_url:
        return node_manager.handle_unavailable_model(request.model)

    logger.info(f'A request is dispatched to {node_url}')
    request_dict = request.model_dump(exclude_none=True)
    start = node_manager.pre_call(node_url)
    if request.stream is True:
        response = node_manager.stream_generate(
            request_dict, node_url,
            '/v1/chat/completions',
            node_manager.status[node_url].api_key
        )
        background_task = node_manager.create_background_tasks(node_url, start)
        return StreamingResponse(response, background=background_task)
    else:
        response = await node_manager.generate(
            request_dict, node_url,
            '/v1/chat/completions',
            node_manager.status[node_url].api_key
        )
        node_manager.post_call(node_url, start)
        return JSONResponse(json.loads(response))


@router.post('/completions', dependencies=[Depends(check_api_key)])
async def completions_v1(
    request: CompletionRequest,
    raw_request: Request = None,
    node_manager: Any = None
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
    check_response = await node_manager.check_request_model(request.model)
    if check_response is not None:
        return check_response
    node_url = node_manager.get_node_url(request.model)
    if not node_url:
        return node_manager.handle_unavailable_model(request.model)

    logger.info(f'A request is dispatched to {node_url}')
    request_dict = request.model_dump(exclude_none=True)
    start = node_manager.pre_call(node_url)
    if request.stream is True:
        response = node_manager.stream_generate(
            request_dict, node_url,
            '/v1/completions',
            node_manager.status[node_url].api_key
        )
        background_task = node_manager.create_background_tasks(node_url, start)
        return StreamingResponse(response, background=background_task)
    else:
        response = await node_manager.generate(request_dict, node_url,
                                               '/v1/completions',
                                               node_manager.status[node_url].api_key
                                               )
        node_manager.post_call(node_url, start)
        return JSONResponse(json.loads(response))
