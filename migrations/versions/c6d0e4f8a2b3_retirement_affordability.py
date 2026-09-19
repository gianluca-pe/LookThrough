"""Add affordability intent without reinterpreting existing plans.

Revision ID: c6d0e4f8a2b3
Revises: b5c9d3e7f1a2
"""
from alembic import op
import sqlalchemy as sa

revision = "c6d0e4f8a2b3"
down_revision = "b5c9d3e7f1a2"
branch_labels = None
depends_on = None


def upgrade():
    # SQLite ADD COLUMN keeps parent rows and income foreign keys intact.
    op.add_column("retirement_plans", sa.Column("planning_mode", sa.String(20), sa.CheckConstraint("planning_mode IN ('budget','affordability')", name="plan_mode"), nullable=False, server_default="budget"))
    op.add_column("retirement_plans", sa.Column("annual_savings_amount", sa.Numeric(28, 12), sa.CheckConstraint("annual_savings_amount >= 0", name="plan_savings_nonnegative"), nullable=False, server_default="0"))
    op.add_column("retirement_plans", sa.Column("legacy_value_basis", sa.String(12),
        sa.CheckConstraint("legacy_value_basis IN ('nominal','today')", name="plan_legacy_basis"),
        sa.CheckConstraint("planning_mode != 'affordability' OR (spending_policy = 'full_budget' "
                           "AND legacy_value_basis = 'today' AND core_inflation_decimal = flexible_inflation_decimal "
                        "AND terminal_legacy_target_amount IS NOT NULL AND terminal_legacy_target_currency_code = currency_code)", name="plan_affordability_contract"), nullable=False, server_default="nominal"))


def downgrade():
    op.drop_column("retirement_plans", "legacy_value_basis")
    op.drop_column("retirement_plans", "annual_savings_amount")
    op.drop_column("retirement_plans", "planning_mode")
