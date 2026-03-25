"""operational_expenses

Revision ID: a1b2c3d4e5f6
Revises: 88a497ca52d5
Create Date: 2026-03-25

Tabel kategori dan pengeluaran biaya operasional per tenant.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = '88a497ca52d5'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    if 'operational_expense_categories' not in tables:
        op.create_table(
            'operational_expense_categories',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('nama', sa.String(length=120), nullable=False),
            sa.Column('deskripsi', sa.Text(), nullable=True),
            sa.Column('sort_order', sa.Integer(), nullable=False),
            sa.Column('aktif', sa.Boolean(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('tenant_id', 'nama', name='uq_opex_category_tenant_nama'),
        )

    if 'operational_expenses' not in tables:
        op.create_table(
            'operational_expenses',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('category_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('branch_id', sa.Integer(), nullable=True),
            sa.Column('jumlah', sa.Float(), nullable=False),
            sa.Column('tanggal', sa.DateTime(), nullable=False),
            sa.Column('keterangan', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['branch_id'], ['branches.id']),
            sa.ForeignKeyConstraint(['category_id'], ['operational_expense_categories.id']),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()
    if 'operational_expenses' in tables:
        op.drop_table('operational_expenses')
    if 'operational_expense_categories' in tables:
        op.drop_table('operational_expense_categories')
