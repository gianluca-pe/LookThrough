"""Configuration and custody source records."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.conventions import utc_now
from app.extensions import db
from app.models.types import ScaledDecimal
from app.models.types import UTCDateTime


ACCOUNT_TYPE_CODES = ("cash", "brokerage", "retirement", "deposit", "other")
CASH_TRACKING_MODE_CODES = ("separate_cash", "included_in_aggregate")


def _annual_spending_currency_default(context) -> str | None:
    """Default omitted spending currency to the reporting currency on insert."""

    return context.get_current_parameters().get("reporting_currency_code")


class TimestampMixin:
    """UTC audit timestamps used by the three current setup records."""

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now
    )


class Portfolio(TimestampMixin, db.Model):
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    reporting_currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    annual_spending_amount: Mapped[Decimal] = mapped_column(
        ScaledDecimal(3), nullable=False
    )
    annual_spending_currency_code: Mapped[str | None] = mapped_column(
        String(3), nullable=True, default=_annual_spending_currency_default
    )
    annual_inflation_decimal: Mapped[Decimal | None] = mapped_column(
        ScaledDecimal(8), nullable=True
    )
    default_as_of_date: Mapped[date | None] = mapped_column(nullable=True)
    price_stale_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=14, server_default="14"
    )
    fx_stale_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7, server_default="7"
    )
    statement_value_stale_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=45, server_default="45"
    )

    institutions: Mapped[list[Institution]] = relationship(back_populates="portfolio")
    accounts: Mapped[list[Account]] = relationship(back_populates="portfolio")

    __table_args__ = (
        CheckConstraint(
            "annual_spending_amount > 0", name="annual_spending_amount_positive"
        ),
        CheckConstraint(
            "annual_inflation_decimal >= 0 AND annual_inflation_decimal <= 1",
            name="annual_inflation_between_zero_and_one",
        ),
        CheckConstraint("price_stale_days > 0", name="price_stale_days_positive"),
        CheckConstraint("fx_stale_days > 0", name="fx_stale_days_positive"),
        CheckConstraint(
            "statement_value_stale_days > 0",
            name="statement_value_stale_days_positive",
        ),
    )


class Institution(TimestampMixin, db.Model):
    __tablename__ = "institutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    portfolio: Mapped[Portfolio] = relationship(back_populates="institutions")
    accounts: Mapped[list[Account]] = relationship(back_populates="institution")
    relationship_rules: Mapped[list[RelationshipRule]] = relationship(
        back_populates="institution"
    )


class Account(TimestampMixin, db.Model):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id"), nullable=False, index=True
    )
    institution_id: Mapped[int] = mapped_column(
        ForeignKey("institutions.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    account_type: Mapped[str] = mapped_column(String(24), nullable=False)
    default_currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    is_multicurrency: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    cash_tracking_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    cash_settlement_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True, index=True
    )
    portfolio_share_decimal: Mapped[Decimal] = mapped_column(
        ScaledDecimal(8), nullable=False, default=Decimal("1"), server_default="1"
    )
    present_access_decimal: Mapped[Decimal] = mapped_column(
        ScaledDecimal(8), nullable=False, default=Decimal("1"), server_default="1"
    )
    earliest_access_date: Mapped[date | None] = mapped_column(nullable=True)
    access_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    relationship_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )

    portfolio: Mapped[Portfolio] = relationship(back_populates="accounts")
    institution: Mapped[Institution] = relationship(back_populates="accounts")
    cash_settlement_account: Mapped[Account | None] = relationship(
        remote_side="Account.id",
        foreign_keys=[cash_settlement_account_id],
    )

    __table_args__ = (
        CheckConstraint(
            "account_type IN ('cash', 'brokerage', 'retirement', 'deposit', 'other')",
            name="account_type_supported",
        ),
        CheckConstraint(
            "cash_tracking_mode IN ('separate_cash', 'included_in_aggregate')",
            name="cash_tracking_mode_supported",
        ),
        CheckConstraint(
            "portfolio_share_decimal >= 0 AND portfolio_share_decimal <= 1",
            name="portfolio_share_between_zero_and_one",
        ),
        CheckConstraint(
            "present_access_decimal >= 0 AND present_access_decimal <= 1",
            name="present_access_between_zero_and_one",
        ),
    )


class RelationshipRule(TimestampMixin, db.Model):
    __tablename__ = "relationship_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institution_id: Mapped[int] = mapped_column(
        ForeignKey("institutions.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    threshold_amount: Mapped[Decimal] = mapped_column(
        ScaledDecimal(3), nullable=False
    )
    threshold_currency_code: Mapped[str] = mapped_column(
        String(3), nullable=False
    )
    warning_buffer_amount: Mapped[Decimal | None] = mapped_column(
        ScaledDecimal(3), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    institution: Mapped[Institution] = relationship(
        back_populates="relationship_rules"
    )

    __table_args__ = (
        CheckConstraint(
            "threshold_amount > 0", name="relationship_threshold_positive"
        ),
        CheckConstraint(
            "warning_buffer_amount IS NULL OR warning_buffer_amount >= 0",
            name="relationship_warning_buffer_nonnegative",
        ),
        Index(
            "uq_active_relationship_rule_institution",
            "institution_id",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
    )
