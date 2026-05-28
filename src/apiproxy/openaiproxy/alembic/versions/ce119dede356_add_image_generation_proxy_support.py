"""add image generation proxy support

Revision ID: ce119dede356
Revises: 8b1f6d4c2e90
Create Date: 2026-05-27 21:04:55.054138

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ce119dede356'
down_revision: Union[str, None] = '8b1f6d4c2e90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _postgres_enum_exists(enum_name: str) -> bool:
    """Check whether a PostgreSQL enum type exists."""
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return False
    query = sa.text(
        "SELECT EXISTS (SELECT 1 FROM pg_type WHERE typname = :enum_name)"
    )
    return bool(bind.execute(query, {'enum_name': enum_name}).scalar())


def upgrade() -> None:
    """Add image-generation related enum values for existing PostgreSQL databases."""
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    if _postgres_enum_exists('modeltype'):
        bind.execute(sa.text("ALTER TYPE modeltype ADD VALUE IF NOT EXISTS 'image-generation'"))

    if _postgres_enum_exists('requestaction'):
        bind.execute(sa.text("ALTER TYPE requestaction ADD VALUE IF NOT EXISTS 'images_edits'"))
        bind.execute(sa.text("ALTER TYPE requestaction ADD VALUE IF NOT EXISTS 'images_generations'"))
        bind.execute(sa.text("ALTER TYPE requestaction ADD VALUE IF NOT EXISTS 'images_variations'"))


def downgrade() -> None:
    """Downgrade is a no-op because PostgreSQL enum values are not removed in place."""
