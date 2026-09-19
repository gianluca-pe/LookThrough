"""Instruments, positions, source values, and minimal opening records."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.conventions import utc_now
from app.extensions import db
from app.models.types import ScaledDecimal
from app.models.configuration import TimestampMixin
from app.models.types import UTCDateTime


INSTRUMENT_TYPE_CODES = (
    "cash", "fund", "etf", "stock", "bond", "government_savings",
    "fixed_deposit", "money_market", "other",
)
TRACKING_MODE_CODES = ("transaction_tracked", "statement_valued")
MATURITY_ACTION_CODES = (
    "undecided", "return_to_cash", "rollover", "switch", "manual",
)
LEGACY_ECONOMIC_ROLE_CODES = (
    "liquidity",
    "ballast",
    "inflation_defence",
    "income_credit",
    "growth",
    "opportunistic",
    "diversifiers",
)
ECONOMIC_ROLE_CODES = ("equity", "income", "liquidity", "alternatives")
LEGACY_FIRE_BUCKET_CODES = ("now", "next", "bridge", "growth", "flex")
FIRE_BUCKET_CODES = ("now", "bridge", "growth", "projects")
HEDGING_STATUS_CODES = ("hedged", "unhedged", "partially_hedged", "unknown")
CAPITAL_CERTAINTY_CODES = ("high", "medium", "low", "unknown")
EQUITY_SENSITIVITY_CODES = ("rarely", "sometimes", "often", "unknown")
LIQUIDITY_PROFILE_CODES = (
    "immediate",
    "days",
    "restricted",
    "maturity_only",
    "unknown",
)
DURATION_BAND_CODES = (
    "none",
    "very_short",
    "short",
    "intermediate",
    "long",
    "unknown",
)
CREDIT_BAND_CODES = (
    "sovereign",
    "investment_grade",
    "mixed",
    "speculative",
    "not_applicable",
    "unknown",
)
CURRENCY_TREATMENT_CODES = (
    "native",
    "hedged_to_share_class",
    "unhedged",
    "partially_hedged",
    "mixed",
    "unknown",
)


class Instrument(TimestampMixin, db.Model):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    ticker_or_isin: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instrument_type: Mapped[str] = mapped_column(String(32), nullable=False)
    valuation_currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    fund_base_currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True)
    hedging_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fire_bucket_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    capital_certainty_code: Mapped[str | None] = mapped_column(String(24), nullable=True)
    equity_sensitivity_code: Mapped[str | None] = mapped_column(String(24), nullable=True)
    liquidity_profile_code: Mapped[str | None] = mapped_column(String(24), nullable=True)
    duration_band_code: Mapped[str | None] = mapped_column(String(24), nullable=True)
    credit_band_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency_treatment_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="1")

    registrations: Mapped[list[PositionRegistration]] = relationship(back_populates="instrument")
    classifications: Mapped[list[InstrumentClassification]] = relationship(
        back_populates="instrument"
    )

    __table_args__ = (
        CheckConstraint(
            "instrument_type IN "
            "('cash','fund','etf','stock','bond','government_savings',"
            "'fixed_deposit','money_market','other')",
            name="instrument_type_supported",
        ),
    )


class InstrumentClassification(TimestampMixin, db.Model):
    __tablename__ = "instrument_classifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id"), nullable=False, index=True
    )
    economic_role_code: Mapped[str] = mapped_column(String(32), nullable=False)
    weight_decimal: Mapped[Decimal] = mapped_column(ScaledDecimal(8), nullable=False)
    effective_date: Mapped[date] = mapped_column(nullable=False, index=True)
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    instrument: Mapped[Instrument] = relationship(back_populates="classifications")

    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "economic_role_code",
            "effective_date",
            name="uq_instrument_classification_role_date",
        ),
        CheckConstraint(
            "economic_role_code IN "
            "('equity','income','liquidity','alternatives','ballast',"
            "'inflation_defence','income_credit','growth','opportunistic',"
            "'diversifiers')",
            name="economic_role_supported",
        ),
        CheckConstraint(
            "weight_decimal > 0 AND weight_decimal <= 1",
            name="classification_weight_positive_at_most_one",
        ),
    )


class PositionRegistration(TimestampMixin, db.Model):
    __tablename__ = "position_registrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False, index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False, index=True)
    tracking_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    opening_date: Mapped[date | None] = mapped_column(nullable=True)
    closing_date: Mapped[date | None] = mapped_column(nullable=True)

    account: Mapped["Account"] = relationship()
    instrument: Mapped[Instrument] = relationship(back_populates="registrations")
    fixed_deposit: Mapped[FixedDeposit | None] = relationship(
        back_populates="position_registration", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "instrument_id", name="uq_registration_account_instrument"),
        CheckConstraint(
            "tracking_mode IN ('transaction_tracked','statement_valued')",
            name="tracking_mode_supported",
        ),
    )


class FixedDeposit(TimestampMixin, db.Model):
    __tablename__ = "fixed_deposits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_registration_id: Mapped[int] = mapped_column(
        ForeignKey("position_registrations.id"), nullable=False, index=True
    )
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    start_date: Mapped[date] = mapped_column(nullable=False)
    maturity_date: Mapped[date] = mapped_column(nullable=False)
    annual_rate_decimal: Mapped[Decimal | None] = mapped_column(
        ScaledDecimal(8), nullable=True
    )
    expected_maturity_proceeds_amount: Mapped[Decimal | None] = mapped_column(
        ScaledDecimal(3), nullable=True
    )
    maturity_action: Mapped[str] = mapped_column(
        String(32), nullable=False, default="undecided", server_default="undecided"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    position_registration: Mapped[PositionRegistration] = relationship(
        back_populates="fixed_deposit"
    )

    __table_args__ = (
        UniqueConstraint(
            "position_registration_id", name="uq_fixed_deposit_registration"
        ),
        CheckConstraint(
            "maturity_date > start_date", name="fixed_deposit_maturity_after_start"
        ),
        CheckConstraint(
            "annual_rate_decimal IS NULL OR annual_rate_decimal >= 0",
            name="fixed_deposit_rate_nonnegative",
        ),
        CheckConstraint(
            "expected_maturity_proceeds_amount IS NULL OR "
            "expected_maturity_proceeds_amount > 0",
            name="fixed_deposit_expected_proceeds_positive",
        ),
        CheckConstraint(
            "maturity_action IN "
            "('undecided','return_to_cash','rollover','switch','manual')",
            name="fixed_deposit_maturity_action_supported",
        ),
    )


class Transaction(TimestampMixin, db.Model):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"), nullable=False, index=True)
    transaction_type: Mapped[str] = mapped_column(String(40), nullable=False)
    effective_date: Mapped[date] = mapped_column(nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="posted", server_default="posted")
    reverses_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    activity_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    postings: Mapped[list[Posting]] = relationship(back_populates="transaction")

    __table_args__ = (
        CheckConstraint("status IN ('posted','reversed')", name="transaction_status_supported"),
    )


class Posting(TimestampMixin, db.Model):
    __tablename__ = "postings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False, index=True)
    posting_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"), nullable=True, index=True)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    quantity_delta: Mapped[Decimal | None] = mapped_column(ScaledDecimal(6), nullable=True)
    cash_amount_delta: Mapped[Decimal | None] = mapped_column(ScaledDecimal(3), nullable=True)
    unit_price: Mapped[Decimal | None] = mapped_column(ScaledDecimal(6), nullable=True)
    price_currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True)

    transaction: Mapped[Transaction] = relationship(back_populates="postings")

    __table_args__ = (
        CheckConstraint(
            "posting_kind IN ('instrument','cash','income','expense','clearing')",
            name="posting_kind_supported",
        ),
    )


class CashBalanceCheckpoint(db.Model):
    __tablename__ = "cash_balance_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id"), nullable=False, index=True
    )
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    effective_date: Mapped[date] = mapped_column(nullable=False, index=True)
    confirmed_balance_amount: Mapped[Decimal] = mapped_column(
        ScaledDecimal(3), nullable=False
    )
    prior_calculated_balance_amount: Mapped[Decimal | None] = mapped_column(
        ScaledDecimal(3), nullable=True
    )
    correction_amount: Mapped[Decimal | None] = mapped_column(
        ScaledDecimal(3), nullable=True
    )
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    account: Mapped["Account"] = relationship()

    __table_args__ = (
        Index(
            "uq_active_cash_checkpoint_account_currency_date",
            "account_id",
            "currency_code",
            "effective_date",
            unique=True,
            sqlite_where=text("superseded_at IS NULL"),
        ),
    )


class Price(TimestampMixin, db.Model):
    __tablename__ = "prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False, index=True)
    effective_date: Mapped[date] = mapped_column(nullable=False, index=True)
    price_amount: Mapped[Decimal] = mapped_column(ScaledDecimal(6), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("instrument_id", "effective_date", name="uq_price_instrument_date"),
        CheckConstraint("price_amount > 0", name="price_amount_positive"),
    )


class ValuationObservation(TimestampMixin, db.Model):
    __tablename__ = "valuation_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_registration_id: Mapped[int] = mapped_column(
        ForeignKey("position_registrations.id"), nullable=False, index=True
    )
    effective_date: Mapped[date] = mapped_column(nullable=False, index=True)
    native_value_amount: Mapped[Decimal] = mapped_column(ScaledDecimal(3), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "position_registration_id",
            "effective_date",
            name="uq_observation_registration_date",
        ),
        CheckConstraint("native_value_amount >= 0", name="native_value_nonnegative"),
    )


class FxRate(TimestampMixin, db.Model):
    __tablename__ = "fx_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    effective_date: Mapped[date] = mapped_column(nullable=False, index=True)
    base_currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    quote_currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    quote_per_base_amount: Mapped[Decimal] = mapped_column(ScaledDecimal(6), nullable=False)
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "base_currency_code",
            "quote_currency_code",
            "effective_date",
            name="uq_fx_pair_date",
        ),
        CheckConstraint("base_currency_code <> quote_currency_code", name="fx_currencies_differ"),
        CheckConstraint("quote_per_base_amount > 0", name="fx_rate_positive"),
    )


from app.models.configuration import Account  # noqa: E402
