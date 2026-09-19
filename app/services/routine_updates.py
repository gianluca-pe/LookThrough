"""One-date grouped maintenance over existing portfolio source records."""

from __future__ import annotations

from app.decimal_policy import exact_units

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping

from sqlalchemy import select

from app.extensions import db
from app.models import (
    Account,
    FxRate,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    Price,
    RelationshipRule,
    ValuationObservation,
)
from app.services.cash import (
    CashConfirmationCommand,
    CashValidationError,
    cash_currencies,
    confirm_cash,
    resolve_cash,
)
from app.services.fx import (
    FxChangeAssessment,
    apply_same_day_fx_correction,
    assess_fx_change,
    fx_correction_context,
    resolve_fx,
    same_day_fx_rates,
)
from app.services.positions import quantity_as_of
from app.services.portfolio_summary import build_portfolio_summary
from app.services.valuation import (
    StatementValueValidationError,
    record_statement_value,
)


class RoutineUpdateValidationError(ValueError):
    """Validation errors keyed to the dynamic input that needs correction."""

    def __init__(
        self,
        errors: Mapping[str, str],
        *,
        fx_warnings: Mapping[str, FxChangeAssessment] | None = None,
    ) -> None:
        self.errors = dict(errors)
        self.fx_warnings = dict(fx_warnings or {})
        super().__init__(next(iter(self.errors.values()), "Update could not be saved."))


@dataclass(frozen=True)
class RoutineUpdateResult:
    saved_count: int
    warnings: tuple[str, ...] = ()


def _validate_date(value: date) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise RoutineUpdateValidationError(
            {"effective_date": "Enter a valid update date."}
        )


def _latest_price(instrument_id: int, as_of_date: date) -> Price | None:
    return db.session.scalar(
        select(Price)
        .where(
            Price.instrument_id == instrument_id,
            Price.effective_date <= as_of_date,
        )
        .order_by(Price.effective_date.desc(), Price.id.desc())
        .limit(1)
    )


def _latest_statement(
    registration_id: int, as_of_date: date
) -> ValuationObservation | None:
    return db.session.scalar(
        select(ValuationObservation)
        .where(
            ValuationObservation.position_registration_id == registration_id,
            ValuationObservation.effective_date <= as_of_date,
        )
        .order_by(
            ValuationObservation.effective_date.desc(),
            ValuationObservation.id.desc(),
        )
        .limit(1)
    )


def _source_status(
    source_date: date | None,
    as_of_date: date,
    stale_days: int,
    *,
    never_stale: bool = False,
) -> str:
    if source_date is None:
        return "missing"
    if not never_stale and (as_of_date - source_date).days > stale_days:
        return "stale"
    return "current"


def _registration_is_current(
    registration: PositionRegistration, as_of_date: date
) -> bool:
    return bool(
        registration.account.is_active
        and registration.instrument.is_active
        and (
            registration.opening_date is None
            or registration.opening_date <= as_of_date
        )
        and (
            registration.closing_date is None
            or registration.closing_date > as_of_date
        )
    )


