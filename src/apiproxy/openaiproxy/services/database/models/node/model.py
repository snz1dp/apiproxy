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
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint
from sqlmodel import Text, Column, DateTime, Field, SQLModel

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
