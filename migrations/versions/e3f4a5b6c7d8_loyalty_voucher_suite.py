"""loyalty voucher suite

Revision ID: e3f4a5b6c7d8
Revises: d1e2f3a4b5c6
Create Date: 2026-03-27
"""
from alembic import op
import sqlalchemy as sa


revision = 'e3f4a5b6c7d8'
down_revision = 'g1h2i3j4k5l6'
branch_labels = None
depends_on = None


def _column_names(insp, table):
    return {c['name'] for c in insp.get_columns(table)}


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    if 'member_tiers' not in tables:
        op.create_table(
            'member_tiers',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('kode', sa.String(length=20), nullable=False),
            sa.Column('nama', sa.String(length=80), nullable=False),
            sa.Column('min_spend', sa.Float(), nullable=False, server_default='0'),
            sa.Column('benefit_discount_pct', sa.Float(), nullable=False, server_default='0'),
            sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('aktif', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('tenant_id', 'kode', name='uq_member_tier_tenant_kode'),
        )

    if 'vouchers' not in tables:
        op.create_table(
            'vouchers',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('kode', sa.String(length=50), nullable=False),
            sa.Column('nama', sa.String(length=120), nullable=False),
            sa.Column('deskripsi', sa.Text(), nullable=True),
            sa.Column('discount_type', sa.String(length=20), nullable=False, server_default='fixed'),
            sa.Column('discount_value', sa.Float(), nullable=False, server_default='0'),
            sa.Column('max_discount', sa.Float(), nullable=True),
            sa.Column('min_spend', sa.Float(), nullable=False, server_default='0'),
            sa.Column('start_at', sa.DateTime(), nullable=False),
            sa.Column('end_at', sa.DateTime(), nullable=False),
            sa.Column('max_usage_global', sa.Integer(), nullable=True),
            sa.Column('max_usage_per_member', sa.Integer(), nullable=True, server_default='1'),
            sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('created_by', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('tenant_id', 'kode', name='uq_voucher_tenant_kode'),
        )
        op.create_index('ix_vouchers_tenant_period', 'vouchers', ['tenant_id', 'start_at', 'end_at'], unique=False)

    if 'voucher_category_scopes' not in tables:
        op.create_table(
            'voucher_category_scopes',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('voucher_id', sa.Integer(), nullable=False),
            sa.Column('category_id', sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(['category_id'], ['product_categories.id']),
            sa.ForeignKeyConstraint(['voucher_id'], ['vouchers.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('voucher_id', 'category_id', name='uq_voucher_category_scope'),
        )

    if 'voucher_redemptions' not in tables:
        op.create_table(
            'voucher_redemptions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('voucher_id', sa.Integer(), nullable=False),
            sa.Column('member_id', sa.Integer(), nullable=True),
            sa.Column('transaction_id', sa.Integer(), nullable=False),
            sa.Column('discount_amount', sa.Float(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(['member_id'], ['members.id']),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
            sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id']),
            sa.ForeignKeyConstraint(['voucher_id'], ['vouchers.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('voucher_id', 'transaction_id', name='uq_voucher_redemption_tx'),
        )
        op.create_index('ix_voucher_redemptions_member', 'voucher_redemptions', ['member_id', 'created_at'], unique=False)

    insp = sa.inspect(bind)
    tables = insp.get_table_names()
    if 'members' in tables:
        cols = _column_names(insp, 'members')
        with op.batch_alter_table('members') as batch_op:
            if 'tier_id' not in cols:
                batch_op.add_column(sa.Column('tier_id', sa.Integer(), nullable=True))
                batch_op.create_foreign_key('fk_members_tier_id', 'member_tiers', ['tier_id'], ['id'])
            if 'tier_evaluated_at' not in cols:
                batch_op.add_column(sa.Column('tier_evaluated_at', sa.DateTime(), nullable=True))
            if 'rolling_spend' not in cols:
                batch_op.add_column(sa.Column('rolling_spend', sa.Float(), nullable=False, server_default='0'))
            if 'rolling_tx_count' not in cols:
                batch_op.add_column(sa.Column('rolling_tx_count', sa.Integer(), nullable=False, server_default='0'))
            if 'rolling_last_days' not in cols:
                batch_op.add_column(sa.Column('rolling_last_days', sa.Integer(), nullable=False, server_default='365'))
            if 'last_transaction_at' not in cols:
                batch_op.add_column(sa.Column('last_transaction_at', sa.DateTime(), nullable=True))

    insp = sa.inspect(bind)
    if 'transactions' in insp.get_table_names():
        cols = _column_names(insp, 'transactions')
        with op.batch_alter_table('transactions') as batch_op:
            if 'promo_code' not in cols:
                batch_op.add_column(sa.Column('promo_code', sa.String(length=50), nullable=True))
            if 'promo_type' not in cols:
                batch_op.add_column(sa.Column('promo_type', sa.String(length=20), nullable=True))
            if 'promo_name' not in cols:
                batch_op.add_column(sa.Column('promo_name', sa.String(length=150), nullable=True))
            if 'promo_discount' not in cols:
                batch_op.add_column(sa.Column('promo_discount', sa.Float(), nullable=False, server_default='0'))
            if 'promo_payload' not in cols:
                batch_op.add_column(sa.Column('promo_payload', sa.Text(), nullable=True))


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    if 'transactions' in tables:
        cols = _column_names(insp, 'transactions')
        with op.batch_alter_table('transactions') as batch_op:
            if 'promo_payload' in cols:
                batch_op.drop_column('promo_payload')
            if 'promo_discount' in cols:
                batch_op.drop_column('promo_discount')
            if 'promo_name' in cols:
                batch_op.drop_column('promo_name')
            if 'promo_type' in cols:
                batch_op.drop_column('promo_type')
            if 'promo_code' in cols:
                batch_op.drop_column('promo_code')

    insp = sa.inspect(bind)
    if 'members' in insp.get_table_names():
        cols = _column_names(insp, 'members')
        with op.batch_alter_table('members') as batch_op:
            if 'last_transaction_at' in cols:
                batch_op.drop_column('last_transaction_at')
            if 'rolling_last_days' in cols:
                batch_op.drop_column('rolling_last_days')
            if 'rolling_tx_count' in cols:
                batch_op.drop_column('rolling_tx_count')
            if 'rolling_spend' in cols:
                batch_op.drop_column('rolling_spend')
            if 'tier_evaluated_at' in cols:
                batch_op.drop_column('tier_evaluated_at')
            if 'tier_id' in cols:
                batch_op.drop_constraint('fk_members_tier_id', type_='foreignkey')
                batch_op.drop_column('tier_id')

    if 'voucher_redemptions' in tables:
        op.drop_index('ix_voucher_redemptions_member', table_name='voucher_redemptions')
        op.drop_table('voucher_redemptions')
    if 'voucher_category_scopes' in tables:
        op.drop_table('voucher_category_scopes')
    if 'vouchers' in tables:
        op.drop_index('ix_vouchers_tenant_period', table_name='vouchers')
        op.drop_table('vouchers')
    if 'member_tiers' in tables:
        op.drop_table('member_tiers')
