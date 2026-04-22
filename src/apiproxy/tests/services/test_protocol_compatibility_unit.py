import threading

from openaiproxy.api.v1.protocol_adapters import (
    anthropic_messages_to_openai_request,
    openai_chat_request_to_anthropic_request,
    openai_response_to_anthropic_payload,
    anthropic_response_to_openai_payload,
)
from openaiproxy.services.database.models.node.model import ProtocolType
from openaiproxy.services.nodeproxy.constants import Strategy
from openaiproxy.services.nodeproxy.schemas import Status
from openaiproxy.services.nodeproxy.service import NodeProxyService


def _build_protocol_service() -> NodeProxyService:
    service = object.__new__(NodeProxyService)
    service._lock = threading.RLock()
    service.nodes = {}
    service.snode = {}
    service.strategy = Strategy.RANDOM
    service._quota_exhausted_models = {}
    service._quota_exhaustion_ttl = 300
    return service


def test_openai_chat_request_can_convert_to_anthropic_messages():
    request_payload = {
        'model': 'claude-test',
        'messages': [
            {'role': 'system', 'content': 'you are helpful'},
            {'role': 'user', 'content': 'hello'},
        ],
        'max_tokens': 128,
        'stream': True,
    }

    converted = openai_chat_request_to_anthropic_request(request_payload)

    assert converted['model'] == 'claude-test'
    assert converted['system'] == 'you are helpful'
    assert converted['messages'][0]['role'] == 'user'
    assert converted['messages'][0]['content'][0]['text'] == 'hello'
    assert converted['stream'] is True


def test_anthropic_messages_can_convert_to_openai_chat_request():
    request_payload = {
        'model': 'gpt-test',
        'system': 'follow instructions',
        'messages': [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'say hi'}]},
        ],
        'max_tokens': 64,
    }

    converted = anthropic_messages_to_openai_request(request_payload)

    assert converted['model'] == 'gpt-test'
    assert converted['messages'][0]['role'] == 'system'
    assert converted['messages'][0]['content'] == 'follow instructions'
    assert converted['messages'][1]['role'] == 'user'
    assert converted['messages'][1]['content'] == 'say hi'


def test_openai_and_anthropic_response_conversion_round_trip_core_fields():
    openai_payload = {
        'id': 'chatcmpl-test',
        'model': 'gpt-test',
        'choices': [
            {
                'index': 0,
                'message': {'role': 'assistant', 'content': 'hello back'},
                'finish_reason': 'stop',
            }
        ],
        'usage': {'prompt_tokens': 12, 'completion_tokens': 8, 'total_tokens': 20},
    }

    anthropic_payload = openai_response_to_anthropic_payload(openai_payload, 'gpt-test')
    round_trip_payload = anthropic_response_to_openai_payload(anthropic_payload, 'gpt-test')

    assert anthropic_payload['type'] == 'message'
    assert anthropic_payload['usage']['input_tokens'] == 12
    assert anthropic_payload['usage']['output_tokens'] == 8
    assert round_trip_payload['choices'][0]['message']['content'] == 'hello back'
    assert round_trip_payload['usage']['prompt_tokens'] == 12
    assert round_trip_payload['usage']['completion_tokens'] == 8


def test_nodeproxy_prefers_same_protocol_before_cross_protocol_fallback():
    service = _build_protocol_service()
    service.nodes = {
        'http://openai-node': Status(
            models=['demo-model'],
            types=['chat'],
            protocol_type=ProtocolType.openai,
        ),
        'http://anthropic-node': Status(
            models=['demo-model'],
            types=['chat'],
            protocol_type=ProtocolType.anthropic,
        ),
    }
    service.snode = dict(service.nodes)

    selected_for_anthropic = service.get_node_url(
        'demo-model',
        'chat',
        request_protocol=ProtocolType.anthropic,
        allow_cross_protocol=True,
    )
    selected_for_openai = service.get_node_url(
        'demo-model',
        'chat',
        request_protocol=ProtocolType.openai,
        allow_cross_protocol=True,
    )

    assert selected_for_anthropic == 'http://anthropic-node'
    assert selected_for_openai == 'http://openai-node'


def test_nodeproxy_lists_models_visible_to_anthropic_protocol_with_fallback():
    service = _build_protocol_service()
    service.snode = {
        'http://openai-node': Status(
            models=['openai-model'],
            types=['chat'],
            protocol_type=ProtocolType.openai,
        ),
        'http://anthropic-node': Status(
            models=['anthropic-model'],
            types=['chat'],
            protocol_type=ProtocolType.anthropic,
        ),
    }

    models = service.list_models_for_protocol(
        ProtocolType.anthropic,
        allow_cross_protocol=True,
    )

    assert 'anthropic-model' in models
    assert 'openai-model' in models