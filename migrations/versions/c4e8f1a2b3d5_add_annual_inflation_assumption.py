"""Add the owner-entered annual inflation assumption.

Revision ID: c4e8f1a2b3d5
Revises: a2d4e6f8b0c1
Create Date: 2026-08-29
"""

import sqlalchemy as sa
from alembic import op


revision = "c4e8f1a2b3d5"
down_revision = "a2d4e6f8b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite can add a nullable column with a column-level CHECK without rebuilding
    # the parent portfolios table (which already has many foreign-key children).
    op.add_column(
        "portfolios",
        sa.Column(
            "annual_inflation_decimal",
            sa.Numeric(precision=10, scale=8),
            sa.CheckConstraint(
                "annual_inflation_decimal >= 0 "
                "AND annual_inflation_decimal <= 1",
                name="annual_inflation_between_zero_and_one",
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("portfolios", "annual_inflation_decimal")
