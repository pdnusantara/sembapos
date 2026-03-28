"""product audit logs

Revision ID: h7i8j9k0l1m2
Revises: e3f4a5b6c7d8
Create Date: 2026-03-27
"""
from alembic import op
import sqlalchemy as sa


revision = 'h7i8j9k0l1m2'
down_revision = 'e3f4a5b6c7d8'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if 'product_audit_logs' in insp.get_table_names():
        return

    op.create_table(
        'product_audit_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('actor_user_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(length=50), nullable=False),
        sa.Column('old_harga_jual', sa.Float(), nullable=True),
        sa.Column('new_harga_jual', sa.Float(), nullable=True),
        sa.Column('old_stok_minimum', sa.Float(), nullable=True),
        sa.Column('new_stok_minimum', sa.Float(), nullable=True),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.ForeignKeyConstraint(['actor_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['product_id'], ['products.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_product_audit_logs_tenant_created', 'product_audit_logs', ['tenant_id', 'created_at'], unique=False)
    op.create_index('ix_product_audit_logs_product_created', 'product_audit_logs', ['product_id', 'created_at'], unique=False)


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if 'product_audit_logs' not in insp.get_table_names():
        return
    op.drop_index('ix_product_audit_logs_product_created', table_name='product_audit_logs')
    op.drop_index('ix_product_audit_logs_tenant_created', table_name='product_audit_logs')
    op.drop_table('product_audit_logs')
