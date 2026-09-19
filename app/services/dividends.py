"""Dividend cash and linked reinvestment activity contracts."""

from __future__ import annotations

from app.decimal_policy import currency_places, money, require_precision, rounded

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from sqlalchemy import select

from app.conventions import normalize_currency_code
from app.extensions import db
from app.models import (
    Account,
    Instrument,
    Portfolio,
    PositionRegistration,
    Posting,
    Transaction,
)
from app.services.activity import (
    ActivityValidationError,
    TRADE_INELIGIBLE_INSTRUMENT_TYPES,
    TradeCommand,
    TradePreview,
    _add_trade_preview,
    get_trade_receipt,
    preview_trade,
)
from app.services.cash import backdated_cash_warning
from app.services.settlement import SettlementAccountError, cash_settlement_account
from app.services.ledger_writes import ledger_write


DIVIDEND_OUTCOMES = ("cash", "reinvest_same", "reinvest_other")


@dataclass(frozen=True)
class DividendCommand:
    portfolio_id: int
    effective_date: date
    account_id: int
    instrument_id: int
    currency_code: str
    net_amount: Decimal
    outcome: str
    gross_amount: Decimal | None = None
    withholding_amount: Decimal | None = None
    reinvestment_instrument_id: int | None = None
    reinvestment_quantity: Decimal | None = None
    reinvestment_unit_price: Decimal | None = None
    reinvestment_purchase_amount: Decimal | None = None
    reinvestment_fee_amount: Decimal = Decimal("0")


@dataclass(frozen=True)
class DividendPreview:
    effective_date: date
    account_id: int
    account_name: str
    cash_account_id: int
    cash_account_name: str
    instrument_id: int
    instrument_name: str
    currency_code: str
    outcome: str
    net_amount: Decimal
    income_amount: Decimal
    gross_amount: Decimal | None
    withholding_amount: Decimal
    reinvestment_instrument_id: int | None
    reinvestment_instrument_name: str | None
    reinvestment_quantity: Decimal | None
    reinvestment_unit_price: Decimal | None
    reinvestment_purchase_amount: Decimal | None
    reinvestment_fee_amount: Decimal
    cash_effect: Decimal
    buy_preview: TradePreview | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class PostedDividend:
    dividend_transaction_id: int
    buy_transaction_id: int | None
    activity_group_id: str | None
    preview: DividendPreview


@dataclass(frozen=True)
class DividendReceipt:
    dividend_transaction_id: int
    buy_transaction_id: int | None
    activity_group_id: str | None
    effective_date: date
    account_name: str
    cash_account_id: int
    cash_account_name: str
    instrument_name: str
    currency_code: str
    outcome: str
    net_amount: Decimal
    income_amount: Decimal
    withholding_amount: Decimal
    reinvestment_instrument_name: str | None
    reinvestment_quantity: Decimal | None
    reinvestment_unit_price: Decimal | None
    reinvestment_purchase_amount: Decimal | None
    reinvestment_fee_amount: Decimal
    cash_effect: Decimal


