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