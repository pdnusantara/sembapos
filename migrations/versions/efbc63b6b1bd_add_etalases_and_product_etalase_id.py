"""add etalases and product.etalase_id

Revision ID: efbc63b6b1bd
Revises: l5m6n7o8p9q0
Create Date: 2026-03-28 14:47:42.923267

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'efbc63b6b1bd'
down_revision = 'l5m6n7o8p9q0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'etalases',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('nama', sa.String(length=100), nullable=False),
        sa.Column('keterangan', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.add_column('products', sa.Column('etalase_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_products_etalase_id_etalases',
        'products',
        'etalases',
        ['etalase_id'],
        ['id'],
    )


def downgrade():
    op.drop_constraint('fk_products_etalase_id_etalases', 'products', type_='foreignkey')
    op.drop_column('products', 'etalase_id')
    op.drop_table('etalases')
