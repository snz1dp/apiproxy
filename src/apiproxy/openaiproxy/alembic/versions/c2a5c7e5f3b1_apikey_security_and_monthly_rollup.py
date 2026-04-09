"""

Revision ID: c2a5c7e5f3b1
Revises: 817dfe2df2ef
Create Date: 2026-04-09 00:00:00.000000

"""
from typing import Sequence, Union
import hashlib

from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = 'c2a5c7e5f3b1'
down_revision: Union[str, None] = '817dfe2df2ef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _build_legacy_hash(ownerapp_id: str | None, encrypted_key: str | None, row_id: str) -> str:
    owner = ownerapp_id or ""
    key_value = encrypted_key or ""
    material = f"{owner}:{key_value}:{row_id}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def upgrade() -> None:
    conn = op.get_bind()

    with op.batch_alter_table('openaiapi_apikeys', schema=None) as batch_op:
        batch_op.add_column(sa.Column('key_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('key_prefix', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('key_version', sa.Integer(), server_default='2', nullable=False))
        batch_op.alter_column('key', existing_type=sa.String(length=80), type_=sa.String(length=256), nullable=True)
        batch_op.create_index(batch_op.f('ix_openaiapi_apikeys_key_hash'), ['key_hash'], unique=False)
        batch_op.create_index(batch_op.f('ix_openaiapi_apikeys_key_prefix'), ['key_prefix'], unique=False)
        batch_op.create_index(batch_op.f('ix_openaiapi_apikeys_key_version'), ['key_version'], unique=False)

    rows = conn.execute(sa.text('SELECT id, ownerapp_id, key FROM openaiapi_apikeys')).mappings().all()
    for row in rows:
        row_id = str(row['id'])
        legacy_hash = _build_legacy_hash(row.get('ownerapp_id'), row.get('key'), row_id)
        key_version = 1 if row.get('key') else 2
        conn.execute(
            sa.text(
                'UPDATE openaiapi_apikeys '
                'SET key_hash = :key_hash, key_version = :key_version '
                'WHERE id = :row_id'
            ),
            {
                'key_hash': legacy_hash,
                'key_version': key_version,
                'row_id': row_id,
            },
        )

    with op.batch_alter_table('openaiapi_apikeys', schema=None) as batch_op:
        batch_op.alter_column('key_hash', existing_type=sa.String(length=64), nullable=False)
        batch_op.create_unique_constraint('uix_openaiapi_apikeys_key_hash', ['ownerapp_id', 'key_hash'])

    op.create_table(
        'openaiapi_app_monthly_usage',
        sa.Column('id', sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column('ownerapp_id', sa.Text(), nullable=False),
        sa.Column('model_name', sa.Text(), nullable=False),
        sa.Column('month_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('call_count', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('request_tokens', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('response_tokens', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('total_tokens', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'ownerapp_id',
            'model_name',
            'month_start',
            name='uix_openaiapi_app_monthly_usage_unique',
        ),
    )
    with op.batch_alter_table('openaiapi_app_monthly_usage', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_openaiapi_app_monthly_usage_ownerapp_id'), ['ownerapp_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_openaiapi_app_monthly_usage_model_name'), ['model_name'], unique=False)
        batch_op.create_index(batch_op.f('ix_openaiapi_app_monthly_usage_month_start'), ['month_start'], unique=False)


def downgrade() -> None:
    conn = op.get_bind()

    with op.batch_alter_table('openaiapi_app_monthly_usage', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_openaiapi_app_monthly_usage_month_start'))
        batch_op.drop_index(batch_op.f('ix_openaiapi_app_monthly_usage_model_name'))
        batch_op.drop_index(batch_op.f('ix_openaiapi_app_monthly_usage_ownerapp_id'))
    op.drop_table('openaiapi_app_monthly_usage')

    conn.execute(sa.text("UPDATE openaiapi_apikeys SET key = '' WHERE key IS NULL"))

    with op.batch_alter_table('openaiapi_apikeys', schema=None) as batch_op:
        batch_op.drop_constraint('uix_openaiapi_apikeys_key_hash', type_='unique')
        batch_op.drop_index(batch_op.f('ix_openaiapi_apikeys_key_version'))
        batch_op.drop_index(batch_op.f('ix_openaiapi_apikeys_key_prefix'))
        batch_op.drop_index(batch_op.f('ix_openaiapi_apikeys_key_hash'))
        batch_op.alter_column('key', existing_type=sa.String(length=256), type_=sa.String(length=80), nullable=False)
        batch_op.drop_column('key_version')
        batch_op.drop_column('key_prefix')
        batch_op.drop_column('key_hash')
