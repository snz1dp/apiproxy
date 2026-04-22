"""add node protocol and request log protocol columns

Revision ID: 3f7d2c9a1b4e
Revises: 89d6a55f7d12
Create Date: 2026-04-22 01:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f7d2c9a1b4e'
down_revision: Union[str, None] = '89d6a55f7d12'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


protocol_type_enum = sa.Enum('openai', 'anthropic', 'both', name='protocol_type_enum')


def upgrade() -> None:
    """Add protocol and node-level proxy columns required by Anthropic compatibility."""
    bind = op.get_bind()
    protocol_type_enum.create(bind, checkfirst=True)

    with op.batch_alter_table('openaiapi_nodes', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'protocol_type',
                protocol_type_enum,
                nullable=False,
                server_default='openai',
            )
        )
        batch_op.add_column(sa.Column('request_proxy_url', sa.Text(), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_openaiapi_nodes_protocol_type'),
            ['protocol_type'],
            unique=False,
        )

    with op.batch_alter_table('openaiapi_nodelogs', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'request_protocol',
                protocol_type_enum,
                nullable=False,
                server_default='openai',
            )
        )
        batch_op.create_index(
            batch_op.f('ix_openaiapi_nodelogs_request_protocol'),
            ['request_protocol'],
            unique=False,
        )


def downgrade() -> None:
    """Remove protocol and node-level proxy columns."""
    with op.batch_alter_table('openaiapi_nodelogs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_openaiapi_nodelogs_request_protocol'))
        batch_op.drop_column('request_protocol')

    with op.batch_alter_table('openaiapi_nodes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_openaiapi_nodes_protocol_type'))
        batch_op.drop_column('request_proxy_url')
        batch_op.drop_column('protocol_type')

    bind = op.get_bind()
    protocol_type_enum.drop(bind, checkfirst=True)