"""affiliate program tables

Revision ID: q8r9s0t1u2v3
Revises: p2q3r4s5t6u7
Create Date: 2026-04-02
"""
from alembic import op
import sqlalchemy as sa


revision = 'q8r9s0t1u2v3'
down_revision = 'p2q3r4s5t6u7'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    if 'affiliates' not in tables:
        op.create_table(
            'affiliates',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('kode', sa.String(length=32), nullable=False),
            sa.Column('jenis', sa.String(length=20), nullable=False),
            sa.Column('tenant_id', sa.Integer(), nullable=True),
            sa.Column('user_id', sa.Integer(), nullable=True),
            sa.Column('nama_tampilan', sa.String(length=120), nullable=False, server_default=''),
            sa.Column('email', sa.String(length=120), nullable=True),
            sa.Column('telepon', sa.String(length=30), nullable=True),
            sa.Column('aktif', sa.Boolean(), nullable=False, server_default='1'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('catatan', sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('jenis', 'tenant_id', name='uq_affiliate_jenis_tenant'),
            sa.UniqueConstraint('kode', name='uq_affiliates_kode'),
        )
        op.create_index('ix_affiliates_kode', 'affiliates', ['kode'])

    if 'tenant_affiliate_attributions' not in tables:
        op.create_table(
            'tenant_affiliate_attributions',
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('affiliate_id', sa.Integer(), nullable=False),
            sa.Column('sumber', sa.String(length=40), nullable=False, server_default='trial'),
            sa.Column('attributed_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['affiliate_id'], ['affiliates.id']),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
            sa.PrimaryKeyConstraint('tenant_id'),
        )

    if 'affiliate_commissions' not in tables:
        op.create_table(
            'affiliate_commissions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('affiliate_id', sa.Integer(), nullable=False),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('tenant_invoice_id', sa.Integer(), nullable=False),
            sa.Column('base_amount', sa.Float(), nullable=False, server_default='0'),
            sa.Column('commission_pct', sa.Float(), nullable=False, server_default='0'),
            sa.Column('commission_amount', sa.Float(), nullable=False, server_default='0'),
            sa.Column('status', sa.String(length=20), nullable=False, server_default='menunggu'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('paid_at', sa.DateTime(), nullable=True),
            sa.Column('catatan', sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(['affiliate_id'], ['affiliates.id']),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
            sa.ForeignKeyConstraint(['tenant_invoice_id'], ['tenant_invoices.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('tenant_invoice_id', name='uq_aff_commission_invoice'),
        )
        op.create_index('ix_aff_comm_affiliate', 'affiliate_commissions', ['affiliate_id'])
        op.create_index('ix_aff_comm_tenant', 'affiliate_commissions', ['tenant_id'])

    cols = [c['name'] for c in insp.get_columns('lead_captures')] if 'lead_captures' in insp.get_table_names() else []
    if 'affiliate_id' not in cols:
        op.add_column(
            'lead_captures',
            sa.Column('affiliate_id', sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            'fk_lead_captures_affiliate_id',
            'lead_captures',
            'affiliates',
            ['affiliate_id'],
            ['id'],
        )


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    cols = [c['name'] for c in insp.get_columns('lead_captures')] if 'lead_captures' in insp.get_table_names() else []
    if 'affiliate_id' in cols:
        op.drop_constraint('fk_lead_captures_affiliate_id', 'lead_captures', type_='foreignkey')
        op.drop_column('lead_captures', 'affiliate_id')

    if 'affiliate_commissions' in tables:
        op.drop_table('affiliate_commissions')
    if 'tenant_affiliate_attributions' in tables:
        op.drop_table('tenant_affiliate_attributions')
    if 'affiliates' in tables:
        op.drop_table('affiliates')
