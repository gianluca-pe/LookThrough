"""Simple fixed-deposit terms and as-of maturity state."""

from __future__ import annotations

from app.decimal_policy import currency_places, require_precision

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.conventions import normalize_currency_code
from app.extensions import db
from app.services.ledger_writes import ledger_write
from app.models import (
    Account,
    MATURITY_ACTION_CODES,
    FixedDeposit,
    Instrument,
    Portfolio,
    PositionRegistration,
    Posting,
    Transaction,
    ValuationObservation,
)


APPROACHING_MATURITY_DAYS = 30
MATURITY_DISPOSITION_CODES = ("return_to_cash", "rollover", "manual")


class FixedDepositValidationError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class FixedDepositCommand:
    registration_id: int
    currency_code: str
    start_date: date
    maturity_date: date
    annual_rate_decimal: Decimal | None
    maturity_action: str
    expected_maturity_proceeds_amount: Decimal | None = None
    notes: str | None = None


@dataclass(frozen=True)
class FixedDepositSnapshot:
    terms_id: int | None
    registration_id: int
    currency_code: str
    start_date: date | None
    maturity_date: date | None
    annual_rate_decimal: Decimal | None
    expected_maturity_proceeds_amount: Decimal | None
    maturity_action: str | None
    notes: str | None
    maturity_status: str
    days_to_maturity: int | None
    is_term_liquidity: bool
    requires_attention: bool


@dataclass(frozen=True)
class MaturityDispositionCommand:
    portfolio_id: int
    registration_id: int
    disposition_type: str
    effective_date: date
    confirmed_principal_amount: Decimal
    confirmed_interest_amount: Decimal
    cash_account_id: int | None = None
    note: str | None = None
    successor_instrument_name: str | None = None
    successor_reference: str | None = None
    successor_maturity_date: date | None = None
    successor_annual_rate_decimal: Decimal | None = None
    successor_expected_proceeds_amount: Decimal | None = None
    successor_maturity_action: str = "undecided"


@dataclass(frozen=True)
class MaturityDispositionPreview:
    registration_id: int
    instrument_name: str
    account_name: str
    disposition_type: str
    effective_date: date
    currency_code: str
    confirmed_principal_amount: Decimal
    confirmed_interest_amount: Decimal
    cash_account_id: int | None
    cash_account_name: str | None
    cash_effect_amount: Decimal
    successor_instrument_name: str | None
    successor_maturity_date: date | None
    expected_maturity_proceeds_amount: Decimal | None
    expected_difference_amount: Decimal | None
    note: str | None


@dataclass(frozen=True)
class PostedMaturityDisposition:
    transaction_id: int
    successor_registration_id: int | None
    preview: MaturityDispositionPreview


def terms_by_registration_ids(
    registration_ids: list[int],
) -> dict[int, FixedDeposit]:
    if not registration_ids:
        return {}
    rows = db.session.scalars(
        select(FixedDeposit).where(
            FixedDeposit.position_registration_id.in_(registration_ids)
        )
    )
    return {row.position_registration_id: row for row in rows}


