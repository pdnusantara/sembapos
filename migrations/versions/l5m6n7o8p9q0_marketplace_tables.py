"""marketplace tables

Revision ID: l5m6n7o8p9q0
Revises: k4l5m6n7o8p9
Create Date: 2026-03-28
"""
from alembic import op
import sqlalchemy as sa


revision = 'l5m6n7o8p9q0'
down_revision = 'k4l5m6n7o8p9'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    if 'marketplace_sellers' not in tables:
        op.create_table(
            'marketplace_sellers',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('nama', sa.String(length=200), nullable=False),
            sa.Column('deskripsi', sa.Text(), nullable=True),
            sa.Column('logo', sa.String(length=300), nullable=True),
            sa.Column('alamat', sa.Text(), nullable=True),
            sa.Column('telepon', sa.String(length=30), nullable=True),
            sa.Column('email', sa.String(length=120), nullable=True),
            sa.Column('aktif', sa.Boolean(), nullable=False, server_default='1'),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
        )

    if 'marketplace_categories' not in tables:
        op.create_table(
            'marketplace_categories',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('nama', sa.String(length=120), nullable=False),
            sa.Column('icon', sa.String(length=10), nullable=True),
            sa.Column('slug', sa.String(length=120), nullable=False),
            sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('aktif', sa.Boolean(), nullable=False, server_default='1'),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('slug', name='uq_mkt_category_slug'),
            sa.PrimaryKeyConstraint('id'),
        )

    if 'marketplace_products' not in tables:
        op.create_table(
            'marketplace_products',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('seller_id', sa.Integer(), nullable=False),
            sa.Column('category_id', sa.Integer(), nullable=True),
            sa.Column('nama', sa.String(length=300), nullable=False),
            sa.Column('deskripsi', sa.Text(), nullable=True),
            sa.Column('harga', sa.Float(), nullable=False, server_default='0'),
            sa.Column('harga_grosir', sa.Float(), nullable=True),
            sa.Column('min_qty_grosir', sa.Integer(), nullable=True),
            sa.Column('stok', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('satuan', sa.String(length=30), nullable=True),
            sa.Column('berat_gram', sa.Integer(), nullable=True, server_default='0'),
            sa.Column('gambar_utama', sa.String(length=300), nullable=True),
            sa.Column('sku', sa.String(length=100), nullable=True),
            sa.Column('aktif', sa.Boolean(), nullable=False, server_default='1'),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['seller_id'], ['marketplace_sellers.id']),
            sa.ForeignKeyConstraint(['category_id'], ['marketplace_categories.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_mkt_products_seller', 'marketplace_products', ['seller_id'])
        op.create_index('ix_mkt_products_category', 'marketplace_products', ['category_id'])

    if 'marketplace_product_images' not in tables:
        op.create_table(
            'marketplace_product_images',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('product_id', sa.Integer(), nullable=False),
            sa.Column('url', sa.String(length=300), nullable=False),
            sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['product_id'], ['marketplace_products.id']),
            sa.PrimaryKeyConstraint('id'),
        )

    if 'marketplace_orders' not in tables:
        op.create_table(
            'marketplace_orders',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('seller_id', sa.Integer(), nullable=False),
            sa.Column('nomor', sa.String(length=50), nullable=False),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='pending'),
            sa.Column('total', sa.Float(), nullable=False, server_default='0'),
            sa.Column('nama_penerima', sa.String(length=200), nullable=True),
            sa.Column('telepon_penerima', sa.String(length=30), nullable=True),
            sa.Column('alamat_kirim', sa.Text(), nullable=True),
            sa.Column('catatan', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
            sa.ForeignKeyConstraint(['seller_id'], ['marketplace_sellers.id']),
            sa.UniqueConstraint('nomor', name='uq_mkt_order_nomor'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_mkt_orders_tenant', 'marketplace_orders', ['tenant_id'])
        op.create_index('ix_mkt_orders_seller', 'marketplace_orders', ['seller_id'])
        op.create_index('ix_mkt_orders_status', 'marketplace_orders', ['status'])

    if 'marketplace_order_items' not in tables:
        op.create_table(
            'marketplace_order_items',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('order_id', sa.Integer(), nullable=False),
            sa.Column('product_id', sa.Integer(), nullable=True),
            sa.Column('nama_produk', sa.String(length=300), nullable=False),
            sa.Column('harga', sa.Float(), nullable=False),
            sa.Column('qty', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('subtotal', sa.Float(), nullable=False),
            sa.Column('satuan', sa.String(length=30), nullable=True),
            sa.ForeignKeyConstraint(['order_id'], ['marketplace_orders.id']),
            sa.ForeignKeyConstraint(['product_id'], ['marketplace_products.id']),
            sa.PrimaryKeyConstraint('id'),
        )

    if 'marketplace_order_status_history' not in tables:
        op.create_table(
            'marketplace_order_status_history',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('order_id', sa.Integer(), nullable=False),
            sa.Column('from_status', sa.String(length=30), nullable=True),
            sa.Column('to_status', sa.String(length=30), nullable=False),
            sa.Column('catatan', sa.Text(), nullable=True),
            sa.Column('changed_by_user_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['order_id'], ['marketplace_orders.id']),
            sa.ForeignKeyConstraint(['changed_by_user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    for tbl in [
        'marketplace_order_status_history',
        'marketplace_order_items',
        'marketplace_orders',
        'marketplace_product_images',
        'marketplace_products',
        'marketplace_categories',
        'marketplace_sellers',
    ]:
        if tbl in tables:
            op.drop_table(tbl)
