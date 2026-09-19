"""Add direct cash settlement account routing.

Revision ID: c921c6d74ef0
Revises: a48d6c9e21f4
Create Date: 2026-08-08
"""

from alembic import op


revision = "c921c6d74ef0"
down_revision = "a48d6c9e21f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite can add a nullable REFERENCES column directly. Avoid batch mode here:
    # rebuilding a populated accounts table is blocked by its position/posting/cash
    # child rows while foreign-key enforcement is enabled.
    op.execute(
        "ALTER TABLE accounts ADD COLUMN cash_settlement_account_id "
        "INTEGER REFERENCES accounts(id)"
    )
    op.create_index(
        "ix_accounts_cash_settlement_account_id",
        "accounts",
        ["cash_settlement_account_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_accounts_cash_settlement_account_id", table_name="accounts")
    op.drop_column("accounts", "cash_settlement_account_id")
