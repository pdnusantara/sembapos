"""tenant_plan_history

Revision ID: 88a497ca52d5
Revises:
Create Date: 2026-03-24 17:52:37.414785

Menambahkan tabel riwayat paket/kuota tenant. Aman dijalankan ulang (cek jika tabel sudah ada).
Skema lain diatur lewat SQLAlchemy db.create_all() pada lingkungan dev.
"""
from alembic import op
import sqlalchemy as sa


revision = '88a497ca52d5'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if 'tenant_plan_history' in insp.get_table_names():
        return
    op.create_table(
        'tenant_plan_history',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('actor_user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('event', sa.String(length=40), nullable=False),
        sa.Column('old_paket_id', sa.Integer(), nullable=True),
        sa.Column('new_paket_id', sa.Integer(), nullable=True),
        sa.Column('old_paket_kode', sa.String(length=40), nullable=True),
        sa.Column('new_paket_kode', sa.String(length=40), nullable=True),
        sa.Column('old_max_cabang', sa.Integer(), nullable=True),
        sa.Column('new_max_cabang', sa.Integer(), nullable=True),
        sa.Column('old_max_user', sa.Integer(), nullable=True),
        sa.Column('new_max_user', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['actor_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if 'tenant_plan_history' in insp.get_table_names():
        op.drop_table('tenant_plan_history')
