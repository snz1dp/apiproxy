

from datetime import datetime
from enum import Enum
from typing import List, Optional
from uuid import UUID, uuid4
from openaiproxy.utils.timezone import current_timezone
from sqlmodel import (
    JSON, func, Text, VARCHAR, Column,
    DateTime, Field, SQLModel
)

class NodeBase(SQLModel):
    """Node protocol consists of url and status."""

    url: str = Field(sa_column=Column(Text, unique=True, index=True, nullable=False))
    """节点地址"""

    name: str = Field(sa_column=Column(Text, index=True, nullable=True))
    """节点名称"""

    description: Optional[str] = Field(default=None, sa_column=Column(Text, index=False, nullable=True))
    """节点描述"""

class NodeFeature(Enum):
    """Node feature enumeration."""
    chat_completions = "chat_completions"

    completions = "completions"

    embeddings = "embeddings"

class Node(NodeBase, SQLModel, table=True):
    """Node model for database table."""

    __tablename__ = "openaiapi_nodes"

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    """节点ID"""

    models: List[str] = Field(sa_column=Column(JSON), default=[])
    """支持的模型列表"""

    features: List[NodeFeature] = Field(sa_column=Column(JSON), default=[])
    """支持的功能列表"""

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

    enabled: Optional[bool] = Field(default=True, nullable=True, index=True)
    """是否启用"""

class NodeStatus(SQLModel, table=True):
    """Node status model for database table."""

    __tablename__ = "openaiapi_node_statuses"

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    """状态ID"""

    node_id: UUID = Field(nullable=False, index=True)
    """节点ID"""

    proxy_name: Optional[str] = Field(nullable=True, index=True)
    """代理ID"""

    status: str = Field(sa_column=Column(Text, nullable=False))
    """节点状态"""

    created_at: Optional[datetime] = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default_factory=lambda: datetime.now(current_timezone())
    )
    """创建时间"""

    checked_at: Optional[datetime] = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default_factory=lambda: datetime.now(current_timezone())
    )
    """检查时间"""

    process_id: Optional[str] = Field(
        max_length=30,
        sa_column=Column(VARCHAR, index=True, nullable=True, server_default=func.pg_backend_pid()),
    )
    """处理进程ID"""
