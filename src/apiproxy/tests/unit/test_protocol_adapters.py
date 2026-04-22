import threading

from openaiproxy.api.v1.protocol_adapters import (
    anthropic_messages_to_openai_request,
    build_anthropic_count_tokens_payload,
    openai_chat_request_to_anthropic_request,
    openai_response_to_anthropic_payload,
)
from openaiproxy.services.database.models.node.model import ProtocolType
from openaiproxy.services.nodeproxy.constants import Strategy
from openaiproxy.services.nodeproxy.schemas import Status
from openaiproxy.services.nodeproxy.service import NodeProxyService


def _build_protocol_service() -> NodeProxyService:
    service = object.__new__(NodeProxyService)
    service._lock = threading.RLock()
    service.strategy = Strategy.RANDOM
    service.nodes = {}
    service.snode = {}
    service._quota_exhausted_models = {}
    service._quota_exhaustion_ttl = 300
    service._is_node_model_quota_exhausted = lambda *args, **kwargs: False
    return service


def test_openai_chat_request_to_anthropic_request_maps_system_and_stop_sequences():
    payload = openai_chat_request_to_anthropic_request(
        {
            'model': 'demo-model',
            'messages': [
                {'role': 'system', 'content': 'you are helpful'},
                {'role': 'user', 'content': 'hello'},
            ],
            'stop': ['END'],
            'stream': True,
            'max_tokens': 128,
        }
    )

    assert payload['model'] == 'demo-model'
    assert payload['system'] == 'you are helpful'
    assert payload['messages'][0]['role'] == 'user'
    assert payload['messages'][0]['content'][0]['text'] == 'hello'
    assert payload['stop_sequences'] == ['END']
    assert payload['stream'] is True


def test_anthropic_messages_to_openai_request_flattens_system_and_messages():
    payload = anthropic_messages_to_openai_request(
        {
            'model': 'demo-model',
            'system': 'system prompt',
            'messages': [
                {'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]},
                {'role': 'assistant', 'content': [{'type': 'text', 'text': 'world'}]},
            ],
            'stop_sequences': ['STOP'],
        }
    )

    assert payload['model'] == 'demo-model'
    assert payload['messages'][0] == {'role': 'system', 'content': 'system prompt'}
    assert payload['messages'][1] == {'role': 'user', 'content': 'hello'}
    assert payload['messages'][2] == {'role': 'assistant', 'content': 'world'}
    assert payload['stop'] == ['STOP']


def test_openai_response_to_anthropic_payload_maps_usage_and_text():
    payload = openai_response_to_anthropic_payload(
        {
            'id': 'chatcmpl-1',
            'model': 'demo-model',
            'choices': [
                {
                    'index': 0,
                    'message': {'role': 'assistant', 'content': 'hello world'},
                    'finish_reason': 'stop',
                }
            ],
            'usage': {
                'prompt_tokens': 10,
                'completion_tokens': 4,
            },
        },
        'demo-model',
    )

    assert payload['type'] == 'message'
    assert payload['content'][0]['text'] == 'hello world'
    assert payload['usage']['input_tokens'] == 10
    assert payload['usage']['output_tokens'] == 4
    assert payload['stop_reason'] == 'end_turn'


def test_build_anthropic_count_tokens_payload_estimates_input_tokens():
    payload = build_anthropic_count_tokens_payload(
        {
            'system': 'system prompt',
            'messages': [
                {'role': 'user', 'content': [{'type': 'text', 'text': 'hello world'}]},
            ],
        }
    )

    assert payload['input_tokens'] > 0


def test_list_models_for_protocol_prefers_direct_and_includes_cross_protocol_when_enabled():
    service = _build_protocol_service()
    service.snode = {
        'http://openai-node': Status(models=['gpt-openai'], types=['chat'], protocol_type=ProtocolType.openai),
        'http://anthropic-node': Status(models=['claude-demo'], types=['chat'], protocol_type=ProtocolType.anthropic),
        'http://both-node': Status(models=['shared-model'], types=['chat'], protocol_type=ProtocolType.both),
    }

    anthropic_models = service.list_models_for_protocol(ProtocolType.anthropic, allow_cross_protocol=True)
    openai_models = service.list_models_for_protocol(ProtocolType.openai, allow_cross_protocol=False)

    assert 'claude-demo' in anthropic_models
    assert 'gpt-openai' in anthropic_models
    assert 'shared-model' in anthropic_models
    assert 'claude-demo' not in openai_models
    assert 'shared-model' in openai_models


def test_get_node_url_falls_back_to_cross_protocol_node_when_direct_node_missing():
    service = _build_protocol_service()
    service.nodes = {
        'http://anthropic-node': Status(models=['fallback-model'], types=['chat'], protocol_type=ProtocolType.anthropic, speed=1.0),
    }
    service.snode = dict(service.nodes)

    node_url = service.get_node_url(
        'fallback-model',
        'chat',
        request_protocol=ProtocolType.openai,
        allow_cross_protocol=True,
    )

    assert node_url == 'http://anthropic-node'