def maturity_snapshot(
    registration: PositionRegistration,
    terms: FixedDeposit | None,
    as_of_date: date,
) -> FixedDepositSnapshot:
    if registration.instrument.instrument_type != "fixed_deposit":
        raise ValueError("Maturity status is only available for fixed deposits")
    if terms is None:
        return FixedDepositSnapshot(
            terms_id=None,
            registration_id=registration.id,
            currency_code=registration.instrument.valuation_currency_code,
            start_date=registration.opening_date,
            maturity_date=None,
            annual_rate_decimal=None,
            expected_maturity_proceeds_amount=None,
            maturity_action=None,
            notes=None,
            maturity_status="missing_terms",
            days_to_maturity=None,
            is_term_liquidity=True,
            requires_attention=True,
        )

    if registration.closing_date is not None and registration.closing_date <= as_of_date:
        return FixedDepositSnapshot(
            terms_id=terms.id,
            registration_id=registration.id,
            currency_code=terms.currency_code,
            start_date=terms.start_date,
            maturity_date=terms.maturity_date,
            annual_rate_decimal=terms.annual_rate_decimal,
            expected_maturity_proceeds_amount=(
                terms.expected_maturity_proceeds_amount
            ),
            maturity_action=terms.maturity_action,
            notes=terms.notes,
            maturity_status="disposed",
            days_to_maturity=(terms.maturity_date - as_of_date).days,
            is_term_liquidity=False,
            requires_attention=False,
        )

    days_to_maturity = (terms.maturity_date - as_of_date).days
    if days_to_maturity < 0:
        status = "overdue"
    elif days_to_maturity == 0:
        status = "due"
    elif days_to_maturity <= APPROACHING_MATURITY_DAYS:
        status = "approaching"
    else:
        status = "active"
    return FixedDepositSnapshot(
        terms_id=terms.id,
        registration_id=registration.id,
        currency_code=terms.currency_code,
        start_date=terms.start_date,
        maturity_date=terms.maturity_date,
        annual_rate_decimal=terms.annual_rate_decimal,
        expected_maturity_proceeds_amount=(
            terms.expected_maturity_proceeds_amount
        ),
        maturity_action=terms.maturity_action,
        notes=terms.notes,
        maturity_status=status,
        days_to_maturity=days_to_maturity,
        is_term_liquidity=as_of_date < terms.maturity_date,
        requires_attention=(
            status in {"due", "overdue"}
            or (status == "approaching" and terms.maturity_action == "undecided")
        ),
    )


def save_fixed_deposit_terms(
    portfolio_id: int,
    command: FixedDepositCommand,
) -> FixedDeposit:
    registration = db.session.scalar(
        select(PositionRegistration)
        .join(Instrument)
        .where(
            PositionRegistration.id == command.registration_id,
            Instrument.portfolio_id == portfolio_id,
        )
    )
    if registration is None:
        raise FixedDepositValidationError(
            "registration_id", "Fixed-deposit position was not found."
        )
    if registration.instrument.instrument_type != "fixed_deposit":
        raise FixedDepositValidationError(
            "registration_id", "Terms can only be saved for a fixed-deposit position."
        )

    try:
        currency_code = normalize_currency_code(
            command.currency_code, field_name="Currency"
        )
    except (TypeError, ValueError) as exc:
        raise FixedDepositValidationError("currency_code", str(exc)) from exc
    if currency_code != registration.instrument.valuation_currency_code:
        raise FixedDepositValidationError(
            "currency_code",
            "Currency must match the fixed deposit's valuation currency.",
        )
    if registration.opening_date and command.start_date != registration.opening_date:
        raise FixedDepositValidationError(
            "start_date",
            "Start date must match the position's opening date "
            f"({registration.opening_date.isoformat()}).",
        )
    if command.maturity_date <= command.start_date:
        raise FixedDepositValidationError(
            "maturity_date", "Maturity date must be after the start date."
        )
    if command.annual_rate_decimal is not None:
        if not command.annual_rate_decimal.is_finite():
            raise FixedDepositValidationError(
                "annual_rate_percent", "Annual rate must be a finite decimal number."
            )
        if command.annual_rate_decimal < 0:
            raise FixedDepositValidationError(
                "annual_rate_percent", "Annual rate cannot be negative."
            )
    _validate_positive_optional_amount(
        command.expected_maturity_proceeds_amount,
        field="expected_maturity_proceeds_amount",
        label="Expected maturity proceeds",
    )
    if command.maturity_action not in MATURITY_ACTION_CODES:
        raise FixedDepositValidationError(
            "maturity_action", "Choose a supported maturity action."
        )

    terms = db.session.scalar(
        select(FixedDeposit).where(
            FixedDeposit.position_registration_id == registration.id
        )
    )
    if terms is None:
        terms = FixedDeposit(position_registration_id=registration.id)
        db.session.add(terms)
    terms.currency_code = currency_code
    terms.start_date = command.start_date
    terms.maturity_date = command.maturity_date
    if command.annual_rate_decimal is not None:
        require_precision(command.annual_rate_decimal, 8, FixedDepositValidationError, 'annual_rate_decimal')
    if command.expected_maturity_proceeds_amount is not None:
        require_precision(command.expected_maturity_proceeds_amount, currency_places(currency_code), FixedDepositValidationError, 'expected_maturity_proceeds_amount')
        require_precision(command.expected_maturity_proceeds_amount, 3, FixedDepositValidationError, 'expected_maturity_proceeds_amount')
    terms.annual_rate_decimal = command.annual_rate_decimal
    terms.expected_maturity_proceeds_amount = (
        command.expected_maturity_proceeds_amount
    )
    terms.maturity_action = command.maturity_action
    terms.notes = command.notes or None
    db.session.flush()
    return terms