def preview_dividend(command: DividendCommand) -> DividendPreview:
    """Validate one dividend outcome and return exact effects without writes."""

    _validate_command_shape(command)
    portfolio = db.session.get(Portfolio, command.portfolio_id)
    if portfolio is None:
        raise ActivityValidationError("portfolio_id", "Portfolio was not found.")
    account = db.session.get(Account, command.account_id)
    if (
        account is None
        or account.portfolio_id != portfolio.id
        or not account.is_active
    ):
        raise ActivityValidationError(
            "account_id", "Choose an active account in this portfolio."
        )
    instrument = db.session.get(Instrument, command.instrument_id)
    if (
        instrument is None
        or instrument.portfolio_id != portfolio.id
        or not instrument.is_active
    ):
        raise ActivityValidationError(
            "instrument_id", "Choose an active instrument in this portfolio."
        )
    if instrument.instrument_type in TRADE_INELIGIBLE_INSTRUMENT_TYPES:
        raise ActivityValidationError(
            "instrument_id",
            "Cash and fixed deposits do not produce dividend activities. Use "
            "their dedicated cash or maturity workflows instead.",
        )
    registration = db.session.scalar(
        select(PositionRegistration).where(
            PositionRegistration.account_id == account.id,
            PositionRegistration.instrument_id == instrument.id,
        )
    )
    if registration is None:
        raise ActivityValidationError(
            "instrument_id",
            "Choose an instrument registered in the selected account.",
        )

    try:
        currency = normalize_currency_code(
            command.currency_code,
            field_name="Dividend currency",
        )
    except (TypeError, ValueError) as exc:
        raise ActivityValidationError("currency_code", str(exc)) from exc
    if not account.is_multicurrency and account.default_currency_code != currency:
        raise ActivityValidationError(
            "currency_code",
            "Dividend currency must match this single-currency account.",
        )
    try:
        cash_account = cash_settlement_account(account, currency)
    except SettlementAccountError as exc:
        raise ActivityValidationError("account_id", str(exc)) from exc

    for field in ('gross_amount', 'net_amount', 'withholding_amount', 'reinvestment_purchase_amount', 'reinvestment_fee_amount'):
        value = getattr(command, field, None)
        if value is not None:
            require_precision(value, currency_places(currency), ActivityValidationError, field)
            require_precision(value, 3, ActivityValidationError, field)
    withholding = command.withholding_amount or Decimal("0")
    if withholding > 0 and command.gross_amount is None:
        raise ActivityValidationError(
            "gross_amount",
            "Enter the gross distribution when withholding is provided.",
        )
    income_amount = command.gross_amount or command.net_amount
    if income_amount - withholding != command.net_amount:
        raise ActivityValidationError(
            "net_amount",
            "Gross distribution minus entered withholding must equal the net "
            "amount exactly.",
        )

    warnings: list[str] = []
    if cash_account.cash_tracking_mode == "included_in_aggregate":
        warnings.append(
            "Cash is included in this account's aggregate value. The dividend is "
            "recorded, but its cash effect is not valued as a separate balance."
        )
    backdated_warning = backdated_cash_warning(
        portfolio.id,
        cash_account.id,
        currency,
        command.effective_date,
    )
    if backdated_warning is not None:
        warnings.append(backdated_warning)

    buy_preview = None
    target_id = None
    target_name = None
    quantity = None
    unit_price = None
    purchase_amount = None
    fee_amount = Decimal("0")
    cash_effect = command.net_amount
    if command.outcome != "cash":
        target_id = (
            instrument.id
            if command.outcome == "reinvest_same"
            else command.reinvestment_instrument_id
        )
        if target_id is None:
            raise ActivityValidationError(
                "reinvestment_instrument_id",
                "Choose the instrument bought with the dividend.",
            )
        if command.outcome == "reinvest_other" and target_id == instrument.id:
            raise ActivityValidationError(
                "reinvestment_instrument_id",
                "Choose a different instrument, or use Reinvested in the same "
                "instrument.",
            )
        quantity = command.reinvestment_quantity
        assert quantity is not None
        fee_amount = command.reinvestment_fee_amount
        unit_price, purchase_amount = _resolve_reinvestment_price(command)
        base_buy_preview = preview_trade(
            TradeCommand(
                portfolio_id=portfolio.id,
                activity_type="buy",
                effective_date=command.effective_date,
                account_id=account.id,
                instrument_id=target_id,
                quantity=quantity,
                unit_price=unit_price,
                fee_amount=fee_amount,
            )
        )
        if base_buy_preview.settlement_currency != currency:
            raise ActivityValidationError(
                (
                    "reinvestment_instrument_id"
                    if command.outcome == "reinvest_other"
                    else "currency_code"
                ),
                "Dividend and reinvestment purchase must use the same currency. "
                "Cross-currency reinvestment is not available in this step.",
            )
        buy_preview = replace(
            base_buy_preview,
            gross_amount=purchase_amount,
            cash_effect=-(purchase_amount + fee_amount),
        )
        target_name = buy_preview.instrument_name
        cash_effect = command.net_amount + buy_preview.cash_effect
        if cash_effect < 0:
            warnings.append(
                "The reinvestment uses more than the net dividend; the difference "
                "reduces account cash."
            )

    return DividendPreview(
        effective_date=command.effective_date,
        account_id=account.id,
        account_name=account.name,
        cash_account_id=cash_account.id,
        cash_account_name=cash_account.name,
        instrument_id=instrument.id,
        instrument_name=instrument.name,
        currency_code=currency,
        outcome=command.outcome,
        net_amount=command.net_amount,
        income_amount=income_amount,
        gross_amount=command.gross_amount,
        withholding_amount=withholding,
        reinvestment_instrument_id=target_id,
        reinvestment_instrument_name=target_name,
        reinvestment_quantity=quantity,
        reinvestment_unit_price=unit_price,
        reinvestment_purchase_amount=purchase_amount,
        reinvestment_fee_amount=fee_amount,
        cash_effect=cash_effect,
        buy_preview=buy_preview,
        warnings=tuple(warnings),
    )


