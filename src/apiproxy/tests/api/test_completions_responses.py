from http import HTTPStatus

from openaiproxy.api.schemas import ChatCompletionRequest, CompletionRequest
from openaiproxy.api.v1.completions import _build_backend_json_response
from openaiproxy.services.nodeproxy.constants import ErrorCodes


def test_backend_json_response_returns_gateway_timeout_for_timeout_payload():
    response = _build_backend_json_response(
        {
            'error_code': ErrorCodes.API_TIMEOUT.value,
            'text': 'timeout',
        }
    )

    assert response.status_code == HTTPStatus.GATEWAY_TIMEOUT


def test_backend_json_response_returns_service_unavailable_for_service_failure():
    response = _build_backend_json_response(
        {
            'error_code': ErrorCodes.SERVICE_UNAVAILABLE.value,
            'text': 'service unavailable',
        }
    )

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE


def test_backend_json_response_keeps_success_status_for_normal_payload():
    response = _build_backend_json_response(
        {
            'id': 'resp_1',
            'object': 'chat.completion',
        }
    )

    assert response.status_code == HTTPStatus.OK


def test_chat_completion_request_omits_default_session_id_from_backend_payload():
    request = ChatCompletionRequest(model='gpt-4o-mini', messages='hi')

    payload = request.model_dump(exclude_none=True)

    assert 'session_id' not in payload


def test_chat_completion_request_allows_string_session_id():
    request = ChatCompletionRequest(model='gpt-4o-mini', messages='hi', session_id='session-123')

    payload = request.model_dump(exclude_none=True)

    assert payload['session_id'] == 'session-123'


def test_completion_request_allows_integer_session_id():
    request = CompletionRequest(model='gpt-4o-mini', prompt='hi', session_id=123)

    payload = request.model_dump(exclude_none=True)

    assert payload['session_id'] == 123