def latest_positive_principal(
    registration_id: int, *, on_or_before: date
) -> Decimal | None:
    """Return the latest positive statement amount suitable as an editable prefill."""

    observation = db.session.scalar(
        select(ValuationObservation)
        .where(
            ValuationObservation.position_registration_id == registration_id,
            ValuationObservation.effective_date <= on_or_before,
            ValuationObservation.native_value_amount > 0,
        )
        .order_by(
            ValuationObservation.effective_date.desc(),
            ValuationObservation.id.desc(),
        )
        .limit(1)
    )
    return observation.native_value_amount if observation is not None else None


def preview_maturity_disposition(
    command: MaturityDispositionCommand,
) -> MaturityDispositionPreview:
    registration, terms = _disposition_registration(command)
    principal = _positive_amount(
        command.confirmed_principal_amount,
        "confirmed_principal_amount",
        "Confirmed principal",
    )
    interest = _nonnegative_amount(
        command.confirmed_interest_amount,
        "confirmed_interest_amount",
        "Confirmed interest",
    )
    for field in ('confirmed_principal_amount', 'confirmed_interest_amount', 'successor_expected_proceeds_amount'):
        value = getattr(command, field, None)
        if value is not None:
            require_precision(value, currency_places(terms.currency_code), FixedDepositValidationError, field)
            require_precision(value, 3, FixedDepositValidationError, field)
    if command.disposition_type not in MATURITY_DISPOSITION_CODES:
        raise FixedDepositValidationError(
            "disposition_type", "Choose a supported maturity outcome."
        )
    if command.effective_date < terms.maturity_date:
        raise FixedDepositValidationError(
            "effective_date",
            "Disposition date cannot be before the fixed deposit matures.",
        )
    if registration.closing_date is not None:
        raise FixedDepositValidationError(
            "disposition_type", "This fixed deposit already has a disposition."
        )

    cash_account = None
    if command.disposition_type in {"return_to_cash", "rollover"}:
        if command.cash_account_id is None:
            raise FixedDepositValidationError(
                "cash_account_id", "Choose the account that receives cash."
            )
        cash_account = _cash_destination(
            command.portfolio_id,
            command.cash_account_id,
            terms.currency_code,
        )
    elif not (command.note or "").strip():
        raise FixedDepositValidationError(
            "note", "Explain the confirmed manual disposition."
        )

    successor_name = None
    successor_maturity = None
    if command.disposition_type == "rollover":
        successor_name = (command.successor_instrument_name or "").strip()
        if not successor_name:
            raise FixedDepositValidationError(
                "successor_instrument_name", "Enter the successor deposit name."
            )
        if len(successor_name) > 200:
            raise FixedDepositValidationError(
                "successor_instrument_name",
                "Successor deposit name must be 200 characters or fewer.",
            )
        successor_maturity = command.successor_maturity_date
        if successor_maturity is None or successor_maturity <= command.effective_date:
            raise FixedDepositValidationError(
                "successor_maturity_date",
                "Successor maturity date must be after the disposition date.",
            )
        if command.successor_annual_rate_decimal is not None:
            require_precision(command.successor_annual_rate_decimal, 8, FixedDepositValidationError, 'successor_annual_rate_percent')
        _validate_rate(
            command.successor_annual_rate_decimal,
            field="successor_annual_rate_percent",
        )
        _validate_positive_optional_amount(
            command.successor_expected_proceeds_amount,
            field="successor_expected_proceeds_amount",
            label="Successor expected proceeds",
        )
        if command.successor_maturity_action not in MATURITY_ACTION_CODES:
            raise FixedDepositValidationError(
                "successor_maturity_action",
                "Choose a supported successor maturity action.",
            )

    expected = terms.expected_maturity_proceeds_amount
    confirmed_total = principal + interest
    return MaturityDispositionPreview(
        registration_id=registration.id,
        instrument_name=registration.instrument.name,
        account_name=registration.account.name,
        disposition_type=command.disposition_type,
        effective_date=command.effective_date,
        currency_code=terms.currency_code,
        confirmed_principal_amount=principal,
        confirmed_interest_amount=interest,
        cash_account_id=cash_account.id if cash_account is not None else None,
        cash_account_name=cash_account.name if cash_account is not None else None,
        cash_effect_amount=(
            confirmed_total
            if command.disposition_type == "return_to_cash"
            else interest if command.disposition_type == "rollover" else Decimal("0")
        ),
        successor_instrument_name=successor_name,
        successor_maturity_date=successor_maturity,
        expected_maturity_proceeds_amount=expected,
        expected_difference_amount=(
            confirmed_total - expected if expected is not None else None
        ),
        note=(command.note or "").strip() or None,
    )


