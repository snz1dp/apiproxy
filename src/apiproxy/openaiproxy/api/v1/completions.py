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

import asyncio
import json
import traceback
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openaiproxy.api.schemas import ChatCompletionRequest, CompletionRequest
from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.logging import logger
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.services.deps import get_node_proxy_service
from openaiproxy.services.database.models.node.model import ModelType
from openaiproxy.services.nodeproxy.service import NodeProxyService

try:  # pragma: no cover - optional dependency
    import tiktoken  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - tiktoken is optional
    tiktoken = None

_ENCODING_CACHE: Dict[str, Any] = {}


def _normalize_content_to_text(content: Any) -> str:
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, bytes):
        try:
            return content.decode('utf-8', errors='ignore')
        except Exception:  # noqa: BLE001 - defensive
            return ''
    if isinstance(content, (int, float, bool)):
        return str(content)
    if isinstance(content, list):
        return ''.join(_normalize_content_to_text(item) for item in content)
    if isinstance(content, dict):
        parts: List[str] = []
        for key in ('text', 'content', 'value'):
            if key in content:
                parts.append(_normalize_content_to_text(content[key]))
        if not parts and 'message' in content:
            parts.append(_normalize_content_to_text(content['message']))
        if not parts and 'arguments' in content:
            parts.append(_normalize_content_to_text(content['arguments']))
        return ''.join(parts)
    return str(content)


def _get_tiktoken_encoding(model: Optional[str]) -> Any:
    if tiktoken is None:
        return None
    cache_key = model or 'default'
    if cache_key in _ENCODING_CACHE:
        return _ENCODING_CACHE[cache_key]
    encoding = None
    try:
        encoding = tiktoken.encoding_for_model(model) if model else tiktoken.get_encoding('cl100k_base')
    except Exception:  # noqa: BLE001 - fall back to default encoding
        try:
            encoding = tiktoken.get_encoding('cl100k_base')
        except Exception:  # noqa: BLE001 - optional dependency
            encoding = None
    if encoding is not None:
        _ENCODING_CACHE[cache_key] = encoding
    return encoding


def _estimate_tokens(text: str, model: Optional[str]) -> int:
    if not text:
        return 0
    encoding = _get_tiktoken_encoding(model)
    if encoding is not None:
        try:
            return len(encoding.encode(text))
        except Exception:  # noqa: BLE001 - fallback to heuristic
            pass
    approx = len(text) // 4
    if approx == 0:
        return len(text.split()) or 0
    return approx


def _estimate_chat_prompt_tokens(request: ChatCompletionRequest) -> int:
    messages = request.messages
    if isinstance(messages, str):
        text = messages
    elif isinstance(messages, list):
        parts: List[str] = []
        for item in messages:
            if isinstance(item, dict):
                parts.append(_normalize_content_to_text(item.get('content')))
            else:
                parts.append(_normalize_content_to_text(item))
        text = ''.join(parts)
    else:
        text = _normalize_content_to_text(messages)
    return _estimate_tokens(text, request.model)


def _estimate_completion_prompt_tokens(request: CompletionRequest) -> int:
    prompt = request.prompt
    if isinstance(prompt, str):
        text = prompt
    elif isinstance(prompt, list):
        text = ''.join(_normalize_content_to_text(item) for item in prompt)
    else:
        text = _normalize_content_to_text(prompt)
    return _estimate_tokens(text, request.model)


def _append_response_text(container: Dict[str, Any], acc: List[str], *, is_chat: bool) -> None:
    choices = container.get('choices') if isinstance(container, dict) else None
    if not isinstance(choices, list):
        return
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        if is_chat:
            delta = choice.get('delta')
            if isinstance(delta, dict):
                if 'content' in delta:
                    acc.append(_normalize_content_to_text(delta.get('content')))
                if 'tool_calls' in delta:
                    acc.append(_normalize_content_to_text(delta.get('tool_calls')))
            message = choice.get('message')
            if isinstance(message, dict):
                acc.append(_normalize_content_to_text(message.get('content')))
        else:
            text = choice.get('text')
            if isinstance(text, str):
                acc.append(text)
        if not is_chat and 'content' in choice:
            acc.append(_normalize_content_to_text(choice['content']))


