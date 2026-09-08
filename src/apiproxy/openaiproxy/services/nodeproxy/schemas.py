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

from collections import deque
from typing import Deque, Dict, List, Optional
from uuid import UUID
from pydantic import BaseModel, Field
from .constants import LATENCY_DEQUE_LEN
from openaiproxy.services.database.models.node.model import ProtocolType


class NodeApiKeyEntry(BaseModel):
    """节点API密钥运行时条目（内存中持有解密后的明文密钥）。"""

    api_key_id: UUID
    """密钥记录ID（写入请求日志用）"""

    api_key: str
    """解密后的明文密钥"""

    priority: int = 1
    """优先级权重（正整数，越大越优先，0不参与选择）"""

    max_tokens: Optional[int] = None
    """最大使用Tokens数，None表示不限制"""

    tokens_used: int = 0
    """已使用Tokens数（刷新周期内的快照值）"""


class Status(BaseModel):
    """Status protocol consists of models' information."""
    models: List[str] = Field(default_factory=list, examples=[[]])
    types: List[str] = Field(default_factory=list, examples=[[]])
    unfinished: int = 0
    latency: Deque = Field(
        default=deque(maxlen=LATENCY_DEQUE_LEN),
        examples=[[]]
    )
    speed: Optional[float] = Field(default=None, examples=[None])

    auto_v1_api: Optional[bool] = Field(default=True, examples=[False])
    """是否自动添加/v1前缀到请求路径"""

    avaiaible: Optional[bool] = Field(default=True, examples=[False])
    api_key: Optional[str] = Field(default=None, examples=[None])
    # The api_key is used to access the node, if the node requires
    api_keys: List[NodeApiKeyEntry] = Field(default_factory=list, examples=[[]])
    # 节点独立API密钥列表；为空时回退使用 api_key（向后兼容）
    protocol_type: ProtocolType = Field(default=ProtocolType.openai, examples=['openai'])
    request_proxy_url: Optional[str] = Field(default=None, examples=[None])
    health_check: Optional[bool] = Field(default=None, examples=[True])
    # The health_check is used to check the node's health
    trusted_without_models_endpoint: Optional[bool] = Field(default=None, examples=[False])
    # The trusted_without_models_endpoint flag keeps node alive without /v1/models
    model_quota: Dict[str, Optional[bool]] = Field(default_factory=dict, examples=[{}])
    quota_exhausted_models: List[str] = Field(default_factory=list, examples=[[]])


class Node(BaseModel):
    """Node protocol consists of url and status."""
    url: str
    status: Optional[Status] = None

class ErrorResponse(BaseModel):
    """Error responses."""
    message: str
    type: str
    code: int
    param: Optional[str] = None
    object: str = 'error'