def post_maturity_disposition(
    command: MaturityDispositionCommand,
) -> PostedMaturityDisposition:
    """Atomically close one matured deposit and persist only confirmed effects."""

    with ledger_write(FixedDepositValidationError, "disposition_type"):
        preview = preview_maturity_disposition(command)
        registration, terms = _disposition_registration(command)
        transaction = Transaction(
            portfolio_id=command.portfolio_id,
            transaction_type="fixed_deposit_maturity",
            effective_date=command.effective_date,
            status="posted",
            note=_disposition_note(preview),
        )
        db.session.add(transaction)
        db.session.flush()
        db.session.add(
            Posting(
                transaction_id=transaction.id,
                account_id=registration.account_id,
                posting_kind="clearing",
                instrument_id=registration.instrument_id,
                currency_code=terms.currency_code,
                cash_amount_delta=-preview.confirmed_principal_amount,
            )
        )

        successor_registration = None
        if command.disposition_type == "rollover":
            successor = Instrument(
                portfolio_id=command.portfolio_id,
                name=preview.successor_instrument_name,
                ticker_or_isin=(command.successor_reference or "").strip() or None,
                instrument_type="fixed_deposit",
                valuation_currency_code=terms.currency_code,
                is_active=True,
            )
            db.session.add(successor)
            db.session.flush()
            successor_registration = PositionRegistration(
                account_id=registration.account_id,
                instrument_id=successor.id,
                tracking_mode="statement_valued",
                opening_date=command.effective_date,
            )
            db.session.add(successor_registration)
            db.session.flush()
            db.session.add_all(
                [
                    ValuationObservation(
                        position_registration_id=successor_registration.id,
                        effective_date=command.effective_date,
                        native_value_amount=preview.confirmed_principal_amount,
                        currency_code=terms.currency_code,
                    ),
                    FixedDeposit(
                        position_registration_id=successor_registration.id,
                        currency_code=terms.currency_code,
                        start_date=command.effective_date,
                        maturity_date=preview.successor_maturity_date,
                        annual_rate_decimal=command.successor_annual_rate_decimal,
                        expected_maturity_proceeds_amount=(
                            command.successor_expected_proceeds_amount
                        ),
                        maturity_action=command.successor_maturity_action,
                    ),
                    Posting(
                        transaction_id=transaction.id,
                        account_id=registration.account_id,
                        posting_kind="clearing",
                        instrument_id=successor.id,
                        currency_code=terms.currency_code,
                        cash_amount_delta=preview.confirmed_principal_amount,
                    ),
                ]
            )

        if preview.confirmed_interest_amount:
            db.session.add(
                Posting(
                    transaction_id=transaction.id,
                    account_id=registration.account_id,
                    posting_kind="income",
                    instrument_id=registration.instrument_id,
                    currency_code=terms.currency_code,
                    cash_amount_delta=preview.confirmed_interest_amount,
                )
            )
        if preview.cash_effect_amount:
            db.session.add(
                Posting(
                    transaction_id=transaction.id,
                    account_id=preview.cash_account_id,
                    posting_kind="cash",
                    instrument_id=None,
                    currency_code=terms.currency_code,
                    cash_amount_delta=preview.cash_effect_amount,
                )
            )
        registration.closing_date = command.effective_date
        result = PostedMaturityDisposition(
            transaction_id=transaction.id,
            successor_registration_id=(
                successor_registration.id
                if successor_registration is not None
                else None
            ),
            preview=preview,
        )
        db.session.commit()
        return result


