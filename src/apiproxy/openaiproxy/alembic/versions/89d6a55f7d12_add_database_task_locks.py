"""add database task locks

Revision ID: 89d6a55f7d12
Revises: 5d2d9d6e8f31
Create Date: 2026-04-22 00:10:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '89d6a55f7d12'
down_revision: Union[str, None] = '5d2d9d6e8f31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create database task lock table for cross-instance scheduled jobs."""

    op.create_table(
        'openaiapi_task_locks',
        sa.Column('id', sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column('task_name', sa.Text(), nullable=False),
        sa.Column('owner_token', sa.Text(), nullable=False),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_name', name='uix_openaiapi_task_locks_task_name'),
    )
    op.create_index('ix_openaiapi_task_locks_task_name', 'openaiapi_task_locks', ['task_name'], unique=False)
    op.create_index('ix_openaiapi_task_locks_owner_token', 'openaiapi_task_locks', ['owner_token'], unique=False)
    op.create_index('ix_openaiapi_task_locks_lease_until', 'openaiapi_task_locks', ['lease_until'], unique=False)


def downgrade() -> None:
    """Drop database task lock table."""

    op.drop_index('ix_openaiapi_task_locks_lease_until', table_name='openaiapi_task_locks')
    op.drop_index('ix_openaiapi_task_locks_owner_token', table_name='openaiapi_task_locks')
    op.drop_index('ix_openaiapi_task_locks_task_name', table_name='openaiapi_task_locks')
    op.drop_table('openaiapi_task_locks')