def build_routine_update_session(
    portfolio: Portfolio, as_of_date: date
) -> dict[str, object]:
    """Return stable current source rows; never manufacture editable values."""

    _validate_date(as_of_date)
    registrations = list(
        db.session.scalars(
            select(PositionRegistration)
            .join(Account, Account.id == PositionRegistration.account_id)
            .join(Institution, Institution.id == Account.institution_id)
            .join(Instrument, Instrument.id == PositionRegistration.instrument_id)
            .where(Account.portfolio_id == portfolio.id)
            .order_by(
                Institution.name,
                Account.name,
                Instrument.name,
                PositionRegistration.id,
            )
        )
    )

    price_accounts: dict[int, list[str]] = {}
    price_instruments: dict[int, Instrument] = {}
    statement_rows: list[dict[str, object]] = []
    currencies: set[str] = set()

    for registration in registrations:
        if not _registration_is_current(registration, as_of_date):
            continue
        instrument = registration.instrument
        if registration.tracking_mode == "transaction_tracked":
            if instrument.instrument_type in {"cash", "fixed_deposit"}:
                continue
            if quantity_as_of(registration, as_of_date) == 0:
                continue
            currencies.add(instrument.valuation_currency_code)
            price_instruments[instrument.id] = instrument
            price_accounts.setdefault(instrument.id, []).append(
                f"{registration.account.institution.name} · {registration.account.name}"
            )
        else:
            currencies.add(instrument.valuation_currency_code)
            latest = _latest_statement(registration.id, as_of_date)
            statement_rows.append(
                {
                    "registration_id": registration.id,
                    "field_name": f"statement_{registration.id}",
                    "instrument_name": instrument.name,
                    "instrument_type": instrument.instrument_type,
                    "account_name": registration.account.name,
                    "institution_name": registration.account.institution.name,
                    "currency_code": instrument.valuation_currency_code,
                    "latest_amount": (
                        latest.native_value_amount if latest is not None else None
                    ),
                    "latest_date": latest.effective_date if latest is not None else None,
                    "status": _source_status(
                        latest.effective_date if latest is not None else None,
                        as_of_date,
                        portfolio.statement_value_stale_days,
                        never_stale=instrument.instrument_type == "fixed_deposit",
                    ),
                }
            )

    price_rows: list[dict[str, object]] = []
    for instrument in sorted(
        price_instruments.values(),
        key=lambda row: (
            price_accounts[row.id][0].casefold(),
            row.name.casefold(),
            row.id,
        ),
    ):
        latest = _latest_price(instrument.id, as_of_date)
        price_rows.append(
            {
                "instrument_id": instrument.id,
                "field_name": f"price_{instrument.id}",
                "instrument_name": instrument.name,
                "ticker_or_isin": instrument.ticker_or_isin,
                "currency_code": instrument.valuation_currency_code,
                "account_names": tuple(dict.fromkeys(price_accounts[instrument.id])),
                "latest_amount": latest.price_amount if latest is not None else None,
                "latest_date": latest.effective_date if latest is not None else None,
                "status": _source_status(
                    latest.effective_date if latest is not None else None,
                    as_of_date,
                    portfolio.price_stale_days,
                ),
            }
        )

    accounts = list(
        db.session.scalars(
            select(Account)
            .join(Institution, Institution.id == Account.institution_id)
            .where(
                Account.portfolio_id == portfolio.id,
                Account.is_active.is_(True),
                Account.cash_tracking_mode == "separate_cash",
                Account.cash_settlement_account_id.is_(None),
            )
            .order_by(Institution.name, Account.name, Account.id)
        )
    )
    cash_rows: list[dict[str, object]] = []
    for account in accounts:
        for currency in cash_currencies(
            portfolio.id, account.id, as_of_date=as_of_date
        ):
            cash = resolve_cash(portfolio.id, account.id, currency, as_of_date)
            is_sourced = cash.checkpoint is not None or bool(cash.effects)
            currencies.add(currency)
            cash_rows.append(
                {
                    "account_id": account.id,
                    "field_name": f"cash_{account.id}_{currency}",
                    "account_name": account.name,
                    "institution_name": account.institution.name,
                    "currency_code": currency,
                    "latest_amount": cash.amount if is_sourced else None,
                    "latest_date": (
                        cash.checkpoint.effective_date
                        if cash.checkpoint is not None
                        else None
                    ),
                    "source_mode": cash.source_mode,
                    "status": "current" if is_sourced else "missing",
                }
            )

    from app.services.retirement_plans import adopted_plan, plan_incomes
    plan = adopted_plan(portfolio.id)
    if plan:
        currencies.add(plan.currency_code)
        if plan.terminal_legacy_target_currency_code:
            currencies.add(plan.terminal_legacy_target_currency_code)
        currencies.update(income.currency_code for income in plan_incomes(plan.id))
    elif portfolio.annual_spending_currency_code:
        currencies.add(portfolio.annual_spending_currency_code)
    rules = list(
        db.session.scalars(
            select(RelationshipRule)
            .join(Institution)
            .where(
                Institution.portfolio_id == portfolio.id,
                RelationshipRule.is_active.is_(True),
            )
            .order_by(Institution.name, RelationshipRule.id)
        )
    )
    currencies.update(rule.threshold_currency_code for rule in rules)

    quote = portfolio.reporting_currency_code
    pair_purposes: dict[tuple[str, str], list[str]] = {
        (base, quote): ["Portfolio reporting"]
        for base in sorted(currencies - {quote})
    }
    if plan:
        # Retirement is evaluated in the plan currency, independently of reporting.
        for source in sorted(currencies - {plan.currency_code}):
            pair = (source, plan.currency_code)
            if (pair[1], pair[0]) in pair_purposes:
                pair = (pair[1], pair[0])
            pair_purposes.setdefault(pair, []).append("Retirement plan")
    if rules:
        # Use the same current source list as relationship valuation. Native
        # currencies are independent of the summary's reporting currency.
        summary = build_portfolio_summary(portfolio, as_of_date)
        eligible_accounts = {
            account.id: account.institution_id
            for account in db.session.scalars(
                select(Account).where(
                    Account.portfolio_id == portfolio.id,
                    Account.is_active.is_(True),
                    Account.relationship_eligible.is_(True),
                )
            )
        }
        source_currencies: dict[int, set[str]] = {}
        for row in summary["holdings"] + summary["cash_balances"]:
            institution_id = eligible_accounts.get(row["account_id"])
            if institution_id is not None:
                source_currencies.setdefault(institution_id, set()).add(
                    row["native_currency"]
                )
        for rule in rules:
            target = rule.threshold_currency_code
            purpose = f"Relationship minimum: {rule.institution.name} · {rule.name}"
            for source in sorted(source_currencies.get(rule.institution_id, set())):
                if source == target:
                    continue
                pair = (source, target)
                if (target, source) in pair_purposes:
                    pair = (target, source)
                purposes = pair_purposes.setdefault(pair, [])
                if purpose not in purposes:
                    purposes.append(purpose)

    fx_rows: list[dict[str, object]] = []
    for (base, quote), purposes in pair_purposes.items():
        resolved = resolve_fx(
            base, quote, as_of_date, stale_days=portfolio.fx_stale_days
        )
        same_day_sources = same_day_fx_rates(base, quote, as_of_date)
        fx_rows.append(
            {
                "base_currency_code": base,
                "quote_currency_code": quote,
                "field_name": f"fx_{base}_{quote}",
                "latest_amount": resolved.rate,
                "latest_date": resolved.effective_date,
                "source_mode": resolved.source_mode,
                "status": resolved.status,
                "path": resolved.path,
                "same_day_sources": same_day_sources,
                "purposes": tuple(purposes),
            }
        )

    from app.services.fx_reference import latest_set
    return {
        "reference_set": latest_set(as_of_date),
        "as_of_date": as_of_date,
        "reporting_currency": portfolio.reporting_currency_code,
        "price_rows": price_rows,
        "statement_rows": statement_rows,
        "cash_rows": cash_rows,
        "fx_rows": fx_rows,
    }