def _finalize_token_counts(
    *,
    request_ctx,
    prompt_estimate: int,
    completion_segments: List[str],
    model_name: Optional[str],
) -> None:
    if completion_segments:
        completion_text = ''.join(completion_segments).strip()
    else:
        completion_text = ''
    completion_tokens = _estimate_tokens(completion_text, model_name)
    prompt_tokens = request_ctx.request_tokens
    if prompt_tokens is None:
        prompt_tokens = prompt_estimate if prompt_estimate > 0 else None
        if prompt_tokens is not None:
            request_ctx.request_tokens = prompt_tokens
    if completion_tokens == 0 and not completion_text:
        return
    total_tokens = completion_tokens
    if prompt_tokens:
        total_tokens += prompt_tokens
    if total_tokens > 0:
        request_ctx.response_tokens = total_tokens


def _to_error_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        serialized = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):  # noqa: BLE001 - defensive
        serialized = str(value)
    serialized = serialized.strip()
    return serialized or None


def _to_error_stack(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or value
    if isinstance(value, list):
        parts = [str(item).rstrip() for item in value if item is not None]
        joined = '\n'.join(parts).strip()
        return joined or None
    return _to_error_text(value)


def _extract_backend_error(payload: Any) -> Tuple[Optional[str], Optional[str]]:
    message: Optional[str] = None
    stack: Optional[str] = None

    if isinstance(payload, dict):
        error_obj = payload.get('error')
        if isinstance(error_obj, dict):
            message = (
                _to_error_text(error_obj.get('message'))
                or _to_error_text(error_obj.get('text'))
                or _to_error_text(error_obj.get('detail'))
                or _to_error_text(error_obj.get('code'))
            )
            stack = (
                _to_error_stack(error_obj.get('stack'))
                or _to_error_stack(error_obj.get('stack_trace'))
                or _to_error_stack(error_obj.get('traceback'))
            )
            data_obj = error_obj.get('data') if isinstance(error_obj.get('data'), dict) else None
            if stack is None and data_obj is not None:
                stack = (
                    _to_error_stack(data_obj.get('stack'))
                    or _to_error_stack(data_obj.get('stack_trace'))
                    or _to_error_stack(data_obj.get('traceback'))
                )
        elif isinstance(error_obj, str):
            message = _to_error_text(error_obj)
        elif error_obj is not None:
            message = _to_error_text(error_obj)

        if message is None:
            for key in ('message', 'text', 'detail', 'error_message', 'errorDescription'):
                candidate = _to_error_text(payload.get(key))
                if candidate:
                    message = candidate
                    break

        if stack is None:
            for key in ('error_stack', 'stack', 'stack_trace', 'traceback'):
                candidate = _to_error_stack(payload.get(key))
                if candidate:
                    stack = candidate
                    break

        if message is None and payload.get('error_code') is not None:
            message = _to_error_text(payload.get('text') or payload.get('message'))
            if message is None:
                message = f'error_code={payload.get("error_code")}'
    elif isinstance(payload, str):
        stripped = payload.strip()
        message = stripped or payload

    return message, stack


def _apply_backend_error_info(request_ctx, message: Optional[str], stack: Optional[str]) -> None:
    if not message and not stack:
        return
    if message and not request_ctx.error_message:
        request_ctx.error_message = message
    if stack and not request_ctx.error_stack:
        request_ctx.error_stack = stack
    request_ctx.error = True


def _try_loads_json(data: str) -> Optional[Any]:
    if not data:
        return None
    try:
        return json.loads(data)
    except Exception:  # noqa: BLE001 - streaming payload may not be JSON
        return None


def _merge_error_info(store: Dict[str, Optional[str]], message: Optional[str], stack: Optional[str]) -> None:
    if message and not store.get('message'):
        store['message'] = message
    if stack and not store.get('stack'):
        store['stack'] = stack


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

    logger.debug('应用 {} 将请求转发到节点 {}', access_ctx.ownerapp_id, node_url)
    request_dict = request.model_dump(exclude_none=True)
    prompt_token_estimate = _estimate_chat_prompt_tokens(request)
    request_ctx = nodeproxy_service.pre_call(
        node_url,
        model_name=request.model,
        ownerapp_id=access_ctx.ownerapp_id,
        request_action=RequestAction.completions,
        request_count=prompt_token_estimate,
        stream=request.stream,
    )
    if request.stream is True:
        raw_stream = nodeproxy_service.stream_generate(
            request_dict, node_url,
            '/v1/chat/completions',
            nodeproxy_service.status[node_url].api_key
        )

        completion_segments: List[str] = []
        backend_error: Dict[str, Optional[str]] = {'message': None, 'stack': None}
        client_disconnected = False

        def _mark_client_disconnect() -> None:
            nonlocal client_disconnected
            client_disconnected = True
            _merge_error_info(backend_error, 'Client disconnected during streaming', None)

        def stream_with_usage_logging():
            try:
                for chunk in raw_stream:
                    logger.debug('流式数据片段: {}', chunk)
                    if isinstance(chunk, (bytes, bytearray)):
                        try:
                            text = chunk.decode('utf-8', errors='ignore')
                        except Exception:  # noqa: BLE001
                            text = ''
                    elif isinstance(chunk, str):
                        text = chunk
                    else:
                        text = str(chunk)
                    if text:
                        for line in text.splitlines():
                            stripped = line.strip()
                            if not stripped:
                                continue
                            payload_obj: Optional[Any] = None
                            is_data_line = stripped.startswith('data:')
                            if is_data_line:
                                payload = stripped[5:].strip()
                                if not payload or payload == '[DONE]':
                                    continue
                                payload_obj = _try_loads_json(payload)
                                if isinstance(payload_obj, dict):
                                    _append_response_text(payload_obj, completion_segments, is_chat=True)
                            elif stripped.startswith('event:') or stripped.startswith(':'):
                                continue
                            else:
                                payload_obj = _try_loads_json(stripped)

                            if payload_obj is not None:
                                message, stack = _extract_backend_error(payload_obj)
                                _merge_error_info(backend_error, message, stack)
                            elif not is_data_line:
                                fallback_msg = _to_error_text(stripped)
                                if fallback_msg:
                                    _merge_error_info(backend_error, fallback_msg, None)
                    yield chunk
            except GeneratorExit:
                _mark_client_disconnect()
                raise
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, asyncio.CancelledError) or exc.__class__.__name__ in {'ClientDisconnect', 'ClientDisconnectError'}:
                    _mark_client_disconnect()
                raise
            finally:
                if client_disconnected:
                    _apply_backend_error_info(
                        request_ctx,
                        backend_error.get('message'),
                        backend_error.get('stack'),
                    )
                elif backend_error['message'] or backend_error['stack']:
                    _apply_backend_error_info(
                        request_ctx,
                        backend_error.get('message'),
                        backend_error.get('stack'),
                    )
                _finalize_token_counts(
                    request_ctx=request_ctx,
                    prompt_estimate=prompt_token_estimate,
                    completion_segments=completion_segments,
                    model_name=request.model,
                )

        background_task = nodeproxy_service.create_background_tasks(node_url, request_ctx)
        return StreamingResponse(stream_with_usage_logging(), background=background_task)
    else:
        response = await nodeproxy_service.generate(
            request_dict, node_url,
            '/v1/chat/completions',
            nodeproxy_service.status[node_url].api_key
        )
        try:
            payload = json.loads(response)
        except Exception:  # noqa: BLE001
            error_message = f'Failed to decode backend response: {response!r}'
            stack = traceback.format_exc()
            _apply_backend_error_info(request_ctx, error_message, stack)
            nodeproxy_service.post_call(node_url, request_ctx)
            raise
        message, stack = _extract_backend_error(payload)
        _apply_backend_error_info(request_ctx, message, stack)
        usage = payload.get('usage') if isinstance(payload, dict) else None
        if isinstance(usage, dict):
            total_tokens = usage.get('total_tokens')
            if isinstance(total_tokens, int):
                request_ctx.response_tokens = total_tokens
            prompt_tokens = usage.get('prompt_tokens')
            if isinstance(prompt_tokens, int):
                request_ctx.request_tokens = prompt_tokens
        elif isinstance(payload, dict):
            completion_segments: List[str] = []
            _append_response_text(payload, completion_segments, is_chat=True)
            _finalize_token_counts(
                request_ctx=request_ctx,
                prompt_estimate=prompt_token_estimate,
                completion_segments=completion_segments,
                model_name=request.model,
            )
        nodeproxy_service.post_call(node_url, request_ctx)
        return JSONResponse(payload)


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

    logger.debug('应用 {} 将请求转发到节点 {}', access_ctx.ownerapp_id, node_url)
    request_dict = request.model_dump(exclude_none=True)
    prompt_token_estimate = _estimate_completion_prompt_tokens(request)
    request_ctx = nodeproxy_service.pre_call(
        node_url,
        model_name=request.model,
        ownerapp_id=access_ctx.ownerapp_id,
        request_action=RequestAction.completions,
        request_count=prompt_token_estimate,
        stream=request.stream,
    )
    if request.stream is True:
        raw_stream = nodeproxy_service.stream_generate(
            request_dict, node_url,
            '/v1/completions',
            nodeproxy_service.status[node_url].api_key
        )

        completion_segments: List[str] = []
        backend_error: Dict[str, Optional[str]] = {'message': None, 'stack': None}
        client_disconnected = False

        def _mark_client_disconnect() -> None:
            nonlocal client_disconnected
            client_disconnected = True
            _merge_error_info(backend_error, 'Client disconnected during streaming', None)

        def stream_with_usage_logging():
            try:
                for chunk in raw_stream:
                    logger.debug('流式数据片段: {}', chunk)
                    if isinstance(chunk, (bytes, bytearray)):
                        try:
                            text = chunk.decode('utf-8', errors='ignore')
                        except Exception:  # noqa: BLE001
                            text = ''
                    elif isinstance(chunk, str):
                        text = chunk
                    else:
                        text = str(chunk)
                    if text:
                        for line in text.splitlines():
                            stripped = line.strip()
                            if not stripped:
                                continue
                            payload_obj: Optional[Any] = None
                            is_data_line = stripped.startswith('data:')
                            if is_data_line:
                                payload = stripped[5:].strip()
                                if not payload or payload == '[DONE]':
                                    continue
                                payload_obj = _try_loads_json(payload)
                                if isinstance(payload_obj, dict):
                                    _append_response_text(payload_obj, completion_segments, is_chat=False)
                            elif stripped.startswith('event:') or stripped.startswith(':'):
                                continue
                            else:
                                payload_obj = _try_loads_json(stripped)

                            if payload_obj is not None:
                                message, stack = _extract_backend_error(payload_obj)
                                _merge_error_info(backend_error, message, stack)
                            elif not is_data_line:
                                fallback_msg = _to_error_text(stripped)
                                if fallback_msg:
                                    _merge_error_info(backend_error, fallback_msg, None)
                    yield chunk
            except GeneratorExit:
                _mark_client_disconnect()
                raise
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, asyncio.CancelledError) or exc.__class__.__name__ in {'ClientDisconnect', 'ClientDisconnectError'}:
                    _mark_client_disconnect()
                raise
            finally:
                if client_disconnected:
                    _apply_backend_error_info(
                        request_ctx,
                        backend_error.get('message'),
                        backend_error.get('stack'),
                    )
                elif backend_error['message'] or backend_error['stack']:
                    _apply_backend_error_info(
                        request_ctx,
                        backend_error.get('message'),
                        backend_error.get('stack'),
                    )
                _finalize_token_counts(
                    request_ctx=request_ctx,
                    prompt_estimate=prompt_token_estimate,
                    completion_segments=completion_segments,
                    model_name=request.model,
                )

        background_task = nodeproxy_service.create_background_tasks(node_url, request_ctx)
        return StreamingResponse(stream_with_usage_logging(), background=background_task)
    else:
        response = await nodeproxy_service.generate(
            request_dict, node_url,
            '/v1/completions',
            nodeproxy_service.status[node_url].api_key
        )
        try:
            payload = json.loads(response)
        except Exception:  # noqa: BLE001
            error_message = f'Failed to decode backend response: {response!r}'
            stack = traceback.format_exc()
            _apply_backend_error_info(request_ctx, error_message, stack)
            nodeproxy_service.post_call(node_url, request_ctx)
            raise
        message, stack = _extract_backend_error(payload)
        _apply_backend_error_info(request_ctx, message, stack)
        usage = payload.get('usage') if isinstance(payload, dict) else None
        if isinstance(usage, dict):
            total_tokens = usage.get('total_tokens')
            if isinstance(total_tokens, int):
                request_ctx.response_tokens = total_tokens
            prompt_tokens = usage.get('prompt_tokens')
            if isinstance(prompt_tokens, int):
                request_ctx.request_tokens = prompt_tokens
        elif isinstance(payload, dict):
            completion_segments: List[str] = []
            _append_response_text(payload, completion_segments, is_chat=False)
            _finalize_token_counts(
                request_ctx=request_ctx,
                prompt_estimate=prompt_token_estimate,
                completion_segments=completion_segments,
                model_name=request.model,
            )
        nodeproxy_service.post_call(node_url, request_ctx)
        return JSONResponse(payload)
