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

import time
from datetime import datetime
import shortuuid
from typing import Optional, Any, Dict, List, Literal, Union, Generic, TypeVar
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from openaiproxy.services.database.models.node.model import ModelType

T = TypeVar('T')

class ModelPermission(BaseModel):
    """Model permissions."""
    id: str = Field(default_factory=lambda: f'modelperm-{shortuuid.random()}')
    object: str = 'model_permission'
    created: int = Field(default_factory=lambda: int(time.time()))
    allow_create_engine: bool = False
    allow_sampling: bool = True
    allow_logprobs: bool = True
    allow_search_indices: bool = True
    allow_view: bool = True
    allow_fine_tuning: bool = False
    organization: str = '*'
    group: Optional[str] = None
    is_blocking: bool = False


class ModelCard(BaseModel):
    """Model cards."""
    id: str
    object: str = 'model'
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = 'apiproxy'
    root: Optional[str] = None
    parent: Optional[str] = None
    permission: List[ModelPermission] = []


class ModelList(BaseModel):
    """Model list consists of model cards."""
    object: str = 'list'
    data: List[ModelCard] = []


class UsageInfo(BaseModel):
    """Usage information."""
    prompt_tokens: int = 0
    total_tokens: int = 0
    completion_tokens: Optional[int] = 0


class Function(BaseModel):
    """Function descriptions."""
    description: Optional[str] = Field(default=None, examples=[None])
    name: str
    parameters: Optional[object] = None


class Tool(BaseModel):
    """Function wrapper."""
    type: str = Field(default='function', examples=['function'])
    function: Function


class ToolChoiceFuncName(BaseModel):
    """The name of tool choice function."""
    name: str


class ToolChoice(BaseModel):
    """The tool choice definition."""
    function: ToolChoiceFuncName
    type: Literal['function'] = Field(default='function',
                                      examples=['function'])


class StreamOptions(BaseModel):
    """The stream options."""
    include_usage: Optional[bool] = False


class JsonSchema(BaseModel):
    name: str
    # description is not used since it depends on model
    description: Optional[str] = None
    # use alias since pydantic does not support the OpenAI key `schema`
    json_schema: Optional[Dict[str, Any]] = Field(
        default=None,
        alias='schema',
        examples=[None]
    )
    # strict is not used
    strict: Optional[bool] = False


class ResponseFormat(BaseModel):
    # regex_schema is extended by apiproxy to support regex output
    type: Literal['text', 'json_object', 'json_schema', 'regex_schema']
    json_schema: Optional[JsonSchema] = None
    regex_schema: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    """Chat completion request."""
    model: str
    # yapf: disable
    messages: Union[str, List[Dict[str, Any]]] = Field(
        examples=[[{'role': 'user', 'content': 'hi'}]]
    )  # noqa
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0
    tools: Optional[List[Tool]] = Field(default=None, examples=[None])
    tool_choice: Optional[Union[
        ToolChoice, Literal['auto', 'required', 'none']
    ]] = Field(default=None, examples=['none'])  # noqa
    logprobs: Optional[bool] = False
    top_logprobs: Optional[int] = None
    n: Optional[int] = 1
    logit_bias: Optional[Dict[str, float]] = Field(default=None, examples=[None])  # noqa
    max_tokens: Optional[int] = Field(default=None, examples=[None])
    stop: Optional[Union[str, List[str]]] = Field(default=[], examples=[None])  # noqa
    # yapf: enable
    stream: Optional[bool] = False
    stream_options: Optional[StreamOptions] = Field(default=None,
                                                    examples=[None])
    presence_penalty: Optional[float] = 0.0
    frequency_penalty: Optional[float] = 0.0
    user: Optional[str] = None
    response_format: Optional[ResponseFormat] = Field(default=None,
                                                      examples=[None])  # noqa
    # additional argument of apiproxy
    repetition_penalty: Optional[float] = 1.0
    session_id: Optional[int] = -1
    ignore_eos: Optional[bool] = False
    skip_special_tokens: Optional[bool] = True
    spaces_between_special_tokens: Optional[bool] = True
    top_k: Optional[int] = 40
    seed: Optional[int] = None
    min_new_tokens: Optional[int] = Field(default=None, examples=[None])
    min_p: float = 0.0


class FunctionResponse(BaseModel):
    """Function response."""
    name: str
    arguments: str


