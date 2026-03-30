"""add stock opname tables

Revision ID: f4c9b2e1a7d0
Revises: e2d76e83d85e
Create Date: 2026-03-30
"""
from alembic import op
import sqlalchemy as sa


revision = 'f4c9b2e1a7d0'
down_revision = 'e2d76e83d85e'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    if 'stock_opname_sessions' not in tables:
        op.create_table(
            'stock_opname_sessions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('branch_id', sa.Integer(), nullable=True),
            sa.Column('kode', sa.String(length=40), nullable=False),
            sa.Column('judul', sa.String(length=160), nullable=True),
            sa.Column('status', sa.String(length=20), nullable=False),
            sa.Column('catatan', sa.Text(), nullable=True),
            sa.Column('created_by', sa.Integer(), nullable=False),
            sa.Column('submitted_by', sa.Integer(), nullable=True),
            sa.Column('reviewed_by', sa.Integer(), nullable=True),
            sa.Column('approved_by', sa.Integer(), nullable=True),
            sa.Column('rejected_by', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('submitted_at', sa.DateTime(), nullable=True),
            sa.Column('reviewed_at', sa.DateTime(), nullable=True),
            sa.Column('approved_at', sa.DateTime(), nullable=True),
            sa.Column('rejected_at', sa.DateTime(), nullable=True),
            sa.Column('finalized_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
            sa.ForeignKeyConstraint(['branch_id'], ['branches.id']),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.ForeignKeyConstraint(['submitted_by'], ['users.id']),
            sa.ForeignKeyConstraint(['reviewed_by'], ['users.id']),
            sa.ForeignKeyConstraint(['approved_by'], ['users.id']),
            sa.ForeignKeyConstraint(['rejected_by'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('kode', name='uq_stock_opname_sessions_kode'),
        )
        op.create_index(
            'ix_stock_opname_sessions_tenant_created',
            'stock_opname_sessions',
            ['tenant_id', 'created_at'],
            unique=False,
        )
        op.create_index(
            'ix_stock_opname_sessions_tenant_status',
            'stock_opname_sessions',
            ['tenant_id', 'status', 'created_at'],
            unique=False,
        )

    if 'stock_opname_items' not in tables:
        op.create_table(
            'stock_opname_items',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('session_id', sa.Integer(), nullable=False),
            sa.Column('product_id', sa.Integer(), nullable=False),
            sa.Column('system_stock', sa.Float(), nullable=False),
            sa.Column('physical_stock', sa.Float(), nullable=True),
            sa.Column('selisih', sa.Float(), nullable=False, server_default='0'),
            sa.Column('alasan', sa.String(length=255), nullable=True),
            sa.Column('catatan', sa.Text(), nullable=True),
            sa.Column('created_by', sa.Integer(), nullable=False),
            sa.Column('updated_by', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['session_id'], ['stock_opname_sessions.id']),
            sa.ForeignKeyConstraint(['product_id'], ['products.id']),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.ForeignKeyConstraint(['updated_by'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('session_id', 'product_id', name='uq_stock_opname_items_session_product'),
        )
        op.create_index(
            'ix_stock_opname_items_session',
            'stock_opname_items',
            ['session_id', 'id'],
            unique=False,
        )
        op.create_index(
            'ix_stock_opname_items_product',
            'stock_opname_items',
            ['product_id'],
            unique=False,
        )

    if 'stock_opname_approval_logs' not in tables:
        op.create_table(
            'stock_opname_approval_logs',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('session_id', sa.Integer(), nullable=False),
            sa.Column('actor_user_id', sa.Integer(), nullable=False),
            sa.Column('action', sa.String(length=40), nullable=False),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['session_id'], ['stock_opname_sessions.id']),
            sa.ForeignKeyConstraint(['actor_user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(
            'ix_stock_opname_approval_logs_session_created',
            'stock_opname_approval_logs',
            ['session_id', 'created_at'],
            unique=False,
        )


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    if 'stock_opname_approval_logs' in tables:
        op.drop_index('ix_stock_opname_approval_logs_session_created', table_name='stock_opname_approval_logs')
        op.drop_table('stock_opname_approval_logs')

    if 'stock_opname_items' in tables:
        op.drop_index('ix_stock_opname_items_product', table_name='stock_opname_items')
        op.drop_index('ix_stock_opname_items_session', table_name='stock_opname_items')
        op.drop_table('stock_opname_items')

    if 'stock_opname_sessions' in tables:
        op.drop_index('ix_stock_opname_sessions_tenant_status', table_name='stock_opname_sessions')
        op.drop_index('ix_stock_opname_sessions_tenant_created', table_name='stock_opname_sessions')
        op.drop_table('stock_opname_sessions')
