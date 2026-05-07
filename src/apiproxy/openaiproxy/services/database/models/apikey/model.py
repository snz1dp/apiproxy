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
from typing import List, Optional
from uuid import UUID, uuid4
from openaiproxy.utils.timezone import current_timezone
from sqlalchemy import BigInteger, ForeignKeyConstraint, Integer, JSON, String, UniqueConstraint
from sqlmodel import Text, Column, DateTime, Field, SQLModel

class ApiKeyBase(SQLModel):
    """API Key base model."""

    name: str = Field(sa_column=Column(Text, index=True, nullable=False))
    """API Key name."""

    description: Optional[str] = Field(default=None, sa_column=Column(Text, index=False, nullable=True))
    """API Key description."""

    allowed_models: Optional[List[str]] = Field(default=None, sa_column=Column(JSON, nullable=True))
    """Token-level allowed model whitelist. Empty or null means unrestricted."""

class ApiKey(ApiKeyBase, table=True):
    """API Key model."""

    __tablename__ = "openaiapi_apikeys"

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    """API ID"""

    key: Optional[str] = Field(default=None, sa_column=Column(String(256), index=True, nullable=True))
    """Legacy encrypted API Key string for backward compatibility only."""

    key_hash: str = Field(sa_column=Column(String(64), index=True, nullable=False))
    """Non-reversible API Key hash used for authentication lookup."""

    key_prefix: Optional[str] = Field(default=None, sa_column=Column(String(16), index=True, nullable=True))
    """Short key prefix for audit tracing only; never used for authentication."""

    key_version: int = Field(default=2, sa_column=Column(Integer, index=True, nullable=False, server_default="2"))
    """Token/key protocol version. 1 for legacy encrypted token, 2 for hash-based token."""

    ownerapp_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String(40), index=True, nullable=False)
    )
    """Associated application ID."""

    created_at: datetime = Field(
        default_factory=current_timezone,
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    """API Key creation timestamp."""

    enabled: Optional[bool] = Field(default=True, nullable=True, index=True)
    """是否启用"""

    expires_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    """过期时间"""

    __table_args__ = (
        UniqueConstraint("ownerapp_id", "key", name="uix_openaiapi_apikeys_key"),
        UniqueConstraint("ownerapp_id", "key_hash", name="uix_openaiapi_apikeys_key_hash"),
    )


class ApiKeyQuota(SQLModel, table=True):
    """API 密钥配额信息（充值单据形式）。"""

    __tablename__ = "openaiapi_apikey_quotas"

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    """配额记录ID"""

    api_key_id: UUID = Field(nullable=False, index=True)
    """关联的 API 密钥ID"""

    order_id: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True, index=True),
    )
    """外部充值单ID，用于标识配额来源"""

    call_limit: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
    )
    """允许的最大调用次数，空值表示不限"""

    call_used: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default='0'),
    )
    """已使用的调用次数"""

    total_tokens_limit: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
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
    """过期时间（软删除标记）"""

    __table_args__ = (
        UniqueConstraint(
            'api_key_id',
            'order_id',
            name='uix_openaiapi_apikey_quota_key_order',
        ),
        ForeignKeyConstraint(
            ['api_key_id'], ['openaiapi_apikeys.id'],
            name='openaiapi_apikey_quota_key_fkey',
        ),
    )


class ApiKeyQuotaUsage(SQLModel, table=True):
    """API 密钥配额使用记录。"""

    __tablename__ = "openaiapi_apikey_quota_usage"

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    """使用记录ID"""

    quota_id: UUID = Field(nullable=False, index=True)
    """关联的配额记录ID"""

    api_key_id: UUID = Field(nullable=False, index=True)
    """关联的 API 密钥ID"""

    proxy_id: Optional[UUID] = Field(default=None, index=True)
    """关联的代理实例ID"""

    nodelog_id: Optional[UUID] = Field(default=None, index=True)
    """关联的节点请求日志ID"""

    ownerapp_id: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True, index=True),
    )
    """所属应用ID"""

    model_name: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True, index=True),
    )
    """模型名称"""

    request_action: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True, index=True),
    )
    """请求类型"""

    call_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default='0'),
    )
    """本次记录消耗的调用次数"""

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
        ForeignKeyConstraint(
            ['quota_id'], ['openaiapi_apikey_quotas.id'],
            name='openaiapi_apikey_quota_usage_quota_fkey',
        ),
        ForeignKeyConstraint(
            ['api_key_id'], ['openaiapi_apikeys.id'],
            name='openaiapi_apikey_quota_usage_key_fkey',
        ),
        ForeignKeyConstraint(
            ['proxy_id'], ['openaiapi_proxy.id'],
            name='openaiapi_apikey_quota_usage_proxy_fkey',
        ),
        ForeignKeyConstraint(
            ['nodelog_id'], ['openaiapi_nodelogs.id'],
            name='openaiapi_apikey_quota_usage_log_fkey',
        ),
    )
