"""Add instrument classification and FIRE metadata.

Revision ID: 8e4c9b1d2a7f
Revises: c921c6d74ef0
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op


revision = "8e4c9b1d2a7f"
down_revision = "c921c6d74ef0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These are optional annotations of existing instruments. Direct nullable
    # columns preserve populated SQLite rows without rebuilding the parent table.
    op.add_column(
        "instruments", sa.Column("fund_base_currency_code", sa.String(3), nullable=True)
    )
    op.add_column(
        "instruments", sa.Column("hedging_status", sa.String(32), nullable=True)
    )
    op.add_column(
        "instruments", sa.Column("fire_bucket_code", sa.String(16), nullable=True)
    )
    op.add_column(
        "instruments",
        sa.Column("capital_certainty_code", sa.String(24), nullable=True),
    )
    op.add_column(
        "instruments",
        sa.Column("equity_sensitivity_code", sa.String(24), nullable=True),
    )
    op.add_column(
        "instruments",
        sa.Column("liquidity_profile_code", sa.String(24), nullable=True),
    )
    op.add_column(
        "instruments", sa.Column("duration_band_code", sa.String(24), nullable=True)
    )
    op.add_column(
        "instruments", sa.Column("credit_band_code", sa.String(32), nullable=True)
    )
    op.add_column(
        "instruments",
        sa.Column("currency_treatment_code", sa.String(32), nullable=True),
    )

    op.create_table(
        "instrument_classifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("economic_role_code", sa.String(length=32), nullable=False),
        sa.Column("weight_decimal", sa.Numeric(precision=10, scale=8), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("source_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "economic_role_code IN "
            "('liquidity','ballast','inflation_defence','income_credit','growth',"
            "'opportunistic','diversifiers')",
            name="economic_role_supported",
        ),
        sa.CheckConstraint(
            "weight_decimal > 0 AND weight_decimal <= 1",
            name="classification_weight_positive_at_most_one",
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instrument_id",
            "economic_role_code",
            "effective_date",
            name="uq_instrument_classification_role_date",
        ),
    )
    op.create_index(
        "ix_instrument_classifications_effective_date",
        "instrument_classifications",
        ["effective_date"],
        unique=False,
    )
    op.create_index(
        "ix_instrument_classifications_instrument_id",
        "instrument_classifications",
        ["instrument_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_instrument_classifications_instrument_id",
        table_name="instrument_classifications",
    )
    op.drop_index(
        "ix_instrument_classifications_effective_date",
        table_name="instrument_classifications",
    )
    op.drop_table("instrument_classifications")
    op.drop_column("instruments", "currency_treatment_code")
    op.drop_column("instruments", "credit_band_code")
    op.drop_column("instruments", "duration_band_code")
    op.drop_column("instruments", "liquidity_profile_code")
    op.drop_column("instruments", "equity_sensitivity_code")
    op.drop_column("instruments", "capital_certainty_code")
    op.drop_column("instruments", "fire_bucket_code")
    op.drop_column("instruments", "hedging_status")
    op.drop_column("instruments", "fund_base_currency_code")