def save_price_updates(
    portfolio: Portfolio,
    effective_date: date,
    updates: Mapping[int, Decimal],
) -> RoutineUpdateResult:
    _validate_date(effective_date)
    session = build_routine_update_session(portfolio, effective_date)
    eligible = {row["instrument_id"]: row for row in session["price_rows"]}
    errors: dict[str, str] = {}
    for instrument_id, amount in updates.items():
        field = f"price_{instrument_id}"
        try:
            exact_units(amount, 6)
        except (ValueError, TypeError) as error:
            errors[field] = str(error)
            continue
        if instrument_id not in eligible:
            errors[field] = "This instrument does not currently use a manual price."
        elif not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
            errors[field] = "Price must be a finite number greater than zero."
        elif db.session.scalar(
            select(Price.id).where(
                Price.instrument_id == instrument_id,
                Price.effective_date == effective_date,
            )
        ) is not None:
            errors[field] = "A price already exists for this instrument and date."
    if errors:
        raise RoutineUpdateValidationError(errors)

    try:
        for instrument_id, amount in updates.items():
            row = eligible[instrument_id]
            db.session.add(
                Price(
                    instrument_id=instrument_id,
                    effective_date=effective_date,
                    price_amount=amount,
                    currency_code=row["currency_code"],
                )
            )
        db.session.commit()
        return RoutineUpdateResult(len(updates))
    except Exception:
        db.session.rollback()
        raise


def save_statement_updates(
    portfolio: Portfolio,
    effective_date: date,
    updates: Mapping[int, Decimal],
) -> RoutineUpdateResult:
    _validate_date(effective_date)
    session = build_routine_update_session(portfolio, effective_date)
    eligible_ids = {row["registration_id"] for row in session["statement_rows"]}
    invalid = {
        f"statement_{registration_id}":
        "This position does not currently use statement-value tracking."
        for registration_id in updates
        if registration_id not in eligible_ids
    }
    if invalid:
        raise RoutineUpdateValidationError(invalid)
    try:
        for registration_id, amount in updates.items():
            try:
                record_statement_value(
                    portfolio_id=portfolio.id,
                    registration_id=registration_id,
                    effective_date=effective_date,
                    native_value_amount=amount,
                    currency_code=next(
                        row["currency_code"]
                        for row in session["statement_rows"]
                        if row["registration_id"] == registration_id
                    ),
                    commit=False,
                )
            except StatementValueValidationError as error:
                raise RoutineUpdateValidationError(
                    {f"statement_{registration_id}": error.message}
                ) from error
        db.session.commit()
        return RoutineUpdateResult(len(updates))
    except Exception:
        db.session.rollback()
        raise


