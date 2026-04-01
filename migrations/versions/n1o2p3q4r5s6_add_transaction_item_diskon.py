"""add transaction_item diskon (per-line POS discount)

Revision ID: n1o2p3q4r5s6
Revises: f4c9b2e1a7d0
Create Date: 2026-04-01
"""
from alembic import op
import sqlalchemy as sa


revision = 'n1o2p3q4r5s6'
down_revision = 'f4c9b2e1a7d0'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('transaction_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('diskon', sa.Float(), nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('transaction_items', schema=None) as batch_op:
        batch_op.drop_column('diskon')
