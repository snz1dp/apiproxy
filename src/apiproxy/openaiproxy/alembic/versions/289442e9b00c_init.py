"""init

Revision ID: 289442e9b00c
Revises:
Create Date: 2025-02-25 13:17:06.607527

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.engine.reflection import Inspector
from openaiproxy.utils import migration


# revision identifiers, used by Alembic.
revision: str = '289442e9b00c'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    pass


def downgrade() -> None:
    conn = op.get_bind()
    pass
