"""Add the deterministic retirement base-case assumptions.

Revision ID: e2f6a8c0d3b5
Revises: d1e5f7a9b2c4
Create Date: 2026-08-30
"""

import sqlalchemy as sa
from alembic import op


revision = "e2f6a8c0d3b5"
down_revision = "d1e5f7a9b2c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "retirement_assumptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("current_age_years", sa.Integer(), nullable=False),
        sa.Column("withdrawal_start_age_years", sa.Integer(), nullable=False),
        sa.Column("final_age_years", sa.Integer(), nullable=False),
        sa.Column(
            "equity_return_decimal", sa.Numeric(precision=10, scale=8), nullable=False
        ),
        sa.Column(
            "income_return_decimal", sa.Numeric(precision=10, scale=8), nullable=False
        ),
        sa.Column(
            "liquidity_return_decimal", sa.Numeric(precision=10, scale=8), nullable=False
        ),
        sa.Column(
            "alternatives_return_decimal",
            sa.Numeric(precision=10, scale=8),
            nullable=False,
        ),
        sa.Column(
            "terminal_legacy_target_amount",
            sa.Numeric(precision=28, scale=12),
            nullable=True,
        ),
        sa.Column(
            "terminal_legacy_target_currency_code",
            sa.String(length=3),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "current_age_years >= 0 AND current_age_years <= 120",
            name="retirement_current_age_supported",
        ),
        sa.CheckConstraint(
            "withdrawal_start_age_years > current_age_years "
            "AND withdrawal_start_age_years <= 120",
            name="retirement_withdrawal_age_ordered",
        ),
        sa.CheckConstraint(
            "final_age_years > withdrawal_start_age_years "
            "AND final_age_years <= 130",
            name="retirement_final_age_ordered",
        ),
        sa.CheckConstraint(
            "equity_return_decimal >= -1 AND equity_return_decimal <= 1 "
            "AND income_return_decimal >= -1 AND income_return_decimal <= 1 "
            "AND liquidity_return_decimal >= -1 AND liquidity_return_decimal <= 1 "
            "AND alternatives_return_decimal >= -1 "
            "AND alternatives_return_decimal <= 1",
            name="retirement_returns_supported",
        ),
        sa.CheckConstraint(
            "terminal_legacy_target_amount IS NULL "
            "OR terminal_legacy_target_amount >= 0",
            name="retirement_legacy_target_nonnegative",
        ),
        sa.CheckConstraint(
            "(terminal_legacy_target_amount IS NULL "
            "AND terminal_legacy_target_currency_code IS NULL) OR "
            "(terminal_legacy_target_amount IS NOT NULL "
            "AND terminal_legacy_target_currency_code IS NOT NULL)",
            name="retirement_legacy_target_currency_paired",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "portfolio_id", name="uq_retirement_assumption_portfolio"
        ),
    )
    op.create_index(
        op.f("ix_retirement_assumptions_portfolio_id"),
        "retirement_assumptions",
        ["portfolio_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_retirement_assumptions_portfolio_id"),
        table_name="retirement_assumptions",
    )
    op.drop_table("retirement_assumptions")
