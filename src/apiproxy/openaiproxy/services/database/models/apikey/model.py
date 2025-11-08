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
from sqlalchemy import ForeignKeyConstraint, String, UniqueConstraint
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

    key: str = Field(sa_column=Column(Text, index=True, nullable=False))
    """The actual API Key string."""

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

    enabled: bool = Field(
        default=True,
        sa_column=Column(nullable=False)
    )

    expires_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    """API Key expiration timestamp."""

    __table_args__ = (
        UniqueConstraint("ownerapp_id", "key", name="uix_openaiapi_apikeys_key"),
    )
