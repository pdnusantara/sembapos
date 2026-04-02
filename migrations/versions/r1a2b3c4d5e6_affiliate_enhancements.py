"""affiliate enhancements: clicks, applications, payout fields, campaign expiry

Revision ID: r1a2b3c4d5e6
Revises: q8r9s0t1u2v3
Create Date: 2026-04-02
"""
from alembic import op
import sqlalchemy as sa


revision = 'r1a2b3c4d5e6'
down_revision = 'q8r9s0t1u2v3'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    if 'affiliate_clicks' not in tables:
        op.create_table(
            'affiliate_clicks',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('affiliate_id', sa.Integer(), nullable=True),
            sa.Column('kode_snapshot', sa.String(length=32), nullable=False),
            sa.Column('ip_hash', sa.String(length=64), nullable=True),
            sa.Column('path', sa.String(length=120), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['affiliate_id'], ['affiliates.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_aff_click_created', 'affiliate_clicks', ['created_at'])
        op.create_index('ix_aff_click_aff', 'affiliate_clicks', ['affiliate_id'])

    if 'affiliate_applications' not in tables:
        op.create_table(
            'affiliate_applications',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('nama', sa.String(length=120), nullable=False),
            sa.Column('email', sa.String(length=120), nullable=True),
            sa.Column('telepon', sa.String(length=30), nullable=True),
            sa.Column('username', sa.String(length=50), nullable=False),
            sa.Column('password_hash', sa.String(length=255), nullable=False),
            sa.Column('alasan', sa.Text(), nullable=True),
            sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('reviewed_at', sa.DateTime(), nullable=True),
            sa.Column('reviewer_user_id', sa.Integer(), nullable=True),
            sa.Column('catatan_admin', sa.Text(), nullable=True),
            sa.Column('created_affiliate_id', sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(['reviewer_user_id'], ['users.id']),
            sa.ForeignKeyConstraint(['created_affiliate_id'], ['affiliates.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_aff_app_status', 'affiliate_applications', ['status'])

    cols_aff = [c['name'] for c in insp.get_columns('affiliates')] if 'affiliates' in tables else []
    if 'affiliates' in tables and 'campaign_expires_at' not in cols_aff:
        op.add_column('affiliates', sa.Column('campaign_expires_at', sa.DateTime(), nullable=True))

    cols_ac = [c['name'] for c in insp.get_columns('affiliate_commissions')] if 'affiliate_commissions' in tables else []
    if 'affiliate_commissions' in tables:
        if 'payout_metode' not in cols_ac:
            op.add_column('affiliate_commissions', sa.Column('payout_metode', sa.String(length=40), nullable=True))
        if 'payout_referensi' not in cols_ac:
            op.add_column('affiliate_commissions', sa.Column('payout_referensi', sa.Text(), nullable=True))
        if 'approved_at' not in cols_ac:
            op.add_column('affiliate_commissions', sa.Column('approved_at', sa.DateTime(), nullable=True))


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()
    cols_ac = [c['name'] for c in insp.get_columns('affiliate_commissions')] if 'affiliate_commissions' in tables else []
    if 'approved_at' in cols_ac:
        op.drop_column('affiliate_commissions', 'approved_at')
    if 'payout_referensi' in cols_ac:
        op.drop_column('affiliate_commissions', 'payout_referensi')
    if 'payout_metode' in cols_ac:
        op.drop_column('affiliate_commissions', 'payout_metode')
    cols_aff = [c['name'] for c in insp.get_columns('affiliates')] if 'affiliates' in tables else []
    if 'campaign_expires_at' in cols_aff:
        op.drop_column('affiliates', 'campaign_expires_at')
    if 'affiliate_applications' in tables:
        op.drop_table('affiliate_applications')
    if 'affiliate_clicks' in tables:
        op.drop_table('affiliate_clicks')
