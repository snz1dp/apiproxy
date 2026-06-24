"""add auto v1 toggle to nodes

Revision ID: 9f4f5c2b1d7a
Revises: e30ff8554e54
Create Date: 2026-06-23 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9f4f5c2b1d7a'
down_revision: Union[str, None] = 'e30ff8554e54'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add node-level toggle for automatic /v1 upstream prefixing."""
    with op.batch_alter_table('openaiapi_nodes', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'auto_v1_api',
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )


def downgrade() -> None:
    """Remove node-level toggle for automatic /v1 upstream prefixing."""
    with op.batch_alter_table('openaiapi_nodes', schema=None) as batch_op:
        batch_op.drop_column('auto_v1_api')