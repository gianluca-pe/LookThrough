"""Add account reference.

Revision ID: a48d6c9e21f4
Revises: 6da3f1c8b972
Create Date: 2026-08-08
"""

from alembic import op
import sqlalchemy as sa


revision = "a48d6c9e21f4"
down_revision = "6da3f1c8b972"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reference", sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.drop_column("reference")