def reverse_maturity_disposition_side_effects(transaction: Transaction) -> None:
    """Restore registration visibility when the generic reversal reverses an FD maturity."""

    if transaction.transaction_type != "fixed_deposit_maturity":
        return
    clearing = [
        posting
        for posting in transaction.postings
        if posting.posting_kind == "clearing"
        and posting.instrument_id is not None
        and posting.cash_amount_delta is not None
    ]
    original = next((row for row in clearing if row.cash_amount_delta < 0), None)
    successor = next((row for row in clearing if row.cash_amount_delta > 0), None)
    if original is None:
        raise FixedDepositValidationError(
            "transaction_id", "Maturity activity is missing its original deposit."
        )
    original_registration = db.session.scalar(
        select(PositionRegistration).where(
            PositionRegistration.account_id == original.account_id,
            PositionRegistration.instrument_id == original.instrument_id,
        )
    )
    if original_registration is None:
        raise FixedDepositValidationError(
            "transaction_id", "Original fixed-deposit position was not found."
        )
    original_registration.closing_date = None
    if successor is not None:
        successor_registration = db.session.scalar(
            select(PositionRegistration).where(
                PositionRegistration.account_id == successor.account_id,
                PositionRegistration.instrument_id == successor.instrument_id,
            )
        )
        if successor_registration is None:
            raise FixedDepositValidationError(
                "transaction_id", "Successor fixed-deposit position was not found."
            )
        successor_registration.closing_date = transaction.effective_date


def _disposition_registration(
    command: MaturityDispositionCommand,
) -> tuple[PositionRegistration, FixedDeposit]:
    if db.session.get(Portfolio, command.portfolio_id) is None:
        raise FixedDepositValidationError("portfolio_id", "Portfolio was not found.")
    registration = db.session.scalar(
        select(PositionRegistration)
        .join(Instrument)
        .where(
            PositionRegistration.id == command.registration_id,
            Instrument.portfolio_id == command.portfolio_id,
            Instrument.instrument_type == "fixed_deposit",
            PositionRegistration.tracking_mode == "statement_valued",
        )
    )
    if registration is None or registration.fixed_deposit is None:
        raise FixedDepositValidationError(
            "registration_id", "Fixed-deposit terms were not found."
        )
    return registration, registration.fixed_deposit


def _cash_destination(
    portfolio_id: int, account_id: int, currency_code: str
) -> Account:
    account = db.session.get(Account, account_id)
    if account is None or account.portfolio_id != portfolio_id or not account.is_active:
        raise FixedDepositValidationError(
            "cash_account_id", "Choose an active cash account in this portfolio."
        )
    if account.cash_tracking_mode != "separate_cash" or account.cash_settlement_account_id:
        raise FixedDepositValidationError(
            "cash_account_id",
            "Choose an account whose cash is tracked separately here.",
        )
    if not account.is_multicurrency and account.default_currency_code != currency_code:
        raise FixedDepositValidationError(
            "cash_account_id", f"Choose an account that accepts {currency_code} cash."
        )
    return account


def _positive_amount(value: Decimal, field: str, label: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise FixedDepositValidationError(field, f"{label} must be a positive amount.")
    return value


def _nonnegative_amount(value: Decimal, field: str, label: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise FixedDepositValidationError(
            field, f"{label} must be zero or a positive amount."
        )
    return value


def _validate_positive_optional_amount(
    value: Decimal | None, *, field: str, label: str
) -> None:
    if value is None:
        return
    if not value.is_finite() or value <= 0:
        raise FixedDepositValidationError(field, f"{label} must be positive.")


def _validate_rate(value: Decimal | None, *, field: str) -> None:
    if value is None:
        return
    if not value.is_finite() or value < 0:
        raise FixedDepositValidationError(
            field, "Annual rate must be zero or a positive finite number."
        )


def _disposition_note(preview: MaturityDispositionPreview) -> str:
    labels = {
        "return_to_cash": "Returned principal and interest to cash",
        "rollover": "Rolled principal into a successor fixed deposit",
        "manual": "Recorded another confirmed maturity disposition",
    }
    amounts = (
        f"Confirmed principal {preview.currency_code} "
        f"{preview.confirmed_principal_amount}; confirmed interest "
        f"{preview.currency_code} {preview.confirmed_interest_amount}."
    )
    return " ".join(
        part for part in (labels[preview.disposition_type] + ".", amounts, preview.note) if part
    )
