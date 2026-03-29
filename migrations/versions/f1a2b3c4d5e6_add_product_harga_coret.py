"""add product harga_coret for shelf labels

Revision ID: f1a2b3c4d5e6
Revises: efbc63b6b1bd
Create Date: 2026-03-28

"""
from alembic import op
import sqlalchemy as sa

revision = 'f1a2b3c4d5e6'
down_revision = 'efbc63b6b1bd'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('products', sa.Column('harga_coret', sa.Float(), nullable=True))


def downgrade():
    op.drop_column('products', 'harga_coret')
