"""One valuation path for unit-based and statement-valued positions."""

from __future__ import annotations

from app.decimal_policy import currency_places, require_precision

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select

from app.extensions import db
from app.conventions import normalize_currency_code
from app.models import Account, PositionRegistration, Price, ValuationObservation
from app.services.fx import resolve_fx
from app.services.positions import quantity_as_of


@dataclass(frozen=True)
class PositionValuation:
    registration_id: int
    tracking_mode: str
    quantity: Decimal | None
    native_amount: Decimal | None
    native_currency: str
    reporting_amount: Decimal | None
    reporting_currency: str
    value_date: date | None
    fx_date: date | None
    source_mode: str
    status: str
    missing_reason: str | None
    missing_source: str | None
    stale_sources: tuple[str, ...]
    fx_path: tuple[str, ...] | None


class StatementValueValidationError(ValueError):
    """A statement-source validation failure mapped to its input field."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def record_statement_value(
    *,
    portfolio_id: int,
    registration_id: int,
    effective_date: date,
    native_value_amount: Decimal,
    currency_code: str,
    commit: bool = True,
) -> ValuationObservation:
    """Validate and atomically retain one dated statement observation."""

    registration = db.session.scalar(
        select(PositionRegistration)
        .join(Account, Account.id == PositionRegistration.account_id)
        .where(
            PositionRegistration.id == registration_id,
            Account.portfolio_id == portfolio_id,
        )
    )
    if registration is None:
        raise StatementValueValidationError(
            "registration_id", "Choose a statement-valued position in this portfolio."
        )
    if registration.tracking_mode != "statement_valued":
        raise StatementValueValidationError(
            "registration_id",
            "This position is tracked by quantity and prices, not statement value.",
        )
    if registration.closing_date is not None:
        raise StatementValueValidationError(
            "registration_id",
            "This position is closed and cannot accept another statement value.",
        )
    if isinstance(effective_date, datetime) or not isinstance(effective_date, date):
        raise StatementValueValidationError(
            "effective_date", "Enter a valid statement date."
        )
    if (
        not isinstance(native_value_amount, Decimal)
        or not native_value_amount.is_finite()
        or native_value_amount < 0
    ):
        raise StatementValueValidationError(
            "native_value_amount",
            "Statement value must be a non-negative decimal number.",
        )
    try:
        currency = normalize_currency_code(currency_code, field_name="Currency")
    except (TypeError, ValueError) as exc:
        raise StatementValueValidationError("currency_code", str(exc)) from exc
    require_precision(native_value_amount, currency_places(currency), StatementValueValidationError, 'native_value_amount')
    require_precision(native_value_amount, 3, StatementValueValidationError, 'native_value_amount')
    if currency != registration.instrument.valuation_currency_code:
        raise StatementValueValidationError(
            "currency_code",
            "Statement currency must match the instrument valuation currency.",
        )
    duplicate = db.session.scalar(
        select(ValuationObservation.id)
        .where(
            ValuationObservation.position_registration_id == registration.id,
            ValuationObservation.effective_date == effective_date,
        )
        .limit(1)
    )
    if duplicate is not None:
        raise StatementValueValidationError(
            "effective_date", "A statement value already exists for this date."
        )

    try:
        observation = ValuationObservation(
            position_registration_id=registration.id,
            effective_date=effective_date,
            native_value_amount=native_value_amount,
            currency_code=currency,
        )
        db.session.add(observation)
        db.session.flush()
        if commit:
            db.session.commit()
        return observation
    except Exception:
        db.session.rollback()
        raise


def value_position(
    registration: PositionRegistration,
    as_of_date: date,
    *,
    reporting_currency: str,
    price_stale_days: int,
    fx_stale_days: int,
    statement_stale_days: int,
) -> PositionValuation:
    instrument = registration.instrument
    quantity: Decimal | None = None

    if registration.tracking_mode == "transaction_tracked":
        quantity = quantity_as_of(registration, as_of_date)
        source = db.session.scalar(
            select(Price)
            .where(
                Price.instrument_id == instrument.id,
                Price.effective_date <= as_of_date,
            )
            .order_by(Price.effective_date.desc(), Price.id.desc())
            .limit(1)
        )
        if source is None:
            return _missing(registration, quantity, reporting_currency, "No eligible price")
        native_amount = quantity * source.price_amount
        native_currency = source.currency_code
        source_mode = "price"
        native_source = "price"
        source_stale_days = price_stale_days
    else:
        source = db.session.scalar(
            select(ValuationObservation)
            .where(
                ValuationObservation.position_registration_id == registration.id,
                ValuationObservation.effective_date <= as_of_date,
            )
            .order_by(ValuationObservation.effective_date.desc(), ValuationObservation.id.desc())
            .limit(1)
        )
        if source is None:
            return _missing(
                registration, None, reporting_currency, "No eligible statement value"
            )
        native_amount = source.native_value_amount
        native_currency = source.currency_code
        source_mode = "statement_observation"
        native_source = "statement"
        source_stale_days = statement_stale_days

    # A fixed deposit's principal observation does not decay merely because the
    # statement is old. Its actionable clock is maturity; FX freshness remains
    # independent and is still applied below.
    source_status = (
        "current"
        if instrument.instrument_type == "fixed_deposit"
        else (
            "stale"
            if (as_of_date - source.effective_date).days > source_stale_days
            else "current"
        )
    )
    stale_sources = (native_source,) if source_status == "stale" else ()
    fx = resolve_fx(
        native_currency,
        reporting_currency,
        as_of_date,
        stale_days=fx_stale_days,
    )
    if fx.rate is None:
        return PositionValuation(
            registration_id=registration.id,
            tracking_mode=registration.tracking_mode,
            quantity=quantity,
            native_amount=native_amount,
            native_currency=native_currency,
            reporting_amount=None,
            reporting_currency=reporting_currency,
            value_date=source.effective_date,
            fx_date=None,
            source_mode=source_mode,
            status="missing",
            missing_reason=fx.missing_reason,
            missing_source="fx",
            stale_sources=stale_sources,
            fx_path=fx.path,
        )

    if fx.status == "stale":
        stale_sources += ("fx",)
    status = "stale" if stale_sources else "current"
    return PositionValuation(
        registration_id=registration.id,
        tracking_mode=registration.tracking_mode,
        quantity=quantity,
        native_amount=native_amount,
        native_currency=native_currency,
        reporting_amount=native_amount * fx.rate,
        reporting_currency=reporting_currency,
        value_date=source.effective_date,
        fx_date=fx.effective_date,
        source_mode=source_mode,
        status=status,
        missing_reason=None,
        missing_source=None,
        stale_sources=stale_sources,
        fx_path=fx.path,
    )


def _missing(
    registration: PositionRegistration,
    quantity: Decimal | None,
    reporting_currency: str,
    reason: str,
) -> PositionValuation:
    return PositionValuation(
        registration_id=registration.id,
        tracking_mode=registration.tracking_mode,
        quantity=quantity,
        native_amount=None,
        native_currency=registration.instrument.valuation_currency_code,
        reporting_amount=None,
        reporting_currency=reporting_currency,
        value_date=None,
        fx_date=None,
        source_mode=(
            "price"
            if registration.tracking_mode == "transaction_tracked"
            else "statement_observation"
        ),
        status="missing",
        missing_reason=reason,
        missing_source=(
            "price"
            if registration.tracking_mode == "transaction_tracked"
            else "statement"
        ),
        stale_sources=(),
        fx_path=None,
    )
