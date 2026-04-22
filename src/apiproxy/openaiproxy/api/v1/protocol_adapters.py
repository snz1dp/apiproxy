"""Protocol conversion helpers for OpenAI and Anthropic compatible routes."""

from __future__ import annotations

import time
from http import HTTPStatus
from typing import Any, Dict, Iterable, Iterator, Optional
from uuid import uuid4

import orjson


def _normalize_text(content: Any) -> str:
    """Normalize heterogeneous content blocks into plain text."""
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, bytes):
        try:
            return content.decode('utf-8', errors='ignore')
        except Exception:  # noqa: BLE001
            return ''
    if isinstance(content, (int, float, bool)):
        return str(content)
    if isinstance(content, list):
        return ''.join(_normalize_text(item) for item in content)
    if isinstance(content, dict):
        block_type = content.get('type')
        if block_type == 'text':
            return _normalize_text(content.get('text'))
        if block_type == 'tool_result':
            return _normalize_text(content.get('content'))
        if block_type == 'tool_use':
            return _normalize_text(content.get('input'))
        parts = []
        for key in ('text', 'content', 'value', 'input', 'arguments'):
            if key in content:
                parts.append(_normalize_text(content.get(key)))
        return ''.join(parts)
    return str(content)


def estimate_text_tokens(text: str) -> int:
    """Estimate token count without introducing a new hard dependency."""
    if not text:
        return 0
    approx = len(text) // 4
    if approx == 0:
        return len(text.split()) or 0
    return approx


def estimate_anthropic_input_tokens(request_payload: Dict[str, Any]) -> int:
    """Estimate Anthropic messages input tokens from system and message content."""
    parts = [_normalize_text(request_payload.get('system'))]
    for message in request_payload.get('messages', []) or []:
        if isinstance(message, dict):
            parts.append(_normalize_text(message.get('content')))
    return estimate_text_tokens(''.join(parts))


def build_anthropic_count_tokens_payload(request_payload: Dict[str, Any]) -> Dict[str, int]:
    """Build a synthetic Anthropic count_tokens response payload."""
    return {'input_tokens': estimate_anthropic_input_tokens(request_payload)}


def _normalize_anthropic_content_blocks(content: Any) -> list[dict[str, Any]]:
    """Normalize incoming content to Anthropic block list."""
    if content is None:
        return []
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, dict):
                blocks.append(item)
            else:
                blocks.append({'type': 'text', 'text': _normalize_text(item)})
        return blocks
    return [{'type': 'text', 'text': _normalize_text(content)}]


def _normalize_openai_messages(messages: Any) -> list[dict[str, Any]]:
    """Normalize incoming messages to OpenAI role/content structure."""
    if isinstance(messages, list):
        normalized_messages: list[dict[str, Any]] = []
        for item in messages:
            if isinstance(item, dict):
                normalized_messages.append(item)
            else:
                normalized_messages.append({'role': 'user', 'content': _normalize_text(item)})
        return normalized_messages
    return [{'role': 'user', 'content': _normalize_text(messages)}]


def openai_chat_request_to_anthropic_request(request_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Convert OpenAI chat completions request payload to Anthropic messages payload."""
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in _normalize_openai_messages(request_payload.get('messages', [])):
        role = message.get('role') or 'user'
        content = message.get('content')
        if role == 'system':
            system_parts.append(_normalize_text(content))
            continue
        messages.append({
            'role': 'assistant' if role == 'assistant' else 'user',
            'content': _normalize_anthropic_content_blocks(content),
        })

    payload: Dict[str, Any] = {
        'model': request_payload.get('model'),
        'messages': messages,
        'max_tokens': int(request_payload.get('max_tokens') or 1024),
        'stream': bool(request_payload.get('stream')),
    }
    if system_parts:
        payload['system'] = '\n'.join(part for part in system_parts if part)
    if request_payload.get('temperature') is not None:
        payload['temperature'] = request_payload.get('temperature')
    if request_payload.get('top_p') is not None:
        payload['top_p'] = request_payload.get('top_p')
    stop = request_payload.get('stop')
    if isinstance(stop, str):
        payload['stop_sequences'] = [stop]
    elif isinstance(stop, list):
        payload['stop_sequences'] = [item for item in stop if isinstance(item, str)]
    tools = request_payload.get('tools')
    if isinstance(tools, list):
        payload['tools'] = [
            {
                'name': tool.get('function', {}).get('name'),
                'description': tool.get('function', {}).get('description'),
                'input_schema': tool.get('function', {}).get('parameters') or {'type': 'object', 'properties': {}},
            }
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get('function'), dict)
        ]
    return payload


def openai_completion_request_to_anthropic_request(request_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Convert OpenAI completions request payload to Anthropic messages payload."""
    prompt = request_payload.get('prompt')
    if isinstance(prompt, list):
        prompt_text = ''.join(_normalize_text(item) for item in prompt)
    else:
        prompt_text = _normalize_text(prompt)
    return {
        'model': request_payload.get('model'),
        'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': prompt_text}]}],
        'max_tokens': int(request_payload.get('max_tokens') or 1024),
        'stream': bool(request_payload.get('stream')),
        'temperature': request_payload.get('temperature'),
        'top_p': request_payload.get('top_p'),
        'stop_sequences': [request_payload['stop']] if isinstance(request_payload.get('stop'), str) else request_payload.get('stop'),
    }