class ToolCall(BaseModel):
    """Tool call response."""
    id: str
    type: Literal['function'] = 'function'
    function: FunctionResponse


class ChatMessage(BaseModel):
    """Chat messages."""
    role: str
    content: str
    tool_calls: Optional[List[ToolCall]] = Field(default=None, examples=[None])


class LogProbs(BaseModel):
    text_offset: List[int] = Field(default_factory=list)
    token_logprobs: List[Optional[float]] = Field(default_factory=list)
    tokens: List[str] = Field(default_factory=list)
    top_logprobs: Optional[List[Optional[Dict[str, float]]]] = None


class TopLogprob(BaseModel):
    token: str
    bytes: Optional[List[int]] = None
    logprob: float


class ChatCompletionTokenLogprob(BaseModel):
    token: str
    bytes: Optional[List[int]] = None
    logprob: float
    top_logprobs: List[TopLogprob]


class ChoiceLogprobs(BaseModel):
    content: Optional[List[ChatCompletionTokenLogprob]] = None


class ChatCompletionResponseChoice(BaseModel):
    """Chat completion response choices."""
    index: int
    message: ChatMessage
    logprobs: Optional[ChoiceLogprobs] = None
    finish_reason: Optional[Literal['stop', 'length', 'tool_calls',
                                    'error']] = None


class ChatCompletionResponse(BaseModel):
    """Chat completion response."""
    id: str = Field(default_factory=lambda: f'chatcmpl-{shortuuid.random()}')
    object: str = 'chat.completion'
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionResponseChoice]
    usage: UsageInfo


class DeltaMessage(BaseModel):
    """Delta messages."""
    role: Optional[str] = None
    content: Optional[str] = None


class ChatCompletionResponseStreamChoice(BaseModel):
    """Chat completion response stream choice."""
    index: int
    delta: DeltaMessage
    logprobs: Optional[ChoiceLogprobs] = None
    finish_reason: Optional[Literal['stop', 'length']] = None


class ChatCompletionStreamResponse(BaseModel):
    """Chat completion stream response."""
    id: str = Field(default_factory=lambda: f'chatcmpl-{shortuuid.random()}')
    object: str = 'chat.completion.chunk'
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionResponseStreamChoice]
    usage: Optional[UsageInfo] = None


class CompletionRequest(BaseModel):
    """Completion request."""
    model: str
    prompt: Union[str, List[Any]]
    suffix: Optional[str] = None
    temperature: Optional[float] = 0.7
    n: Optional[int] = 1
    logprobs: Optional[int] = None
    max_tokens: Optional[int] = 16
    stop: Optional[Union[str, List[str]]] = Field(
        default=None,
        examples=[None]
    )
    stream: Optional[bool] = False
    stream_options: Optional[StreamOptions] = Field(
        default=None,
        examples=[None]
    )
    top_p: Optional[float] = 1.0
    echo: Optional[bool] = False
    presence_penalty: Optional[float] = 0.0
    frequency_penalty: Optional[float] = 0.0
    user: Optional[str] = None
    # additional argument of apiproxy
    repetition_penalty: Optional[float] = 1.0
    session_id: Optional[int] = -1
    ignore_eos: Optional[bool] = False
    skip_special_tokens: Optional[bool] = True
    spaces_between_special_tokens: Optional[bool] = True
    top_k: Optional[int] = 40  # for opencompass
    seed: Optional[int] = None


class CompletionResponseChoice(BaseModel):
    """Completion response choices."""
    index: int
    text: str
    logprobs: Optional[LogProbs] = None
    finish_reason: Optional[Literal['stop', 'length']] = None


class CompletionResponse(BaseModel):
    """Completion response."""
    id: str = Field(default_factory=lambda: f'cmpl-{shortuuid.random()}')
    object: str = 'text_completion'
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[CompletionResponseChoice]
    usage: UsageInfo


class CompletionResponseStreamChoice(BaseModel):
    """Completion response stream choice."""
    index: int
    text: str
    logprobs: Optional[LogProbs] = None
    finish_reason: Optional[Literal['stop', 'length']] = None


class CompletionStreamResponse(BaseModel):
    """Completion stream response."""
    id: str = Field(default_factory=lambda: f'cmpl-{shortuuid.random()}')
    object: str = 'text_completion'
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[CompletionResponseStreamChoice]
    usage: Optional[UsageInfo] = None


