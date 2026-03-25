"""sales_returns and sales_return_items

Revision ID: d1e2f3a4b5c6
Revises: c9d8e7f6a5b4
Create Date: 2026-03-26

"""
from alembic import op
import sqlalchemy as sa


revision = 'd1e2f3a4b5c6'
down_revision = 'c9d8e7f6a5b4'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    if 'sales_returns' not in tables:
        op.create_table(
            'sales_returns',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('branch_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('shift_id', sa.Integer(), nullable=True),
            sa.Column('source_transaction_id', sa.Integer(), nullable=False),
            sa.Column('replacement_transaction_id', sa.Integer(), nullable=True),
            sa.Column('nomor', sa.String(length=50), nullable=False),
            sa.Column('total_retur', sa.Float(), nullable=False),
            sa.Column('alasan', sa.Text(), nullable=True),
            sa.Column('catatan', sa.Text(), nullable=True),
            sa.Column('jenis', sa.String(length=20), nullable=False),
            sa.Column('metode_pengembalian', sa.String(length=30), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['branch_id'], ['branches.id']),
            sa.ForeignKeyConstraint(['replacement_transaction_id'], ['transactions.id']),
            sa.ForeignKeyConstraint(['shift_id'], ['cashier_shifts.id']),
            sa.ForeignKeyConstraint(['source_transaction_id'], ['transactions.id']),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('tenant_id', 'nomor', name='uq_sales_return_tenant_nomor'),
        )
        op.create_index('ix_sales_returns_source_tx', 'sales_returns', ['source_transaction_id'], unique=False)
        op.create_index('ix_sales_returns_tenant_created', 'sales_returns', ['tenant_id', 'created_at'], unique=False)

    if 'sales_return_items' not in tables:
        op.create_table(
            'sales_return_items',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('return_id', sa.Integer(), nullable=False),
            sa.Column('source_transaction_item_id', sa.Integer(), nullable=False),
            sa.Column('product_id', sa.Integer(), nullable=False),
            sa.Column('qty_retur', sa.Float(), nullable=False),
            sa.Column('harga', sa.Float(), nullable=False),
            sa.Column('subtotal', sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(['product_id'], ['products.id']),
            sa.ForeignKeyConstraint(['return_id'], ['sales_returns.id']),
            sa.ForeignKeyConstraint(['source_transaction_item_id'], ['transaction_items.id']),
            sa.PrimaryKeyConstraint('id'),
        )


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()
    if 'sales_return_items' in tables:
        op.drop_table('sales_return_items')
    if 'sales_returns' in tables:
        op.drop_index('ix_sales_returns_tenant_created', table_name='sales_returns')
        op.drop_index('ix_sales_returns_source_tx', table_name='sales_returns')
        op.drop_table('sales_returns')