def post_dividend(
    command: DividendCommand,
    *,
    group_id_provider: Callable[[], str] = lambda: uuid4().hex,
) -> PostedDividend:
    """Atomically post dividend income and its optional linked Buy."""

    with ledger_write(ActivityValidationError, "account_id"):
        preview = preview_dividend(command)
        group_id = group_id_provider() if preview.buy_preview is not None else None
        dividend = Transaction(
            portfolio_id=command.portfolio_id,
            transaction_type="dividend",
            effective_date=preview.effective_date,
            status="posted",
            activity_group_id=group_id,
        )
        db.session.add(dividend)
        db.session.flush()
        postings = [
            Posting(
                transaction_id=dividend.id,
                account_id=preview.account_id,
                posting_kind="income",
                instrument_id=preview.instrument_id,
                currency_code=preview.currency_code,
                cash_amount_delta=-preview.income_amount,
            ),
            Posting(
                transaction_id=dividend.id,
                account_id=preview.cash_account_id,
                posting_kind="cash",
                instrument_id=preview.instrument_id,
                currency_code=preview.currency_code,
                cash_amount_delta=preview.net_amount,
            ),
        ]
        if preview.withholding_amount:
            postings.append(
                Posting(
                    transaction_id=dividend.id,
                    account_id=preview.account_id,
                    posting_kind="expense",
                    instrument_id=preview.instrument_id,
                    currency_code=preview.currency_code,
                    cash_amount_delta=preview.withholding_amount,
                )
            )
        db.session.add_all(postings)

        buy_transaction_id = None
        if preview.buy_preview is not None:
            buy, _ = _add_trade_preview(
                preview.buy_preview,
                portfolio_id=command.portfolio_id,
                activity_group_id=group_id,
            )
            buy_transaction_id = buy.id
        result = PostedDividend(
            dividend_transaction_id=dividend.id,
            buy_transaction_id=buy_transaction_id,
            activity_group_id=group_id,
            preview=preview,
        )
        db.session.commit()
        return result


def get_dividend_receipt(
    transaction_id: int, *, portfolio_id: int
) -> DividendReceipt | None:
    """Rebuild a dividend success receipt from persisted postings and group."""

    transaction = db.session.scalar(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.portfolio_id == portfolio_id,
            Transaction.transaction_type == "dividend",
            Transaction.status == "posted",
        )
    )
    if transaction is None:
        return None
    income = next(
        (row for row in transaction.postings if row.posting_kind == "income"),
        None,
    )
    cash = next(
        (row for row in transaction.postings if row.posting_kind == "cash"),
        None,
    )
    withholding = next(
        (row for row in transaction.postings if row.posting_kind == "expense"),
        None,
    )
    if income is None or cash is None or income.instrument_id is None:
        return None
    account = db.session.get(Account, income.account_id)
    cash_account = db.session.get(Account, cash.account_id)
    instrument = db.session.get(Instrument, income.instrument_id)
    if account is None or cash_account is None or instrument is None:
        return None

    buy_receipt = None
    if transaction.activity_group_id is not None:
        linked_buy = db.session.scalar(
            select(Transaction)
            .where(
                Transaction.portfolio_id == portfolio_id,
                Transaction.transaction_type == "buy",
                Transaction.status == "posted",
                Transaction.activity_group_id == transaction.activity_group_id,
            )
            .order_by(Transaction.id)
            .limit(1)
        )
        if linked_buy is None:
            return None
        buy_receipt = get_trade_receipt(linked_buy.id, portfolio_id=portfolio_id)
        if buy_receipt is None:
            return None

    net_amount = cash.cash_amount_delta or Decimal("0")
    income_amount = abs(income.cash_amount_delta or Decimal("0"))
    withholding_amount = (
        withholding.cash_amount_delta
        if withholding is not None and withholding.cash_amount_delta is not None
        else Decimal("0")
    )
    outcome = "cash"
    if buy_receipt is not None:
        outcome = (
            "reinvest_same"
            if buy_receipt.instrument_id == instrument.id
            else "reinvest_other"
        )
    return DividendReceipt(
        dividend_transaction_id=transaction.id,
        buy_transaction_id=(
            buy_receipt.transaction_id if buy_receipt is not None else None
        ),
        activity_group_id=transaction.activity_group_id,
        effective_date=transaction.effective_date,
        account_name=account.name,
        cash_account_id=cash_account.id,
        cash_account_name=cash_account.name,
        instrument_name=instrument.name,
        currency_code=cash.currency_code,
        outcome=outcome,
        net_amount=net_amount,
        income_amount=income_amount,
        withholding_amount=withholding_amount,
        reinvestment_instrument_name=(
            buy_receipt.instrument_name if buy_receipt is not None else None
        ),
        reinvestment_quantity=(
            buy_receipt.quantity if buy_receipt is not None else None
        ),
        reinvestment_unit_price=(
            buy_receipt.unit_price if buy_receipt is not None else None
        ),
        reinvestment_purchase_amount=(
            buy_receipt.gross_amount if buy_receipt is not None else None
        ),
        reinvestment_fee_amount=(
            buy_receipt.fee_amount if buy_receipt is not None else Decimal("0")
        ),
        cash_effect=(
            net_amount + buy_receipt.cash_effect
            if buy_receipt is not None
            else net_amount
        ),
    )


