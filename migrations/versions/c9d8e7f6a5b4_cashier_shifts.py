"""cashier_shifts and transaction.shift_id

Revision ID: c9d8e7f6a5b4
Revises: a1b2c3d4e5f6
Create Date: 2026-03-25

"""
from alembic import op
import sqlalchemy as sa


revision = 'c9d8e7f6a5b4'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    if 'cashier_shifts' not in tables:
        op.create_table(
            'cashier_shifts',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('branch_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('opened_at', sa.DateTime(), nullable=False),
            sa.Column('closed_at', sa.DateTime(), nullable=True),
            sa.Column('opening_float', sa.Float(), nullable=False),
            sa.Column('closing_counted', sa.Float(), nullable=True),
            sa.Column('expected_cash', sa.Float(), nullable=True),
            sa.Column('variance', sa.Float(), nullable=True),
            sa.Column('status', sa.String(length=20), nullable=False),
            sa.Column('note_open', sa.Text(), nullable=True),
            sa.Column('note_close', sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(['branch_id'], ['branches.id']),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(
            'ix_cashier_shifts_branch_user_status',
            'cashier_shifts',
            ['branch_id', 'user_id', 'status'],
            unique=False,
        )

    insp2 = sa.inspect(bind)
    txn_tables = insp2.get_table_names()
    cols = [c['name'] for c in insp2.get_columns('transactions')] if 'transactions' in txn_tables else []
    if 'transactions' in txn_tables and 'shift_id' not in cols:
        with op.batch_alter_table('transactions') as batch_op:
            batch_op.add_column(sa.Column('shift_id', sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                'fk_transactions_shift_id',
                'cashier_shifts',
                ['shift_id'],
                ['id'],
            )


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()
    cols = [c['name'] for c in insp.get_columns('transactions')] if 'transactions' in tables else []
    if 'transactions' in tables and 'shift_id' in cols:
        with op.batch_alter_table('transactions') as batch_op:
            batch_op.drop_constraint('fk_transactions_shift_id', type_='foreignkey')
            batch_op.drop_column('shift_id')
    if 'cashier_shifts' in tables:
        op.drop_index('ix_cashier_shifts_branch_user_status', table_name='cashier_shifts')
        op.drop_table('cashier_shifts')