class EmbeddingsRequest(BaseModel):
    """Embedding request."""
    model: str = None
    input: Union[str, List[str]]
    user: Optional[str] = None


class EmbeddingsResponse(BaseModel):
    """Embedding response."""
    object: str = 'list'
    data: List[Dict[str, Any]]
    model: str
    usage: UsageInfo


class RerankRequest(BaseModel):
    """Rerank request.

    This is a lightweight schema for rerank endpoints. It mirrors the common
    fields used by OpenAI-style rerank APIs: a `model` identifier, a
    `query` (string or list) and a list of `documents` to be reranked.
    """
    model: str = None
    query: Union[str, List[Any]]
    documents: Optional[List[Any]] = None
    user: Optional[str] = None


class EncodeRequest(BaseModel):
    """Encode request."""
    input: Union[str, List[str]]
    do_preprocess: Optional[bool] = False
    add_bos: Optional[bool] = True


class EncodeResponse(BaseModel):
    """Encode response."""
    input_ids: Union[List[int], List[List[int]]]
    length: Union[int, List[int]]


class GenerateRequest(BaseModel):
    """Generate request."""
    prompt: Union[str, List[Dict[str, Any]]]
    image_url: Optional[Union[str, List[str]]] = Field(
        default=None,
        examples=[None]
    )
    session_id: int = -1
    interactive_mode: bool = False
    stream: bool = False
    stop: Optional[Union[str, List[str]]] = Field(
        default=None,
        examples=[None]
    )
    request_output_len: Optional[int] = Field(
        default=None,
        examples=[None]
    )
    top_p: float = 0.8
    top_k: int = 40
    temperature: float = 0.8
    repetition_penalty: float = 1.0
    ignore_eos: bool = False
    skip_special_tokens: Optional[bool] = True
    cancel: Optional[bool] = False  # cancel a responding request
    adapter_name: Optional[str] = Field(default=None, examples=[None])
    seed: Optional[int] = None
    min_new_tokens: Optional[int] = Field(default=None, examples=[None])
    min_p: float = 0.0

class GenerateResponse(BaseModel):
    """Generate response."""
    text: str
    tokens: int
    input_tokens: int
    history_tokens: int
    finish_reason: Optional[Literal['stop', 'length']] = None

class PageResponse(BaseModel, Generic[T]):
    """分页响应"""
    offset: int = 0
    total: int = 0
    data: List[T] = Field(default_factory=list)

class OpenAINodeUpdate(BaseModel):
    """OpenAI兼容服务节点更新参数"""
    name: Optional[str] = None
    api_key: Optional[str] = None
    description: Optional[str] = None
    modify_user: Optional[str] = None
    enabled: Optional[bool] = None
    verify: Optional[bool] = True

class OpenAINodeModelUpdate(BaseModel):
    """OpenAI兼容服务节点模型更新参数"""
    enabled: Optional[bool] = None

class ApiKeyCreate(BaseModel):
    """API Key创建参数"""
    ownerapp_id: str
    name: str
    description: Optional[str] = None
    expires_at: Optional[datetime] = None

class ApiKeyUpdate(BaseModel):
    """API Key更新参数"""
    name: Optional[str] = None
    description: Optional[str] = None
    expires_at: Optional[datetime] = None
    enabled: Optional[bool] = None

class ApiKeyRead(BaseModel):
    """API Key读取参数"""
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    ownerapp_id: Optional[str]
    name: str
    description: Optional[str]
    created_at: datetime
    expires_at: Optional[datetime]


class ApiKeyCreateResponse(ApiKeyRead):
    """API Key创建响应，包含一次性返回的密钥令牌"""
    token: str


class CreateOpenAINode(BaseModel):
    """OpenAI兼容服务节点响应参数"""
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: Optional[str]
    url: str
    api_key: Optional[str] = None
    description: Optional[str]
    created_at: datetime
    create_user: Optional[str]
    updated_at: datetime
    modify_user: Optional[str]
    enabled: bool
    verify: Optional[bool] = True

class OpenAINodeReponse(BaseModel):
    """OpenAI兼容服务节点响应参数"""
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: Optional[str]
    url: str
    description: Optional[str]
    created_at: datetime
    create_user: Optional[str]
    updated_at: datetime
    modify_user: Optional[str]
    enabled: bool
