"""Add optional bank-quoted fixed-deposit maturity proceeds.

Revision ID: f7b2c9d4e6a1
Revises: e4a91b7c2d60
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op


revision = "f7b2c9d4e6a1"
down_revision = "e4a91b7c2d60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive and nullable: existing terms and every source value are unchanged.
    with op.batch_alter_table("fixed_deposits") as batch_op:
        batch_op.add_column(
            sa.Column(
                "expected_maturity_proceeds_amount",
                sa.Numeric(precision=28, scale=12),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "fixed_deposit_expected_proceeds_positive",
            "expected_maturity_proceeds_amount IS NULL OR "
            "expected_maturity_proceeds_amount > 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("fixed_deposits") as batch_op:
        batch_op.drop_constraint(
            "fixed_deposit_expected_proceeds_positive", type_="check"
        )
        batch_op.drop_column("expected_maturity_proceeds_amount")
