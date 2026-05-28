"""add responses request action

Revision ID: 4a8d0d5dbf12
Revises: 11b4c0f9c6d2
Create Date: 2026-05-28 15:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4a8d0d5dbf12'
down_revision: Union[str, None] = '11b4c0f9c6d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the responses action enum for existing PostgreSQL databases."""
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    bind.execute(sa.text("ALTER TYPE requestaction ADD VALUE IF NOT EXISTS 'responses'"))


def downgrade() -> None:
    """Downgrade is a no-op because PostgreSQL enum values are not removed in place."""