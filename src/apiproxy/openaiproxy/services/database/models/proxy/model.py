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
from sqlalchemy import VARCHAR, Column, DateTime, ForeignKeyConstraint, Text, func
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
        sa_column=Column(DateTime(timezone=True), default=current_timezone, nullable=False)
    )
    """创建时间"""

    updated_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            default=current_timezone,
            onupdate=current_timezone,
            nullable=False,
        )
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
        sa_column=Column(DateTime(timezone=True), default=current_timezone, nullable=False)
    )
    """创建时间"""

    updated_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            default=current_timezone,
            onupdate=current_timezone,
            nullable=False,
        )
    )
    """更新时间"""

    __table_args__ = (
        ForeignKeyConstraint(["node_id"], ["openaiapi_nodes.id"], name="openaiapi_node_status_node_fkey"),
        ForeignKeyConstraint(["proxy_id"], ["openaiapi_proxy.id"], name="openaiapi_node_status_proxy_fkey"),
    )

class Action(Enum):
    """日志类型枚举"""

    request = "request"

    check = "check"

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

    action: Action = Field(default=Action.request, nullable=False, index=True)
    """日志类型"""

    model_name: str = Field(sa_column=Column(Text, nullable=True, index=True))
    """模型名称"""

    start_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), default=current_timezone, nullable=False)
    )
    """时间戳"""

    end_at: Optional[datetime] = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    """结束时间戳"""

    latency: float = Field(default=0.0, nullable=False)
    """延迟时间，单位秒"""

    token_count: int = Field(default=0, nullable=False)
    """处理的令牌数"""

    __table_args__ = (
        ForeignKeyConstraint(["node_id"], ["openaiapi_nodes.id"], name="openaiapi_nodelogs_node_fkey"),
        ForeignKeyConstraint(["proxy_id"], ["openaiapi_proxy.id"], name="openaiapi_nodelogs_proxy_fkey"),
        ForeignKeyConstraint(["status_id"], ["openaiapi_status.id"], name="openaiapi_nodelogs_status_fkey"),
    )
