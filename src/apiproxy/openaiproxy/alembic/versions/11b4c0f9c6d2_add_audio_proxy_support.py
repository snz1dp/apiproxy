"""add audio proxy support

Revision ID: 11b4c0f9c6d2
Revises: 04674948678b
Create Date: 2026-05-28 10:15:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '11b4c0f9c6d2'
down_revision: Union[str, None] = '04674948678b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add audio-related enum values for existing PostgreSQL databases."""
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    bind.execute(sa.text("ALTER TYPE modeltype ADD VALUE IF NOT EXISTS 'speech-to-text'"))
    bind.execute(sa.text("ALTER TYPE modeltype ADD VALUE IF NOT EXISTS 'text-to-speech'"))
    bind.execute(sa.text("ALTER TYPE requestaction ADD VALUE IF NOT EXISTS 'audio_speech'"))
    bind.execute(sa.text("ALTER TYPE requestaction ADD VALUE IF NOT EXISTS 'audio_transcriptions'"))
    bind.execute(sa.text("ALTER TYPE requestaction ADD VALUE IF NOT EXISTS 'audio_translations'"))


def downgrade() -> None:
    """Downgrade is a no-op because PostgreSQL enum values are not removed in place."""