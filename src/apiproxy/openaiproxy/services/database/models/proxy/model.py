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
from typing import Optional
from uuid import UUID, uuid4
from openaiproxy.utils.timezone import current_timezone
from sqlalchemy import (
    VARCHAR, Boolean, Column, DateTime, Text, func,
    ForeignKeyConstraint, UniqueConstraint, text
)
from sqlmodel import Field, SQLModel
from enum import Enum

class ProxyInstance(SQLModel, table=True):
    """代理实例"""

    __tablename__ = "openaiapi_proxy"

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    """代理实例ID"""

    instance_name: str = Field(sa_column=Column(Text, nullable=False, index=True))
    """代理实例名称"""

    instance_ip: str = Field(sa_column=Column(Text, nullable=False, index=True))
    """实例IP地址"""

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default_factory=lambda: datetime.now(current_timezone())
    )
    """创建时间"""

    updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default_factory=lambda: datetime.now(current_timezone())
    )
    """更新时间"""

    process_id: Optional[str] = Field(
        max_length=30,
        sa_column=Column(VARCHAR, index=True, nullable=True, server_default=func.pg_backend_pid()),
    )
    """处理进程ID"""

class ProxyNodeStatus(SQLModel, table=True):
    """Node status model for database table."""

    __tablename__ = "openaiapi_status"

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    """状态ID"""

    node_id: UUID = Field(nullable=False, index=True)
    """节点ID"""

    proxy_id: Optional[UUID] = Field(nullable=True, index=True)
    """代理实例ID"""

    unfinished: int = Field(default=0, nullable=False)
    """未完成请求数"""

    latency: float = Field(default=0.0, nullable=False)
    """最后耗时，单位秒"""

    speed: float = Field(default=-1, nullable=False)
    """处理速度，单位请求数/秒"""

    avaiaible: bool = Field(default=True, nullable=False, index=True)
    """节点可用状态"""

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default_factory=lambda: datetime.now(current_timezone())
    )
    """创建时间"""

    updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default_factory=lambda: datetime.now(current_timezone())
    )
    """更新时间"""

    __table_args__ = (
        UniqueConstraint("node_id", "proxy_id", name="uix_openaiapi_status_node_proxy"),
        ForeignKeyConstraint(["node_id"], ["openaiapi_nodes.id"], name="openaiapi_status_node_fkey"),
        ForeignKeyConstraint(["proxy_id"], ["openaiapi_proxy.id"], name="openaiapi_status_proxy_fkey"),
    )

class RequestAction(Enum):
    """日志类型枚举"""

    completions = "completions"

    embeddings = "embeddings"

    healthcheck = "healthcheck"

    rerankdocs = "rerankdocs"

class ProxyNodeStatusLog(SQLModel, table=True):
    """节点请求纪录."""

    __tablename__ = "openaiapi_nodelogs"

    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    """日志ID"""

    node_id: UUID = Field(nullable=False, index=True)
    """节点ID"""

    proxy_id: UUID = Field(nullable=False, index=True)
    """代理ID"""

    status_id: UUID = Field(nullable=False, index=True)
    """状态ID"""

    ownerapp_id: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, index=True, nullable=True)
    )
    """所属应用ID"""

    action: RequestAction = Field(default=RequestAction.completions, nullable=False, index=True)
    """日志类型"""

    model_name: str = Field(sa_column=Column(Text, nullable=True, index=True))
    """模型名称"""

    start_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default_factory=lambda: datetime.now(current_timezone())
    )
    """时间戳"""

    end_at: Optional[datetime] = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default_factory=lambda: datetime.now(current_timezone())
    )
    """结束时间戳"""

    latency: float = Field(default=0.0, nullable=False)
    """延迟时间，单位秒"""

    stream: bool = Field(default=False, nullable=False, index=True)
    """是否为流式请求"""

    request_data: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True)
    )
    """请求数据"""

    request_tokens: int = Field(default=0, nullable=False)
    """请求令牌数"""

    response_data: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True)
    )
    """响应数据"""

    response_tokens: int = Field(default=0, nullable=False)
    """响应令牌数"""

    total_tokens: int = Field(default=0, nullable=False)
    """总令牌消耗数"""

    error: bool = Field(default=False,nullable=False, index=True)
    """是否发生错误"""

    error_message: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True)
    )
    """错误信息"""

    error_stack: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True)
    )
    """错误堆栈"""

    __table_args__ = (
        ForeignKeyConstraint(["node_id"], ["openaiapi_nodes.id"], name="openaiapi_nodelogs_node_fkey"),
    )
