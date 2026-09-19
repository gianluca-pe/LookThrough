"""Separate-cash replay, end-of-day confirmations, and data warnings."""

from __future__ import annotations

from app.decimal_policy import currency_places, require_precision

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Callable

from sqlalchemy import select

from app.conventions import normalize_currency_code, utc_now
from app.extensions import db
from app.models import (
    Account,
    CashBalanceCheckpoint,
    Portfolio,
    Posting,
    Transaction,
)


class CashValidationError(ValueError):
    """A cash business-rule failure mapped to one confirmation field."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class CashEffect:
    transaction_id: int
    transaction_type: str
    effective_date: date
    amount: Decimal


@dataclass(frozen=True)
class CashCheckpointView:
    checkpoint_id: int
    effective_date: date
    confirmed_balance_amount: Decimal
    prior_calculated_balance_amount: Decimal | None
    correction_amount: Decimal | None
    source_note: str | None


@dataclass(frozen=True)
class CashBalance:
    portfolio_id: int
    account_id: int
    account_name: str
    currency_code: str
    as_of_date: date
    amount: Decimal | None
    status: str
    source_mode: str
    checkpoint: CashCheckpointView | None
    later_cash_effect_amount: Decimal
    effects: tuple[CashEffect, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class CashConfirmationCommand:
    portfolio_id: int
    account_id: int
    currency_code: str
    effective_date: date
    confirmed_balance_amount: Decimal
    source_note: str | None = None


@dataclass(frozen=True)
class CashConfirmationResult:
    checkpoint_id: int
    prior_calculated_balance_amount: Decimal
    confirmed_balance_amount: Decimal
    correction_amount: Decimal
    superseded_checkpoint_id: int | None
    warnings: tuple[str, ...]


def resolve_cash(
    portfolio_id: int,
    account_id: int,
    currency_code: str,
    as_of_date: date,
) -> CashBalance:
    """Replay one account/currency cash sleeve as of an explicit date."""

    _validate_date(as_of_date)
    account = _load_account(portfolio_id, account_id)
    currency = _currency(currency_code)
    _validate_account_currency(account, currency)

    if account.cash_settlement_account is not None:
        target = account.cash_settlement_account
        return CashBalance(
            portfolio_id=portfolio_id,
            account_id=account.id,
            account_name=account.name,
            currency_code=currency,
            as_of_date=as_of_date,
            amount=None,
            status="settled_elsewhere",
            source_mode="settlement_account",
            checkpoint=None,
            later_cash_effect_amount=Decimal("0"),
            effects=(),
            warnings=(
                f"Activity cash settles in {target.name}; this custody account "
                "does not add a separate cash balance.",
            ),
        )

    if account.cash_tracking_mode == "included_in_aggregate":
        return CashBalance(
            portfolio_id=portfolio_id,
            account_id=account.id,
            account_name=account.name,
            currency_code=currency,
            as_of_date=as_of_date,
            amount=None,
            status="included_in_aggregate",
            source_mode="aggregate",
            checkpoint=None,
            later_cash_effect_amount=Decimal("0"),
            effects=(),
            warnings=(
                "Cash is included in this account's aggregate value and is not "
                "valued as a separate cash balance.",
            ),
        )

    checkpoint = _latest_active_checkpoint(
        account.id, currency, on_or_before=as_of_date
    )
    statement = (
        select(Posting, Transaction)
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(
            Posting.account_id == account.id,
            Posting.currency_code == currency,
            Posting.posting_kind == "cash",
            Posting.cash_amount_delta.is_not(None),
            Transaction.status == "posted",
            Transaction.reverses_transaction_id.is_(None),
            Transaction.effective_date <= as_of_date,
        )
        .order_by(Transaction.effective_date, Transaction.id, Posting.id)
    )
    if checkpoint is not None:
        statement = statement.where(
            Transaction.effective_date > checkpoint.effective_date
        )
    effects = tuple(
        CashEffect(
            transaction_id=transaction.id,
            transaction_type=transaction.transaction_type,
            effective_date=transaction.effective_date,
            amount=posting.cash_amount_delta,
        )
        for posting, transaction in db.session.execute(statement)
    )
    later_effect = sum((row.amount for row in effects), Decimal("0"))
    base_amount = (
        checkpoint.confirmed_balance_amount
        if checkpoint is not None
        else Decimal("0")
    )
    amount = base_amount + later_effect
    warnings = ()
    if amount < 0:
        warnings = (
            "Calculated cash is negative. This may reflect settlement timing, "
            "margin, or missing activity; the value is retained as entered.",
        )

    return CashBalance(
        portfolio_id=portfolio_id,
        account_id=account.id,
        account_name=account.name,
        currency_code=currency,
        as_of_date=as_of_date,
        amount=amount,
        status="confirmed" if checkpoint is not None else "calculated",
        source_mode=(
            "cash_confirmation" if checkpoint is not None else "transaction"
        ),
        checkpoint=_checkpoint_view(checkpoint),
        later_cash_effect_amount=later_effect,
        effects=effects,
        warnings=warnings,
    )


def confirm_cash(
    command: CashConfirmationCommand,
    *,
    timestamp_provider: Callable[[], datetime] = utc_now,
    commit: bool = True,
) -> CashConfirmationResult:
    """Atomically create or replace a confirmation without creating postings."""

    _validate_date(command.effective_date)
    if (
        not isinstance(command.confirmed_balance_amount, Decimal)
        or not command.confirmed_balance_amount.is_finite()
    ):
        raise CashValidationError(
            "confirmed_balance_amount", "Confirmed balance must be a decimal number."
        )
    if command.source_note is not None and len(command.source_note.strip()) > 1000:
        raise CashValidationError(
            "source_note", "Source note must be 1,000 characters or fewer."
        )

    require_precision(command.confirmed_balance_amount, currency_places(command.currency_code), CashValidationError, 'confirmed_balance_amount')
    require_precision(command.confirmed_balance_amount, 3, CashValidationError, 'confirmed_balance_amount')
    account = _load_account(command.portfolio_id, command.account_id)
    currency = _currency(command.currency_code)
    _validate_account_currency(account, currency)
    if account.cash_settlement_account is not None:
        raise CashValidationError(
            "currency_code",
            f"Activity cash settles in {account.cash_settlement_account.name}. "
            "Confirm the balance there instead.",
        )
    if account.cash_tracking_mode != "separate_cash":
        raise CashValidationError(
            "currency_code",
            "This account includes cash in an aggregate value, so a separate cash "
            "confirmation would count the same value twice.",
        )

    try:
        prior = resolve_cash(
            command.portfolio_id,
            account.id,
            currency,
            command.effective_date,
        )
        assert prior.amount is not None
        correction = command.confirmed_balance_amount - prior.amount
        replaced = _active_checkpoint_on_date(
            account.id, currency, command.effective_date
        )
        replaced_id = replaced.id if replaced is not None else None
        if replaced is not None:
            replaced.superseded_at = timestamp_provider()
            db.session.flush()

        checkpoint = CashBalanceCheckpoint(
            account_id=account.id,
            currency_code=currency,
            effective_date=command.effective_date,
            confirmed_balance_amount=command.confirmed_balance_amount,
            prior_calculated_balance_amount=prior.amount,
            correction_amount=correction,
            source_note=(
                command.source_note.strip() if command.source_note else None
            ),
        )
        db.session.add(checkpoint)
        db.session.flush()
        if commit:
            db.session.commit()
        warnings = ()
        if command.confirmed_balance_amount < 0:
            warnings = (
                "Confirmed cash is negative. The value is saved because brokers "
                "may report temporary settlement or margin balances.",
            )
        return CashConfirmationResult(
            checkpoint_id=checkpoint.id,
            prior_calculated_balance_amount=prior.amount,
            confirmed_balance_amount=command.confirmed_balance_amount,
            correction_amount=correction,
            superseded_checkpoint_id=replaced_id,
            warnings=warnings,
        )
    except Exception:
        db.session.rollback()
        raise


def backdated_cash_warning(
    portfolio_id: int,
    account_id: int,
    currency_code: str,
    effective_date: date,
) -> str | None:
    """Explain when an activity is superseded by a same/later confirmation."""

    _validate_date(effective_date)
    account = _load_account(portfolio_id, account_id)
    if account.cash_tracking_mode != "separate_cash":
        return None
    currency = _currency(currency_code)
    checkpoint = _latest_active_checkpoint(account.id, currency)
    if checkpoint is None or effective_date > checkpoint.effective_date:
        return None
    return (
        f"This activity is on or before the cash confirmation dated "
        f"{checkpoint.effective_date.isoformat()}. It changes position history "
        "but not current confirmed-and-carried-forward cash."
    )


def cash_currencies(
    portfolio_id: int,
    account_id: int,
    *,
    as_of_date: date | None = None,
) -> tuple[str, ...]:
    """List currencies with a default, posted cash effect, or active confirmation.

    When an as-of date is supplied, future activity and confirmations do not create
    a fictional historical cash sleeve. The account's default currency remains a
    candidate; callers can distinguish an untouched zero from a sourced balance by
    inspecting the resolved checkpoint and effects.
    """

    account = _load_account(portfolio_id, account_id)
    currencies = {account.default_currency_code}
    posting_statement = (
        select(Posting.currency_code)
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(
            Posting.account_id == account.id,
            Posting.posting_kind == "cash",
            Transaction.status == "posted",
            Transaction.reverses_transaction_id.is_(None),
        )
        .distinct()
    )
    checkpoint_statement = (
        select(CashBalanceCheckpoint.currency_code)
        .where(
            CashBalanceCheckpoint.account_id == account.id,
            CashBalanceCheckpoint.superseded_at.is_(None),
        )
        .distinct()
    )
    if as_of_date is not None:
        _validate_date(as_of_date)
        posting_statement = posting_statement.where(
            Transaction.effective_date <= as_of_date
        )
        checkpoint_statement = checkpoint_statement.where(
            CashBalanceCheckpoint.effective_date <= as_of_date
        )
    currencies.update(db.session.scalars(posting_statement))
    currencies.update(db.session.scalars(checkpoint_statement))
    return tuple(sorted(currencies))


def cash_setup_complete(portfolio_id: int) -> bool:
    """Return whether every current separate cash sleeve has a confirmation."""

    if db.session.get(Portfolio, portfolio_id) is None:
        raise CashValidationError("portfolio_id", "Portfolio was not found.")
    accounts = list(
        db.session.scalars(
            select(Account)
            .where(
                Account.portfolio_id == portfolio_id,
                Account.is_active.is_(True),
            )
            .order_by(Account.id)
        )
    )
    if not accounts:
        return False
    for account in accounts:
        if (
            account.cash_tracking_mode == "included_in_aggregate"
            or account.cash_settlement_account_id is not None
        ):
            continue
        for currency in cash_currencies(portfolio_id, account.id):
            if _latest_active_checkpoint(account.id, currency) is None:
                return False
    return True


def _load_account(portfolio_id: int, account_id: int) -> Account:
    if db.session.get(Portfolio, portfolio_id) is None:
        raise CashValidationError("portfolio_id", "Portfolio was not found.")
    account = db.session.get(Account, account_id)
    if (
        account is None
        or account.portfolio_id != portfolio_id
        or not account.is_active
    ):
        raise CashValidationError(
            "account_id", "Choose an active account in this portfolio."
        )
    return account


def _currency(value: str) -> str:
    try:
        return normalize_currency_code(value, field_name="Currency")
    except (TypeError, ValueError) as exc:
        raise CashValidationError("currency_code", str(exc)) from exc


def _validate_account_currency(account: Account, currency: str) -> None:
    if not account.is_multicurrency and account.default_currency_code != currency:
        raise CashValidationError(
            "currency_code",
            "Currency must match this single-currency account.",
        )


def _validate_date(value: date) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise CashValidationError("effective_date", "Enter a valid effective date.")


def _latest_active_checkpoint(
    account_id: int,
    currency_code: str,
    *,
    on_or_before: date | None = None,
) -> CashBalanceCheckpoint | None:
    statement = select(CashBalanceCheckpoint).where(
        CashBalanceCheckpoint.account_id == account_id,
        CashBalanceCheckpoint.currency_code == currency_code,
        CashBalanceCheckpoint.superseded_at.is_(None),
    )
    if on_or_before is not None:
        statement = statement.where(
            CashBalanceCheckpoint.effective_date <= on_or_before
        )
    return db.session.scalar(
        statement.order_by(
            CashBalanceCheckpoint.effective_date.desc(),
            CashBalanceCheckpoint.id.desc(),
        ).limit(1)
    )


def _active_checkpoint_on_date(
    account_id: int, currency_code: str, effective_date: date
) -> CashBalanceCheckpoint | None:
    return db.session.scalar(
        select(CashBalanceCheckpoint).where(
            CashBalanceCheckpoint.account_id == account_id,
            CashBalanceCheckpoint.currency_code == currency_code,
            CashBalanceCheckpoint.effective_date == effective_date,
            CashBalanceCheckpoint.superseded_at.is_(None),
        )
    )


def _checkpoint_view(
    checkpoint: CashBalanceCheckpoint | None,
) -> CashCheckpointView | None:
    if checkpoint is None:
        return None
    return CashCheckpointView(
        checkpoint_id=checkpoint.id,
        effective_date=checkpoint.effective_date,
        confirmed_balance_amount=checkpoint.confirmed_balance_amount,
        prior_calculated_balance_amount=checkpoint.prior_calculated_balance_amount,
        correction_amount=checkpoint.correction_amount,
        source_note=checkpoint.source_note,
    )
