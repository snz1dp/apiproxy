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

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from openaiproxy.utils.timezone import current_timezone
from sqlalchemy import BigInteger, ForeignKeyConstraint, UniqueConstraint
from sqlmodel import Column, DateTime, Field, SQLModel, Text

class NodeBase(SQLModel):
    """Node protocol consists of url and status."""

    url: str = Field(sa_column=Column(Text, unique=True, index=True, nullable=False))
    """节点地址"""

    name: str = Field(sa_column=Column(Text, index=True, nullable=True))
    """节点名称"""

    description: Optional[str] = Field(default=None, sa_column=Column(Text, index=False, nullable=True))
    """节点描述"""

class ModelType(Enum):
    """model type enumeration."""

    chat = "chat"

    embeddings = "embeddings"
    
    rerank = "rerank"

class Node(NodeBase, table=True):
    """OpenAI兼容服务节点"""

    __tablename__ = "openaiapi_nodes"

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    """节点ID"""

    api_key: Optional[str] = Field(default=None, nullable=True)
    """接口访问API密钥"""

    health_check: Optional[bool] = Field(default=True, nullable=False, index=True)
    """是否启用健康检查"""

    created_at: Optional[datetime] = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default_factory=lambda: datetime.now(current_timezone())
    )
    """创建时间"""

    create_user: Optional[str] = Field(default=None, nullable=True)
    """创建用户"""

    updated_at: Optional[datetime] = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default_factory=lambda: datetime.now(current_timezone())
    )
    """最后修改时间"""

    modify_user: Optional[str] = Field(default=None, nullable=True)
    """最后修改用户"""

    expired_at: Optional[datetime] = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None
    )
    """过期时间"""

    enabled: Optional[bool] = Field(default=True, nullable=True, index=True)
    """是否启用"""

class NodeModel(SQLModel, table=True):
    """OpenAI兼容服务节点模型"""

    __tablename__ = "openaiapi_models"

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    """映射ID"""

    node_id: UUID = Field(nullable=False, index=True)
    """节点ID"""

    model_name: str = Field(sa_column=Column(Text, nullable=False, index=True))
    """模型名称"""

    model_type: ModelType = Field(default=ModelType.chat, nullable=False, index=True)
    """模型类型"""

    enabled: Optional[bool] = Field(default=True, nullable=True, index=True)
    """是否启用"""

    __table_args__ = (
        UniqueConstraint('node_id', 'model_name', "model_type", name='uix_openaiapi_node_models_type'),
        ForeignKeyConstraint(["node_id"], ["openaiapi_nodes.id"], name="openaiapi_node_models_node_fkey"),
    )


class NodeModelQuota(SQLModel, table=True):
    """节点模型配额信息."""

    __tablename__ = "openaiapi_model_quotas"

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    """配额记录ID"""

    node_model_id: UUID = Field(nullable=False, index=True)
    """关联的节点模型ID"""

    order_id: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True, index=True)
    )
    """外部订单ID，用于标识配额来源"""

    call_limit: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True)
    )
    """允许的最大调用次数，空值表示不限"""

    call_used: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default='0'),
    )
    """已使用的调用次数"""

    prompt_tokens_limit: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True)
    )
    """允许的输入Tokens配额，空值表示不限"""

    prompt_tokens_used: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default='0'),
    )
    """累计消耗的输入Tokens"""

    completion_tokens_limit: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True)
    )
    """允许的输出Tokens配额，空值表示不限"""

    completion_tokens_used: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default='0'),
    )
    """累计消耗的输出Tokens"""

    total_tokens_limit: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True)
    )
    """允许的总Tokens配额，空值表示不限"""

    total_tokens_used: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default='0'),
    )
    """累计消耗的总Tokens"""

    last_reset_at: Optional[datetime] = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
    """上一次配额重置时间"""

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(current_timezone()),
    )
    """创建时间"""

    updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(current_timezone()),
    )
    """更新时间"""

    expired_at: Optional[datetime] = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
    """过期时间"""

    __table_args__ = (
        UniqueConstraint(
            'node_model_id',
            'order_id',
            name='uix_openaiapi_model_quota_model_order'
        ),
        ForeignKeyConstraint(
            ['node_model_id'], ['openaiapi_models.id'],
            name='openaiapi_model_quota_model_fkey'
        ),
    )


class NodeModelQuotaUsage(SQLModel, table=True):
    """节点模型配额使用记录."""

    __tablename__ = "openaiapi_model_quota_usage"

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    """使用记录ID"""

    quota_id: UUID = Field(nullable=False, index=True)
    """关联的配额记录ID"""

    node_id: UUID = Field(nullable=False, index=True)
    """关联的节点ID"""

    node_model_id: UUID = Field(nullable=False, index=True)
    """关联的节点模型ID"""

    proxy_id: Optional[UUID] = Field(default=None, index=True)
    """关联的代理ID"""

    nodelog_id: Optional[UUID] = Field(default=None, index=True)
    """关联的节点请求日志ID"""

    ownerapp_id: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True, index=True)
    )
    """所属应用ID"""

    request_action: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True, index=True)
    )
    """请求类型"""

    call_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default='0'),
    )
    """本次记录消耗的调用次数"""

    request_tokens: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default='0'),
    )
    """本次记录消耗的输入Tokens"""

    response_tokens: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default='0'),
    )
    """本次记录消耗的输出Tokens"""

    total_tokens: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default='0'),
    )
    """本次记录消耗的总Tokens"""

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(current_timezone()),
    )
    """创建时间"""

    updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(current_timezone()),
    )
    """更新时间"""

    __table_args__ = (
        ForeignKeyConstraint(['quota_id'], ['openaiapi_model_quotas.id'], name='openaiapi_model_quota_usage_quota_fkey'),
        ForeignKeyConstraint(['node_id'], ['openaiapi_nodes.id'], name='openaiapi_model_quota_usage_node_fkey'),
        ForeignKeyConstraint(['node_model_id'], ['openaiapi_models.id'], name='openaiapi_model_quota_usage_model_fkey'),
        ForeignKeyConstraint(['proxy_id'], ['openaiapi_proxy.id'], name='openaiapi_model_quota_usage_proxy_fkey'),
        ForeignKeyConstraint(['nodelog_id'], ['openaiapi_nodelogs.id'], name='openaiapi_model_quota_usage_log_fkey'),
    )
