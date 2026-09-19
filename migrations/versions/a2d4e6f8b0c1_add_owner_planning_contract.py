"""Add owner allocation targets and accept the four-role vocabulary.

Revision ID: a2d4e6f8b0c1
Revises: f7b2c9d4e6a1
Create Date: 2026-08-29
"""

import sqlalchemy as sa
from alembic import op


revision = "a2d4e6f8b0c1"
down_revision = "f7b2c9d4e6a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep dated legacy rows in place while accepting all new four-role writes.
    # The classification service maps these stored legacy roles when reading.
    with op.batch_alter_table("instrument_classifications") as batch_op:
        batch_op.drop_constraint("economic_role_supported", type_="check")
        batch_op.create_check_constraint(
            "economic_role_supported",
            "economic_role_code IN "
            "('equity','income','liquidity','alternatives','ballast',"
            "'inflation_defence','income_credit','growth','opportunistic',"
            "'diversifiers')",
        )

    op.create_table(
        "allocation_targets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("asset_role_code", sa.String(length=24), nullable=False),
        sa.Column("minimum_decimal", sa.Numeric(precision=10, scale=8), nullable=False),
        sa.Column("maximum_decimal", sa.Numeric(precision=10, scale=8), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "asset_role_code IN ('equity','income','liquidity','alternatives')",
            name="allocation_target_role_supported",
        ),
        sa.CheckConstraint(
            "minimum_decimal >= 0 AND minimum_decimal <= 1",
            name="allocation_target_minimum_between_zero_and_one",
        ),
        sa.CheckConstraint(
            "maximum_decimal >= 0 AND maximum_decimal <= 1",
            name="allocation_target_maximum_between_zero_and_one",
        ),
        sa.CheckConstraint(
            "minimum_decimal <= maximum_decimal",
            name="allocation_target_ordered",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "portfolio_id", "asset_role_code", name="uq_allocation_target_role"
        ),
    )
    op.create_index(
        "ix_allocation_targets_portfolio_id",
        "allocation_targets",
        ["portfolio_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_allocation_targets_portfolio_id", table_name="allocation_targets"
    )
    op.drop_table("allocation_targets")
    with op.batch_alter_table("instrument_classifications") as batch_op:
        batch_op.drop_constraint("economic_role_supported", type_="check")
        batch_op.create_check_constraint(
            "economic_role_supported",
            "economic_role_code IN "
            "('liquidity','ballast','inflation_defence','income_credit','growth',"
            "'opportunistic','diversifiers')",
        )
