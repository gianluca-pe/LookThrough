"""Owner planning targets and deterministic retirement assumptions."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.types import ScaledDecimal
from app.models.configuration import TimestampMixin
from app.models.portfolio import ECONOMIC_ROLE_CODES, FIRE_BUCKET_CODES


ASSET_ROLE_CODES = ECONOMIC_ROLE_CODES
PLANNING_BUCKET_CODES = FIRE_BUCKET_CODES


class AllocationTarget(TimestampMixin, db.Model):
    """One coherent owner-entered acceptable range for an allocation role."""

    __tablename__ = "allocation_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id"), nullable=False, index=True
    )
    asset_role_code: Mapped[str] = mapped_column(String(24), nullable=False)
    minimum_decimal: Mapped[Decimal] = mapped_column(ScaledDecimal(8), nullable=False)
    maximum_decimal: Mapped[Decimal] = mapped_column(ScaledDecimal(8), nullable=False)

    portfolio: Mapped["Portfolio"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "portfolio_id", "asset_role_code", name="uq_allocation_target_role"
        ),
        CheckConstraint(
            "asset_role_code IN ('equity','income','liquidity','alternatives')",
            name="allocation_target_role_supported",
        ),
        CheckConstraint(
            "minimum_decimal >= 0 AND minimum_decimal <= 1",
            name="allocation_target_minimum_between_zero_and_one",
        ),
        CheckConstraint(
            "maximum_decimal >= 0 AND maximum_decimal <= 1",
            name="allocation_target_maximum_between_zero_and_one",
        ),
        CheckConstraint(
            "minimum_decimal <= maximum_decimal",
            name="allocation_target_ordered",
        ),
    )


class RetirementAssumption(TimestampMixin, db.Model):
    """The portfolio's one explicit deterministic retirement base case."""

    __tablename__ = "retirement_assumptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id"), nullable=False, index=True
    )
    current_age_years: Mapped[int] = mapped_column(Integer, nullable=False)
    withdrawal_start_age_years: Mapped[int] = mapped_column(Integer, nullable=False)
    final_age_years: Mapped[int] = mapped_column(Integer, nullable=False)
    equity_return_decimal: Mapped[Decimal] = mapped_column(
        ScaledDecimal(8), nullable=False
    )
    income_return_decimal: Mapped[Decimal] = mapped_column(
        ScaledDecimal(8), nullable=False
    )
    liquidity_return_decimal: Mapped[Decimal] = mapped_column(
        ScaledDecimal(8), nullable=False
    )
    alternatives_return_decimal: Mapped[Decimal] = mapped_column(
        ScaledDecimal(8), nullable=False
    )
    terminal_legacy_target_amount: Mapped[Decimal | None] = mapped_column(
        ScaledDecimal(3), nullable=True
    )
    terminal_legacy_target_currency_code: Mapped[str | None] = mapped_column(
        String(3), nullable=True
    )

    portfolio: Mapped["Portfolio"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "portfolio_id", name="uq_retirement_assumption_portfolio"
        ),
        CheckConstraint(
            "current_age_years >= 0 AND current_age_years <= 120",
            name="retirement_current_age_supported",
        ),
        CheckConstraint(
            "withdrawal_start_age_years > current_age_years "
            "AND withdrawal_start_age_years <= 120",
            name="retirement_withdrawal_age_ordered",
        ),
        CheckConstraint(
            "final_age_years > withdrawal_start_age_years "
            "AND final_age_years <= 130",
            name="retirement_final_age_ordered",
        ),
        CheckConstraint(
            "equity_return_decimal >= -1 AND equity_return_decimal <= 1 "
            "AND income_return_decimal >= -1 AND income_return_decimal <= 1 "
            "AND liquidity_return_decimal >= -1 AND liquidity_return_decimal <= 1 "
            "AND alternatives_return_decimal >= -1 "
            "AND alternatives_return_decimal <= 1",
            name="retirement_returns_supported",
        ),
        CheckConstraint(
            "terminal_legacy_target_amount IS NULL "
            "OR terminal_legacy_target_amount >= 0",
            name="retirement_legacy_target_nonnegative",
        ),
        CheckConstraint(
            "(terminal_legacy_target_amount IS NULL "
            "AND terminal_legacy_target_currency_code IS NULL) OR "
            "(terminal_legacy_target_amount IS NOT NULL "
            "AND terminal_legacy_target_currency_code IS NOT NULL)",
            name="retirement_legacy_target_currency_paired",
        ),
    )
