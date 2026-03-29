"""add timezone columns to tenants and users

Revision ID: m7n8o9p0q1r2
Revises: f1a2b3c4d5e6
Create Date: 2026-03-29

"""
from alembic import op
import sqlalchemy as sa

revision = 'm7n8o9p0q1r2'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tcols = {c["name"] for c in insp.get_columns("tenants")}
    if "timezone" not in tcols:
        op.add_column(
            "tenants",
            sa.Column(
                "timezone",
                sa.String(length=30),
                nullable=False,
                server_default="Asia/Jakarta",
            ),
        )
    ucols = {c["name"] for c in insp.get_columns("users")}
    if "timezone" not in ucols:
        op.add_column(
            "users", sa.Column("timezone", sa.String(length=30), nullable=True)
        )


def downgrade():
    op.drop_column('users', 'timezone')
    op.drop_column('tenants', 'timezone')
