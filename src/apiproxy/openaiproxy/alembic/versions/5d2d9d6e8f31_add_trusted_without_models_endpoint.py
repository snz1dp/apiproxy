"""add trusted node flag for nodes without models endpoint

Revision ID: 5d2d9d6e8f31
Revises: 0e4bdcd25316
Create Date: 2026-04-22 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5d2d9d6e8f31'
down_revision: Union[str, None] = '0e4bdcd25316'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add trusted flag for nodes that do not expose `/v1/models`."""
    with op.batch_alter_table('openaiapi_nodes', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'trusted_without_models_endpoint',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_index(
            batch_op.f('ix_openaiapi_nodes_trusted_without_models_endpoint'),
            ['trusted_without_models_endpoint'],
            unique=False,
        )


def downgrade() -> None:
    """Remove trusted flag for nodes that do not expose `/v1/models`."""
    with op.batch_alter_table('openaiapi_nodes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_openaiapi_nodes_trusted_without_models_endpoint'))
        batch_op.drop_column('trusted_without_models_endpoint')