def anthropic_messages_to_openai_request(request_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Convert Anthropic messages request payload to OpenAI chat completions payload."""
    messages: list[dict[str, Any]] = []
    system_text = _normalize_text(request_payload.get('system'))
    if system_text:
        messages.append({'role': 'system', 'content': system_text})
    for message in request_payload.get('messages', []) or []:
        if not isinstance(message, dict):
            continue
        role = message.get('role') or 'user'
        messages.append({
            'role': role,
            'content': _normalize_text(message.get('content')),
        })

    payload: Dict[str, Any] = {
        'model': request_payload.get('model'),
        'messages': messages,
        'max_tokens': request_payload.get('max_tokens'),
        'temperature': request_payload.get('temperature'),
        'top_p': request_payload.get('top_p'),
        'stream': bool(request_payload.get('stream')),
    }
    stop_sequences = request_payload.get('stop_sequences')
    if isinstance(stop_sequences, list):
        payload['stop'] = [item for item in stop_sequences if isinstance(item, str)]
    tools = request_payload.get('tools')
    if isinstance(tools, list):
        payload['tools'] = [
            {
                'type': 'function',
                'function': {
                    'name': tool.get('name'),
                    'description': tool.get('description'),
                    'parameters': tool.get('input_schema') or {'type': 'object', 'properties': {}},
                },
            }
            for tool in tools
            if isinstance(tool, dict)
        ]
    return payload


def _map_finish_reason_to_anthropic(finish_reason: Optional[str]) -> Optional[str]:
    if finish_reason == 'length':
        return 'max_tokens'
    if finish_reason == 'tool_calls':
        return 'tool_use'
    if finish_reason in {'stop', 'function_call'}:
        return 'end_turn'
    return finish_reason


def _map_finish_reason_to_openai(stop_reason: Optional[str]) -> Optional[str]:
    if stop_reason == 'max_tokens':
        return 'length'
    if stop_reason == 'tool_use':
        return 'tool_calls'
    if stop_reason in {'end_turn', 'stop_sequence'}:
        return 'stop'
    return stop_reason


def _build_openai_error_payload(message: str, status_code: int = 400) -> Dict[str, Any]:
    """Build an OpenAI-compatible error payload."""
    return {
        'error': {
            'message': message,
            'type': 'invalid_request_error' if status_code < 500 else 'service_unavailable_error',
            'param': None,
            'code': status_code,
        }
    }


def _build_anthropic_error_payload(message: str, error_type: str = 'invalid_request_error') -> Dict[str, Any]:
    """Build an Anthropic-compatible error payload."""
    return {
        'type': 'error',
        'error': {
            'type': error_type,
            'message': message,
        },
    }


def anthropic_response_to_openai_payload(payload: Dict[str, Any], model_name: Optional[str]) -> Dict[str, Any]:
    """Convert Anthropic messages response payload to OpenAI chat/completions shape."""
    if not isinstance(payload, dict):
        return _build_openai_error_payload('Invalid Anthropic response payload', HTTPStatus.BAD_GATEWAY.value)
    if payload.get('type') == 'error' or isinstance(payload.get('error'), dict):
        error_obj = payload.get('error') if isinstance(payload.get('error'), dict) else {}
        message = _normalize_text(error_obj.get('message')) or 'Anthropic backend request failed'
        return _build_openai_error_payload(message, HTTPStatus.BAD_GATEWAY.value)

    content_blocks = payload.get('content') if isinstance(payload.get('content'), list) else []
    text_content = ''.join(_normalize_text(block) for block in content_blocks)
    usage = payload.get('usage') if isinstance(payload.get('usage'), dict) else {}
    prompt_tokens = int(usage.get('input_tokens') or 0)
    completion_tokens = int(usage.get('output_tokens') or 0)
    return {
        'id': payload.get('id') or f'chatcmpl-{uuid4().hex}',
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': payload.get('model') or model_name,
        'choices': [
            {
                'index': 0,
                'message': {
                    'role': 'assistant',
                    'content': text_content,
                },
                'finish_reason': _map_finish_reason_to_openai(payload.get('stop_reason')),
            }
        ],
        'usage': {
            'prompt_tokens': prompt_tokens,
            'completion_tokens': completion_tokens,
            'total_tokens': prompt_tokens + completion_tokens,
        },
    }


def openai_response_to_anthropic_payload(payload: Dict[str, Any], model_name: Optional[str]) -> Dict[str, Any]:
    """Convert OpenAI chat/completions response payload to Anthropic messages shape."""
    if not isinstance(payload, dict):
        return _build_anthropic_error_payload('Invalid OpenAI response payload', 'api_error')
    if isinstance(payload.get('error'), dict):
        message = _normalize_text(payload['error'].get('message')) or 'OpenAI backend request failed'
        return _build_anthropic_error_payload(message, 'api_error')

    choices = payload.get('choices') if isinstance(payload.get('choices'), list) else []
    first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message_obj = first_choice.get('message') if isinstance(first_choice.get('message'), dict) else {}
    text_content = _normalize_text(message_obj.get('content') or first_choice.get('text'))
    usage = payload.get('usage') if isinstance(payload.get('usage'), dict) else {}
    input_tokens = int(usage.get('prompt_tokens') or usage.get('input_tokens') or 0)
    output_tokens = int(usage.get('completion_tokens') or usage.get('output_tokens') or 0)
    return {
        'id': payload.get('id') or f'msg_{uuid4().hex}',
        'type': 'message',
        'role': 'assistant',
        'model': payload.get('model') or model_name,
        'content': [{'type': 'text', 'text': text_content}],
        'stop_reason': _map_finish_reason_to_anthropic(first_choice.get('finish_reason')),
        'stop_sequence': None,
        'usage': {
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
        },
    }


def build_anthropic_models_payload(model_names: Iterable[str]) -> Dict[str, Any]:
    """Build Anthropic-compatible model list payload."""
    unique_models = list(dict.fromkeys(model_names))
    return {
        'data': [
            {
                'type': 'model',
                'id': model_name,
                'display_name': model_name,
                'created_at': '1970-01-01T00:00:00Z',
            }
            for model_name in unique_models
        ],
        'first_id': unique_models[0] if unique_models else None,
        'last_id': unique_models[-1] if unique_models else None,
        'has_more': False,
    }


def _iter_sse_payloads(raw_stream: Iterable[Any]) -> Iterator[tuple[str, Dict[str, Any]]]:
    """Iterate parsed SSE event payloads from backend chunks."""
    current_event = 'message'
    for chunk in raw_stream:
        if isinstance(chunk, (bytes, bytearray)):
            text = chunk.decode('utf-8', errors='ignore')
        else:
            text = str(chunk)
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith('event:'):
                current_event = stripped[6:].strip() or 'message'
                continue
            if not stripped.startswith('data:'):
                continue
            payload_text = stripped[5:].strip()
            if not payload_text or payload_text == '[DONE]':
                continue
            try:
                payload = orjson.loads(payload_text)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(payload, dict):
                yield current_event, payload


def iter_openai_sse_from_anthropic(raw_stream: Iterable[Any], *, model_name: Optional[str]) -> Iterator[bytes]:
    """Convert Anthropic SSE stream to OpenAI-compatible SSE chunks."""
    stream_id = f'chatcmpl-{uuid4().hex}'
    created = int(time.time())
    sent_done = False
    emitted_role = False
    for _, payload in _iter_sse_payloads(raw_stream):
        payload_type = payload.get('type')
        if payload_type == 'message_start' and not emitted_role:
            emitted_role = True
            chunk_payload = {
                'id': stream_id,
                'object': 'chat.completion.chunk',
                'created': created,
                'model': model_name,
                'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}],
            }
            yield f"data: {orjson.dumps(chunk_payload).decode('utf-8')}\n\n".encode('utf-8')
            continue
        if payload_type == 'content_block_delta':
            delta = payload.get('delta') if isinstance(payload.get('delta'), dict) else {}
            text_delta = _normalize_text(delta.get('text'))
            if text_delta:
                chunk_payload = {
                    'id': stream_id,
                    'object': 'chat.completion.chunk',
                    'created': created,
                    'model': model_name,
                    'choices': [{'index': 0, 'delta': {'content': text_delta}, 'finish_reason': None}],
                }
                yield f"data: {orjson.dumps(chunk_payload).decode('utf-8')}\n\n".encode('utf-8')
            continue
        if payload_type == 'message_delta':
            stop_reason = _map_finish_reason_to_openai(payload.get('delta', {}).get('stop_reason') if isinstance(payload.get('delta'), dict) else payload.get('stop_reason'))
            if stop_reason:
                chunk_payload = {
                    'id': stream_id,
                    'object': 'chat.completion.chunk',
                    'created': created,
                    'model': model_name,
                    'choices': [{'index': 0, 'delta': {}, 'finish_reason': stop_reason}],
                }
                yield f"data: {orjson.dumps(chunk_payload).decode('utf-8')}\n\n".encode('utf-8')
            continue
        if payload_type == 'message_stop' and not sent_done:
            sent_done = True
            yield b'data: [DONE]\n\n'
            continue
        if payload_type == 'error':
            error_payload = _build_openai_error_payload(_normalize_text(payload.get('error', {}).get('message')) or 'Anthropic backend stream failed', HTTPStatus.BAD_GATEWAY.value)
            yield f"data: {orjson.dumps(error_payload).decode('utf-8')}\n\n".encode('utf-8')
    if not sent_done:
        yield b'data: [DONE]\n\n'


def iter_anthropic_sse_from_openai(raw_stream: Iterable[Any], *, model_name: Optional[str]) -> Iterator[bytes]:
    """Convert OpenAI SSE stream to Anthropic-compatible SSE chunks."""
    message_id = f'msg_{uuid4().hex}'
    started = False
    content_started = False
    final_stop_reason: Optional[str] = None
    prompt_tokens = 0
    output_tokens = 0

    def emit_event(event: str, payload: Dict[str, Any]) -> bytes:
        serialized = orjson.dumps(payload).decode('utf-8')
        return f'event: {event}\ndata: {serialized}\n\n'.encode('utf-8')

    for _, payload in _iter_sse_payloads(raw_stream):
        if isinstance(payload.get('error'), dict):
            error_payload = _build_anthropic_error_payload(
                _normalize_text(payload['error'].get('message')) or 'OpenAI backend stream failed',
                'api_error',
            )
            yield emit_event('error', error_payload)
            continue

        if not started:
            started = True
            yield emit_event(
                'message_start',
                {
                    'type': 'message_start',
                    'message': {
                        'id': message_id,
                        'type': 'message',
                        'role': 'assistant',
                        'model': model_name,
                        'content': [],
                        'stop_reason': None,
                        'stop_sequence': None,
                        'usage': {'input_tokens': 0, 'output_tokens': 0},
                    },
                },
            )
        choices = payload.get('choices') if isinstance(payload.get('choices'), list) else []
        first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        delta = first_choice.get('delta') if isinstance(first_choice.get('delta'), dict) else {}
        text_delta = _normalize_text(delta.get('content') or first_choice.get('text'))
        if text_delta:
            if not content_started:
                content_started = True
                yield emit_event(
                    'content_block_start',
                    {
                        'type': 'content_block_start',
                        'index': 0,
                        'content_block': {'type': 'text', 'text': ''},
                    },
                )
            output_tokens += estimate_text_tokens(text_delta)
            yield emit_event(
                'content_block_delta',
                {
                    'type': 'content_block_delta',
                    'index': 0,
                    'delta': {'type': 'text_delta', 'text': text_delta},
                },
            )

        usage = payload.get('usage') if isinstance(payload.get('usage'), dict) else {}
        prompt_tokens = int(usage.get('prompt_tokens') or usage.get('input_tokens') or prompt_tokens)
        output_tokens = int(usage.get('completion_tokens') or usage.get('output_tokens') or output_tokens)
        finish_reason = first_choice.get('finish_reason')
        if finish_reason:
            final_stop_reason = _map_finish_reason_to_anthropic(finish_reason)

    if not started:
        yield emit_event(
            'message_start',
            {
                'type': 'message_start',
                'message': {
                    'id': message_id,
                    'type': 'message',
                    'role': 'assistant',
                    'model': model_name,
                    'content': [],
                    'stop_reason': None,
                    'stop_sequence': None,
                    'usage': {'input_tokens': prompt_tokens, 'output_tokens': output_tokens},
                },
            },
        )
    if content_started:
        yield emit_event('content_block_stop', {'type': 'content_block_stop', 'index': 0})
    yield emit_event(
        'message_delta',
        {
            'type': 'message_delta',
            'delta': {'stop_reason': final_stop_reason or 'end_turn', 'stop_sequence': None},
            'usage': {'output_tokens': output_tokens},
        },
    )
    yield emit_event('message_stop', {'type': 'message_stop'})