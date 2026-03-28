"""transaction payments

Revision ID: k4l5m6n7o8p9
Revises: j3k4l5m6n7o8
Create Date: 2026-03-27
"""
from alembic import op
import sqlalchemy as sa


revision = 'k4l5m6n7o8p9'
down_revision = 'j3k4l5m6n7o8'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if 'transaction_payments' in insp.get_table_names():
        return
    op.create_table(
        'transaction_payments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('transaction_id', sa.Integer(), nullable=False),
        sa.Column('method', sa.String(length=20), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_transaction_payments_tenant', 'transaction_payments', ['tenant_id'], unique=False)
    op.create_index('ix_transaction_payments_trx', 'transaction_payments', ['transaction_id'], unique=False)
    op.create_index('ix_transaction_payments_method', 'transaction_payments', ['method'], unique=False)


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if 'transaction_payments' not in insp.get_table_names():
        return
    op.drop_index('ix_transaction_payments_method', table_name='transaction_payments')
    op.drop_index('ix_transaction_payments_trx', table_name='transaction_payments')
    op.drop_index('ix_transaction_payments_tenant', table_name='transaction_payments')
    op.drop_table('transaction_payments')