def _resolve_reinvestment_price(
    command: DividendCommand,
) -> tuple[Decimal, Decimal]:
    quantity = command.reinvestment_quantity
    assert quantity is not None
    unit_price = command.reinvestment_unit_price
    purchase_amount = command.reinvestment_purchase_amount
    if unit_price is None and purchase_amount is None:
        raise ActivityValidationError(
            "reinvestment_unit_price",
            "Enter the actual unit price or total purchase amount.",
        )
    if unit_price is not None and purchase_amount is not None:
        if money(quantity * unit_price, command.currency_code) != purchase_amount:
            raise ActivityValidationError(
                "reinvestment_purchase_amount",
                "Units multiplied by unit price must equal the purchase amount "
                "exactly.",
            )
        return unit_price, purchase_amount
    if unit_price is not None:
        return unit_price, money(quantity * unit_price, command.currency_code)
    assert purchase_amount is not None
    unit_price = rounded(purchase_amount / quantity, 6)
    if unit_price <= 0 or money(quantity * unit_price, command.currency_code) != purchase_amount:
        raise ActivityValidationError('reinvestment_unit_price', 'The purchase amount cannot be represented by a six-decimal unit price. Enter the actual unit price and matching purchase amount.')
    return unit_price, purchase_amount


def _validate_command_shape(command: DividendCommand) -> None:
    if isinstance(command.effective_date, datetime) or not isinstance(
        command.effective_date, date
    ):
        raise ActivityValidationError("effective_date", "Enter a valid payment date.")
    if command.outcome not in DIVIDEND_OUTCOMES:
        raise ActivityValidationError("outcome", "Choose what happened to the dividend.")
    _positive_decimal(command.net_amount, "net_amount", "Net amount")
    if command.gross_amount is not None:
        _positive_decimal(command.gross_amount, "gross_amount", "Gross distribution")
    if command.withholding_amount is not None:
        _nonnegative_decimal(
            command.withholding_amount,
            "withholding_amount",
            "Entered withholding",
        )
    if command.outcome != "cash":
        _positive_decimal(
            command.reinvestment_quantity,
            "reinvestment_quantity",
            "Additional units",
        )
        if command.reinvestment_unit_price is not None:
            _positive_decimal(
                command.reinvestment_unit_price,
                "reinvestment_unit_price",
                "Unit price",
            )
        if command.reinvestment_purchase_amount is not None:
            _positive_decimal(
                command.reinvestment_purchase_amount,
                "reinvestment_purchase_amount",
                "Purchase amount",
            )
        _nonnegative_decimal(
            command.reinvestment_fee_amount,
            "reinvestment_fee_amount",
            "Reinvestment fee",
        )


def _positive_decimal(
    value: Decimal | None,
    field: str,
    label: str,
) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ActivityValidationError(field, f"{label} must be a decimal number.")
    if value <= 0:
        raise ActivityValidationError(field, f"{label} must be greater than zero.")


def _nonnegative_decimal(value: Decimal, field: str, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ActivityValidationError(field, f"{label} must be a decimal number.")
    if value < 0:
        raise ActivityValidationError(field, f"{label} must not be negative.")
