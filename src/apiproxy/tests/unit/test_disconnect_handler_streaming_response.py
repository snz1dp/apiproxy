import asyncio

import pytest
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect

from openaiproxy.api.schemas import DisconnectHandlerStreamingResponse


@pytest.mark.asyncio
async def test_disconnect_handler_streaming_response_runs_background_after_disconnect() -> None:
    """客户端中断流式响应时，仍应执行 background 并保留 finally 中写入的状态。"""
    state: dict[str, str | int | None] = {
        'response_data': None,
        'background_seen': None,
        'disconnect_calls': 0,
    }

    async def content():
        try:
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        finally:
            state['response_data'] = 'partial'

    def mark_disconnect() -> None:
        state['disconnect_calls'] = int(state['disconnect_calls'] or 0) + 1

    async def finalize() -> None:
        state['background_seen'] = state['response_data']

    response = DisconnectHandlerStreamingResponse(
        content(),
        background=BackgroundTask(finalize),
        on_disconnect=mark_disconnect,
    )

    async def receive() -> dict[str, str]:
        return {'type': 'http.request'}

    async def send(message: dict[str, object]) -> None:
        if message['type'] == 'http.response.body' and message.get('body'):
            raise OSError('client disconnected')

    with pytest.raises(ClientDisconnect):
        await response(
            {
                'type': 'http',
                'asgi': {'spec_version': '2.4'},
                'http_version': '1.1',
                'method': 'POST',
                'path': '/v1/chat/completions',
                'headers': [],
            },
            receive,
            send,
        )

    assert state['response_data'] == 'partial'
    assert state['background_seen'] == 'partial'
    assert state['disconnect_calls'] == 1


@pytest.mark.asyncio
async def test_task_group_path_normal_completion_no_false_disconnect() -> None:
    """spec_version < 2.4 的 task group 路径：流正常完成后客户端断连，不应触发断连回调。

    复现场景：流式数据全部发送完毕 → 客户端正常关闭连接 →
    listen_for_disconnect 收到 http.disconnect，但此时流已完成，
    不应误记 'Client disconnected during streaming'。
    """
    state: dict[str, str | int | None] = {
        'response_data': None,
        'background_seen': None,
        'disconnect_calls': 0,
    }
    stream_done_event = asyncio.Event()

    def sync_content():
        """同步生成器，模拟 completions.py 中的 stream_with_usage_logging。"""
        try:
            yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            yield b'data: [DONE]\n\n'
        finally:
            state['response_data'] = 'done'

    def mark_disconnect() -> None:
        state['disconnect_calls'] = int(state['disconnect_calls'] or 0) + 1

    async def finalize() -> None:
        state['background_seen'] = state['response_data']

    response = DisconnectHandlerStreamingResponse(
        sync_content(),
        background=BackgroundTask(finalize),
        on_disconnect=mark_disconnect,
    )

    async def send(message: dict[str, object]) -> None:
        # 所有 send 正常完成；当最终空 body 发出时标记流已完成
        if message['type'] == 'http.response.body' and not message.get('body'):
            stream_done_event.set()

    async def receive() -> dict[str, str]:
        # 等待流完成后再返回断连事件，模拟客户端收完数据后正常关闭连接
        await stream_done_event.wait()
        return {'type': 'http.disconnect'}

    await response(
        {
            'type': 'http',
            'asgi': {'spec_version': '2.0'},
            'http_version': '1.1',
            'method': 'POST',
            'path': '/v1/chat/completions',
            'headers': [],
        },
        receive,
        send,
    )

    # 流正常完成，不应触发断连回调
    assert state['disconnect_calls'] == 0
    # finally 块应正常执行
    assert state['response_data'] == 'done'
    # background 应正常执行
    assert state['background_seen'] == 'done'


@pytest.mark.asyncio
async def test_task_group_path_mid_stream_disconnect_records_error() -> None:
    """spec_version < 2.4 的 task group 路径：客户端在流式传输中途断开，应触发断连回调。"""
    state: dict[str, str | int | None] = {
        'response_data': None,
        'background_seen': None,
        'disconnect_calls': 0,
    }
    first_chunk_sent = asyncio.Event()

    def sync_content():
        """同步生成器，模拟流式传输中途被中断。"""
        try:
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            # 模拟还有更多数据未发送（生成器在此挂起等待下一次 next()）
            yield b'data: {"choices":[{"delta":{"content":"more"}}]}\n\n'
        except GeneratorExit:
            state['response_data'] = 'interrupted'
            raise
        finally:
            if state['response_data'] is None:
                state['response_data'] = 'completed'

    def mark_disconnect() -> None:
        state['disconnect_calls'] = int(state['disconnect_calls'] or 0) + 1

    async def finalize() -> None:
        state['background_seen'] = state['response_data']

    response = DisconnectHandlerStreamingResponse(
        sync_content(),
        background=BackgroundTask(finalize),
        on_disconnect=mark_disconnect,
    )

    async def send(message: dict[str, object]) -> None:
        # 第一个数据块发出后通知断连事件可以到达
        if message['type'] == 'http.response.body' and message.get('body'):
            first_chunk_sent.set()

    async def receive() -> dict[str, str]:
        # 等待第一个 chunk 发出后立即断连，模拟客户端中途断开
        await first_chunk_sent.wait()
        return {'type': 'http.disconnect'}

    await response(
        {
            'type': 'http',
            'asgi': {'spec_version': '2.0'},
            'http_version': '1.1',
            'method': 'POST',
            'path': '/v1/chat/completions',
            'headers': [],
        },
        receive,
        send,
    )

    # 客户端在流式传输中途断开，应触发断连回调
    assert state['disconnect_calls'] == 1
    # background 仍应执行
    assert state['background_seen'] is not None