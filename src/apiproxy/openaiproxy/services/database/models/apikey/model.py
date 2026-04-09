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
from sqlalchemy import Integer, String, UniqueConstraint
from sqlmodel import Text, Column, DateTime, Field, SQLModel

class ApiKeyBase(SQLModel):
    """API Key base model."""

    name: str = Field(sa_column=Column(Text, index=True, nullable=False))
    """API Key name."""

    description: Optional[str] = Field(default=None, sa_column=Column(Text, index=False, nullable=True))
    """API Key description."""

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
