"""add app and apikey model access policies

Revision ID: 8b1f6d4c2e90
Revises: 3f7d2c9a1b4e
Create Date: 2026-05-07 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '8b1f6d4c2e90'
down_revision: Union[str, None] = '3f7d2c9a1b4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add application-level and token-level model access policy persistence."""
    with op.batch_alter_table('openaiapi_apikeys', schema=None) as batch_op:
        batch_op.add_column(sa.Column('allowed_models', sa.JSON(), nullable=True))

    op.create_table(
        'openaiapi_app_model_access_policies',
        sa.Column('id', sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column('ownerapp_id', sa.Text(), nullable=False),
        sa.Column('allowed_models', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'ownerapp_id',
            name='uix_openaiapi_app_model_access_policy_ownerapp',
        ),
    )
    with op.batch_alter_table('openaiapi_app_model_access_policies', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_openaiapi_app_model_access_policies_ownerapp_id'),
            ['ownerapp_id'],
            unique=False,
        )


def downgrade() -> None:
    """Remove application-level and token-level model access policy persistence."""
    with op.batch_alter_table('openaiapi_app_model_access_policies', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_openaiapi_app_model_access_policies_ownerapp_id'))
    op.drop_table('openaiapi_app_model_access_policies')

    with op.batch_alter_table('openaiapi_apikeys', schema=None) as batch_op:
        batch_op.drop_column('allowed_models')