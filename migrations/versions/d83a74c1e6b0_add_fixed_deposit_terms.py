"""Add simple fixed-deposit terms.

Revision ID: d83a74c1e6b0
Revises: b6f1d3a9e5c2
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op


revision = "d83a74c1e6b0"
down_revision = "b6f1d3a9e5c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive and deliberately empty: existing position registrations and
    # statement values keep their exact meaning until the owner attaches terms.
    op.create_table(
        "fixed_deposits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("position_registration_id", sa.Integer(), nullable=False),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("maturity_date", sa.Date(), nullable=False),
        sa.Column(
            "annual_rate_decimal", sa.Numeric(precision=18, scale=12), nullable=True
        ),
        sa.Column(
            "maturity_action",
            sa.String(length=32),
            nullable=False,
            server_default="undecided",
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "maturity_date > start_date", name="fixed_deposit_maturity_after_start"
        ),
        sa.CheckConstraint(
            "annual_rate_decimal IS NULL OR annual_rate_decimal >= 0",
            name="fixed_deposit_rate_nonnegative",
        ),
        sa.CheckConstraint(
            "maturity_action IN "
            "('undecided','return_to_cash','rollover','switch','manual')",
            name="fixed_deposit_maturity_action_supported",
        ),
        sa.ForeignKeyConstraint(
            ["position_registration_id"], ["position_registrations.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "position_registration_id", name="uq_fixed_deposit_registration"
        ),
    )
    op.create_index(
        "ix_fixed_deposits_position_registration_id",
        "fixed_deposits",
        ["position_registration_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fixed_deposits_position_registration_id", table_name="fixed_deposits"
    )
    op.drop_table("fixed_deposits")
