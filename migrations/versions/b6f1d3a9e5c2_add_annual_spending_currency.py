"""Add the annual spending currency.

Revision ID: b6f1d3a9e5c2
Revises: 8e4c9b1d2a7f
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op


revision = "b6f1d3a9e5c2"
down_revision = "8e4c9b1d2a7f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The old amount meant "in reporting currency". Copy that currency into
    # the new explicit field so no stored amount is reinterpreted. Keep the
    # column nullable at SQLite schema level to avoid rebuilding a populated
    # parent table; all application writes require and validate it.
    op.add_column(
        "portfolios",
        sa.Column("annual_spending_currency_code", sa.String(3), nullable=True),
    )
    op.execute(
        "UPDATE portfolios "
        "SET annual_spending_currency_code = reporting_currency_code "
        "WHERE annual_spending_currency_code IS NULL"
    )


def downgrade() -> None:
    op.drop_column("portfolios", "annual_spending_currency_code")
