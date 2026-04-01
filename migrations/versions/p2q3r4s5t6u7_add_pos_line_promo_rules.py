"""add pos_line_promo_rules for automatic line discounts

Revision ID: p2q3r4s5t6u7
Revises: n1o2p3q4r5s6
Create Date: 2026-04-01
"""
from alembic import op
import sqlalchemy as sa


revision = 'p2q3r4s5t6u7'
down_revision = 'n1o2p3q4r5s6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'pos_line_promo_rules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('nama', sa.String(length=120), nullable=False),
        sa.Column('scope', sa.String(length=20), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=True),
        sa.Column('category_id', sa.Integer(), nullable=True),
        sa.Column('discount_type', sa.String(length=20), nullable=False),
        sa.Column('discount_value', sa.Float(), nullable=False),
        sa.Column('max_discount', sa.Float(), nullable=True),
        sa.Column('min_qty', sa.Float(), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('start_at', sa.DateTime(), nullable=False),
        sa.Column('end_at', sa.DateTime(), nullable=False),
        sa.Column('aktif', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['category_id'], ['product_categories.id']),
        sa.ForeignKeyConstraint(['product_id'], ['products.id']),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('pos_line_promo_rules')
