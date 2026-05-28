"""add video generation proxy support

Revision ID: 84af435ce6f4
Revises: ce119dede356
Create Date: 2026-05-27 22:32:55.131416

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '84af435ce6f4'
down_revision: Union[str, None] = 'ce119dede356'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add video-generation related enum values for existing PostgreSQL databases."""
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    bind.execute(sa.text("ALTER TYPE modeltype ADD VALUE IF NOT EXISTS 'video-generation'"))
    bind.execute(sa.text("ALTER TYPE requestaction ADD VALUE IF NOT EXISTS 'videos_cancel'"))
    bind.execute(sa.text("ALTER TYPE requestaction ADD VALUE IF NOT EXISTS 'videos_content'"))
    bind.execute(sa.text("ALTER TYPE requestaction ADD VALUE IF NOT EXISTS 'videos_generations'"))
    bind.execute(sa.text("ALTER TYPE requestaction ADD VALUE IF NOT EXISTS 'videos_retrieve'"))


def downgrade() -> None:
    """Downgrade is a no-op because PostgreSQL enum values are not removed in place."""
