"""Explicitly adopted two-tier baseline and its dated external income."""

from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.types import ScaledDecimal
from app.models.configuration import TimestampMixin


class RetirementPlan(TimestampMixin, db.Model):
    __tablename__ = "retirement_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"), index=True)
    base_date: Mapped[date] = mapped_column()
    currency_code: Mapped[str] = mapped_column(String(3))
    current_age_years: Mapped[int] = mapped_column()
    withdrawal_start_age_years: Mapped[int] = mapped_column()
    final_age_years: Mapped[int] = mapped_column()
    core_amount: Mapped[Decimal] = mapped_column(ScaledDecimal(3))
    flexible_amount: Mapped[Decimal] = mapped_column(ScaledDecimal(3))
    core_inflation_decimal: Mapped[Decimal] = mapped_column(ScaledDecimal(8))
    flexible_inflation_decimal: Mapped[Decimal] = mapped_column(ScaledDecimal(8))
    equity_return_decimal: Mapped[Decimal] = mapped_column(ScaledDecimal(8))
    income_return_decimal: Mapped[Decimal] = mapped_column(ScaledDecimal(8))
    liquidity_return_decimal: Mapped[Decimal] = mapped_column(ScaledDecimal(8))
    alternatives_return_decimal: Mapped[Decimal] = mapped_column(ScaledDecimal(8))
    planning_mode: Mapped[str] = mapped_column(String(20), default="budget", server_default="budget")
    annual_savings_amount: Mapped[Decimal] = mapped_column(ScaledDecimal(3), default=Decimal("0"), server_default="0")
    legacy_value_basis: Mapped[str] = mapped_column(String(12), default="nominal", server_default="nominal")
    spending_policy: Mapped[str] = mapped_column(String(16))
    lower_rate_decimal: Mapped[Decimal | None] = mapped_column(ScaledDecimal(8))
    upper_rate_decimal: Mapped[Decimal | None] = mapped_column(ScaledDecimal(8))
    lower_multiplier_decimal: Mapped[Decimal | None] = mapped_column(ScaledDecimal(8))
    middle_multiplier_decimal: Mapped[Decimal | None] = mapped_column(ScaledDecimal(8))
    upper_multiplier_decimal: Mapped[Decimal | None] = mapped_column(ScaledDecimal(8))
    terminal_legacy_target_amount: Mapped[Decimal | None] = mapped_column(ScaledDecimal(3))
    terminal_legacy_target_currency_code: Mapped[str | None] = mapped_column(String(3))

    __table_args__ = (
        UniqueConstraint("portfolio_id", name="uq_retirement_plan_portfolio"),
        CheckConstraint("planning_mode IN ('budget','affordability')", name="plan_mode"),
        CheckConstraint("annual_savings_amount >= 0", name="plan_savings_nonnegative"),
        CheckConstraint("legacy_value_basis IN ('nominal','today')", name="plan_legacy_basis"),
        CheckConstraint("planning_mode != 'affordability' OR (spending_policy = 'full_budget' "
                        "AND legacy_value_basis = 'today' AND core_inflation_decimal = flexible_inflation_decimal "
                        "AND terminal_legacy_target_amount IS NOT NULL AND terminal_legacy_target_currency_code = currency_code)",
                        name="plan_affordability_contract"),
        CheckConstraint("spending_policy IN ('full_budget', 'guardrails')", name="plan_spending_policy"),
        CheckConstraint("(spending_policy = 'full_budget' AND lower_rate_decimal IS NULL "
                        "AND upper_rate_decimal IS NULL AND lower_multiplier_decimal IS NULL "
                        "AND middle_multiplier_decimal IS NULL AND upper_multiplier_decimal IS NULL) "
                        "OR (lower_rate_decimal IS NOT NULL AND upper_rate_decimal IS NOT NULL "
                        "AND lower_multiplier_decimal IS NOT NULL AND middle_multiplier_decimal IS NOT NULL "
                        "AND upper_multiplier_decimal IS NOT NULL)", name="plan_rules_complete"),
        CheckConstraint("current_age_years >= 0 AND current_age_years <= 120 "
                        "AND withdrawal_start_age_years >= current_age_years "
                        "AND withdrawal_start_age_years <= 120 "
                        "AND final_age_years > withdrawal_start_age_years "
                        "AND final_age_years <= 130", name="plan_age_order"),
        CheckConstraint("core_amount >= 0 AND flexible_amount >= 0 "
                        "AND core_amount + flexible_amount > 0", name="plan_spending_positive"),
        CheckConstraint("core_inflation_decimal >= 0 AND core_inflation_decimal <= 1 "
                        "AND flexible_inflation_decimal >= 0 AND flexible_inflation_decimal <= 1",
                        name="plan_inflation_bounds"),
        CheckConstraint("equity_return_decimal >= -1 AND equity_return_decimal <= 1 "
                        "AND income_return_decimal >= -1 AND income_return_decimal <= 1 "
                        "AND liquidity_return_decimal >= -1 AND liquidity_return_decimal <= 1 "
                        "AND alternatives_return_decimal >= -1 AND alternatives_return_decimal <= 1",
                        name="plan_return_bounds"),
        CheckConstraint("lower_rate_decimal > 0 AND upper_rate_decimal > lower_rate_decimal "
                        "AND upper_rate_decimal <= 1", name="plan_rate_order"),
        CheckConstraint("lower_multiplier_decimal >= middle_multiplier_decimal "
                        "AND middle_multiplier_decimal >= upper_multiplier_decimal "
                        "AND upper_multiplier_decimal >= 0 AND lower_multiplier_decimal <= 2",
                        name="plan_multiplier_order"),
        CheckConstraint("(terminal_legacy_target_amount IS NULL AND terminal_legacy_target_currency_code IS NULL) "
                        "OR (terminal_legacy_target_amount IS NOT NULL AND terminal_legacy_target_amount >= 0 AND terminal_legacy_target_currency_code IS NOT NULL)",
                        name="plan_legacy_paired"),
    )


class RetirementIncome(TimestampMixin, db.Model):
    __tablename__ = "retirement_income"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("retirement_plans.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    annual_amount: Mapped[Decimal] = mapped_column(ScaledDecimal(3))
    currency_code: Mapped[str] = mapped_column(String(3))
    start_date: Mapped[date] = mapped_column()
    end_date: Mapped[date | None] = mapped_column()
    inflation_decimal: Mapped[Decimal] = mapped_column(ScaledDecimal(8))

    __table_args__ = (
        CheckConstraint("annual_amount >= 0", name="retirement_income_nonnegative"),
        CheckConstraint("inflation_decimal >= 0 AND inflation_decimal <= 1", name="retirement_income_inflation"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="retirement_income_dates"),
    )
