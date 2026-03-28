"""fifo cost layers

Revision ID: j3k4l5m6n7o8
Revises: h7i8j9k0l1m2
Create Date: 2026-03-27
"""
from alembic import op
import sqlalchemy as sa


revision = 'j3k4l5m6n7o8'
down_revision = 'h7i8j9k0l1m2'
branch_labels = None
depends_on = None


def _columns(insp, table_name):
    return {c['name'] for c in insp.get_columns(table_name)}


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    if 'inventory_cost_layers' not in tables:
        op.create_table(
            'inventory_cost_layers',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('product_id', sa.Integer(), nullable=False),
            sa.Column('source_type', sa.String(length=30), nullable=False),
            sa.Column('source_id', sa.Integer(), nullable=True),
            sa.Column('received_at', sa.DateTime(), nullable=False),
            sa.Column('qty_in', sa.Float(), nullable=False),
            sa.Column('qty_remaining', sa.Float(), nullable=False),
            sa.Column('unit_cost', sa.Float(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
            sa.ForeignKeyConstraint(['product_id'], ['products.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_cost_layers_product_received', 'inventory_cost_layers', ['product_id', 'received_at', 'id'], unique=False)
        op.create_index('ix_cost_layers_tenant_product', 'inventory_cost_layers', ['tenant_id', 'product_id'], unique=False)

    if 'inventory_cost_layer_usages' not in tables:
        op.create_table(
            'inventory_cost_layer_usages',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('layer_id', sa.Integer(), nullable=False),
            sa.Column('transaction_item_id', sa.Integer(), nullable=False),
            sa.Column('qty_used', sa.Float(), nullable=False),
            sa.Column('unit_cost', sa.Float(), nullable=False),
            sa.Column('subtotal_cost', sa.Float(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
            sa.ForeignKeyConstraint(['layer_id'], ['inventory_cost_layers.id']),
            sa.ForeignKeyConstraint(['transaction_item_id'], ['transaction_items.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_cost_usages_transaction_item', 'inventory_cost_layer_usages', ['transaction_item_id'], unique=False)
        op.create_index('ix_cost_usages_layer', 'inventory_cost_layer_usages', ['layer_id'], unique=False)

    insp = sa.inspect(bind)
    if 'transaction_items' in insp.get_table_names():
        cols = _columns(insp, 'transaction_items')
        with op.batch_alter_table('transaction_items') as batch_op:
            if 'hpp_snapshot' not in cols:
                batch_op.add_column(sa.Column('hpp_snapshot', sa.Float(), nullable=True))
            if 'modal_snapshot' not in cols:
                batch_op.add_column(sa.Column('modal_snapshot', sa.Float(), nullable=True))


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    if 'transaction_items' in tables:
        cols = _columns(insp, 'transaction_items')
        with op.batch_alter_table('transaction_items') as batch_op:
            if 'modal_snapshot' in cols:
                batch_op.drop_column('modal_snapshot')
            if 'hpp_snapshot' in cols:
                batch_op.drop_column('hpp_snapshot')

    if 'inventory_cost_layer_usages' in tables:
        op.drop_index('ix_cost_usages_layer', table_name='inventory_cost_layer_usages')
        op.drop_index('ix_cost_usages_transaction_item', table_name='inventory_cost_layer_usages')
        op.drop_table('inventory_cost_layer_usages')
    if 'inventory_cost_layers' in tables:
        op.drop_index('ix_cost_layers_tenant_product', table_name='inventory_cost_layers')
        op.drop_index('ix_cost_layers_product_received', table_name='inventory_cost_layers')
        op.drop_table('inventory_cost_layers')