def save_cash_updates(
    portfolio: Portfolio,
    effective_date: date,
    updates: Mapping[tuple[int, str], Decimal],
) -> RoutineUpdateResult:
    _validate_date(effective_date)
    session = build_routine_update_session(portfolio, effective_date)
    eligible = {
        (row["account_id"], row["currency_code"])
        for row in session["cash_rows"]
    }
    invalid = {
        f"cash_{account_id}_{currency}":
        "This account and currency is not a current separate cash sleeve."
        for account_id, currency in updates
        if (account_id, currency) not in eligible
    }
    if invalid:
        raise RoutineUpdateValidationError(invalid)
    warnings: list[str] = []
    try:
        for (account_id, currency), amount in updates.items():
            try:
                result = confirm_cash(
                    CashConfirmationCommand(
                        portfolio_id=portfolio.id,
                        account_id=account_id,
                        currency_code=currency,
                        effective_date=effective_date,
                        confirmed_balance_amount=amount,
                        source_note="Routine update session",
                    ),
                    commit=False,
                )
            except CashValidationError as error:
                raise RoutineUpdateValidationError(
                    {f"cash_{account_id}_{currency}": error.message}
                ) from error
            warnings.extend(result.warnings)
        db.session.commit()
        return RoutineUpdateResult(len(updates), tuple(warnings))
    except Exception:
        db.session.rollback()
        raise


def save_fx_updates(
    portfolio: Portfolio,
    effective_date: date,
    updates: Mapping[tuple[str, str], Decimal],
    *,
    acknowledgements: frozenset[tuple[str, str]] = frozenset(),
    replacements: frozenset[tuple[str, str]] = frozenset(),
) -> RoutineUpdateResult:
    _validate_date(effective_date)
    session = build_routine_update_session(portfolio, effective_date)
    if session["reference_set"]:
        raise RoutineUpdateValidationError({f"fx_{base}_{quote}": "Reference rates are maintained in Settings. Use Enter or correct rates manually."
                                            for base, quote in updates})
    eligible = {
        (row["base_currency_code"], row["quote_currency_code"])
        for row in session["fx_rows"]
    }
    errors: dict[str, str] = {}
    warnings: dict[str, FxChangeAssessment] = {}
    corrections = {}
    for (base, quote), amount in updates.items():
        field = f"fx_{base}_{quote}"
        try:
            exact_units(amount, 6)
        except (ValueError, TypeError) as error:
            errors[field] = str(error)
            continue
        if (base, quote) not in eligible:
            errors[field] = "This FX relationship is not used by the current session."
        elif not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
            errors[field] = "FX rate must be a finite number greater than zero."
        else:
            existing = same_day_fx_rates(base, quote, effective_date)
            if existing:
                try:
                    corrections[(base, quote)] = fx_correction_context(
                        existing,
                        base_currency=base,
                        quote_currency=quote,
                        quote_per_base=amount,
                    )
                except ValueError as error:
                    errors[field] = str(error)
                    continue
                if (base, quote) not in replacements:
                    errors[f"replace_{field}"] = (
                        "Confirm replacement of the existing FX source for this date."
                    )
            assessment = assess_fx_change(base, quote, effective_date, amount)
            if (
                assessment.requires_acknowledgement
                and (base, quote) not in acknowledgements
            ):
                errors[field] = (
                    "This rate differs from the latest comparable rate by more "
                    "than 10%. Check the direction and value, then acknowledge it."
                )
                warnings[field] = assessment
    if errors:
        raise RoutineUpdateValidationError(errors, fx_warnings=warnings)

    try:
        for (base, quote), amount in updates.items():
            correction = corrections.get((base, quote))
            if correction is not None:
                apply_same_day_fx_correction(correction)
            else:
                db.session.add(
                    FxRate(
                        effective_date=effective_date,
                        base_currency_code=base,
                        quote_currency_code=quote,
                        quote_per_base_amount=amount,
                    )
                )
        db.session.commit()
        return RoutineUpdateResult(len(updates))
    except Exception:
        db.session.rollback()
        raise
