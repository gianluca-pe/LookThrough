"""Explicit Buy/Sell commands and their internal posting templates."""

from __future__ import annotations

from app.decimal_policy import currency_places, money, require_precision

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import or_, select

from app.conventions import normalize_currency_code
from app.extensions import db
from app.models import (
    Account,
    INSTRUMENT_TYPE_CODES,
    Instrument,
    Portfolio,
    PositionRegistration,
    Posting,
    Transaction,
)
from app.services.positions import quantity_as_of
from app.services.cash import backdated_cash_warning
from app.services.settlement import SettlementAccountError, cash_settlement_account
from app.services.ledger_writes import ledger_write


TRADE_TYPES = ("buy", "sell")
TRADE_INELIGIBLE_INSTRUMENT_TYPES = frozenset({"cash", "fixed_deposit"})


class ActivityValidationError(ValueError):
    """A business-rule failure mapped to one activity form field."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class TradeCommand:
    portfolio_id: int
    activity_type: str
    effective_date: date
    account_id: int
    instrument_id: int
    quantity: Decimal | None
    unit_price: Decimal
    fee_amount: Decimal = Decimal("0")
    quantity_mode: str = "entered"
    replacement_sale_id: int | None = None


@dataclass(frozen=True)
class InlineInstrumentCommand:
    portfolio_id: int
    name: str
    ticker_or_isin: str | None
    valuation_currency_code: str
    instrument_type: str
    account_id: int | None = None


@dataclass(frozen=True)
class CreatedInstrument:
    instrument_id: int
    name: str
    ticker_or_isin: str | None
    valuation_currency_code: str
    instrument_type: str


@dataclass(frozen=True)
class TradePreview:
    activity_type: str
    effective_date: date
    account_id: int
    account_name: str
    cash_account_id: int
    cash_account_name: str
    instrument_id: int
    instrument_name: str
    settlement_currency: str
    quantity_mode: str
    quantity: Decimal
    unit_price: Decimal
    gross_amount: Decimal
    fee_amount: Decimal
    cash_effect: Decimal
    quantity_before: Decimal
    quantity_after: Decimal
    registration_id: int | None
    creates_registration: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class PostedTrade:
    transaction_id: int
    registration_id: int
    preview: TradePreview


@dataclass(frozen=True)
class TradeReceipt:
    transaction_id: int
    activity_type: str
    effective_date: date
    account_id: int
    account_name: str
    cash_account_id: int
    cash_account_name: str
    instrument_id: int
    instrument_name: str
    settlement_currency: str
    quantity: Decimal
    unit_price: Decimal
    gross_amount: Decimal
    fee_amount: Decimal
    cash_effect: Decimal


@dataclass(frozen=True)
class ReplacementContext:
    sale_transaction_id: int
    account_id: int
    account_name: str
    effective_date: date
    settlement_currency: str
    available_proceeds: Decimal


def create_trade_instrument(
    command: InlineInstrumentCommand,
) -> CreatedInstrument:
    """Save the compact instrument identity used by the Buy workflow."""

    portfolio = db.session.get(Portfolio, command.portfolio_id)
    if portfolio is None:
        raise ActivityValidationError("portfolio_id", "Portfolio was not found.")

    name = command.name.strip() if isinstance(command.name, str) else ""
    if not name:
        raise ActivityValidationError("new_instrument_name", "Name is required.")
    if len(name) > 200:
        raise ActivityValidationError(
            "new_instrument_name", "Name must be 200 characters or fewer."
        )

    ticker = (
        command.ticker_or_isin.strip()
        if isinstance(command.ticker_or_isin, str)
        else None
    )
    if ticker == "":
        ticker = None
    if ticker is not None and len(ticker) > 64:
        raise ActivityValidationError(
            "new_ticker_or_isin",
            "Ticker or ISIN must be 64 characters or fewer.",
        )
    if command.instrument_type not in INSTRUMENT_TYPE_CODES:
        raise ActivityValidationError(
            "new_instrument_type", "Choose a supported broad type."
        )
    if command.instrument_type in TRADE_INELIGIBLE_INSTRUMENT_TYPES:
        raise ActivityValidationError(
            "new_instrument_type",
            "Buy/Sell records quantities. Choose a quantity-tracked investment "
            "type; cash and fixed deposits use their dedicated workflows.",
        )
    try:
        currency = normalize_currency_code(
            command.valuation_currency_code,
            field_name="Valuation currency",
        )
    except (TypeError, ValueError) as exc:
        raise ActivityValidationError(
            "new_valuation_currency_code", str(exc)
        ) from exc

    if command.account_id is not None:
        account = db.session.get(Account, command.account_id)
        if (
            account is None
            or account.portfolio_id != portfolio.id
            or not account.is_active
        ):
            raise ActivityValidationError(
                "account_id", "Choose an active account in this portfolio."
            )
        if (
            not account.is_multicurrency
            and account.default_currency_code != currency
        ):
            raise ActivityValidationError(
                "new_valuation_currency_code",
                "Valuation currency must match the selected single-currency "
                "account. Cross-currency settlement is not available in this "
                "Buy step.",
            )

    try:
        instrument = Instrument(
            portfolio_id=portfolio.id,
            name=name,
            ticker_or_isin=ticker,
            instrument_type=command.instrument_type,
            valuation_currency_code=currency,
            is_active=True,
        )
        db.session.add(instrument)
        db.session.commit()
        return CreatedInstrument(
            instrument_id=instrument.id,
            name=instrument.name,
            ticker_or_isin=instrument.ticker_or_isin,
            valuation_currency_code=instrument.valuation_currency_code,
            instrument_type=instrument.instrument_type,
        )
    except Exception:
        db.session.rollback()
        raise


def preview_trade(command: TradeCommand) -> TradePreview:
    """Validate a same-currency Buy/Sell and return exact effects without writes."""

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
            "Cash and fixed deposits cannot receive Buy/Sell quantity postings. "
            "Use their dedicated workflows instead.",
        )

    settlement_currency = instrument.valuation_currency_code
    if (
        not account.is_multicurrency
        and account.default_currency_code != settlement_currency
    ):
        raise ActivityValidationError(
            "instrument_id",
            "This instrument uses a different currency from the account. "
            "Cross-currency settlement is not available in this Buy/Sell step.",
        )
    try:
        cash_account = cash_settlement_account(account, settlement_currency)
    except SettlementAccountError as exc:
        raise ActivityValidationError("account_id", str(exc)) from exc
    if command.replacement_sale_id is not None:
        replacement = get_replacement_context(
            command.replacement_sale_id,
            portfolio_id=portfolio.id,
        )
        if replacement is None:
            raise ActivityValidationError(
                "replacement_sale_id",
                "The source sale is not available for a replacement purchase.",
            )
        if command.activity_type != "buy":
            raise ActivityValidationError(
                "activity_type", "Sale proceeds can continue only to a Buy."
            )
        if account.id != replacement.account_id:
            raise ActivityValidationError(
                "account_id",
                "Use the same account as the source sale so the proceeds remain "
                "traceable.",
            )
        if settlement_currency != replacement.settlement_currency:
            raise ActivityValidationError(
                "instrument_id",
                "Choose an instrument in the sale's settlement currency "
                f"({replacement.settlement_currency}). Cross-currency replacement "
                "is not available in this step.",
            )
        if command.effective_date < replacement.effective_date:
            raise ActivityValidationError(
                "effective_date",
                "Replacement purchase date must be on or after the source sale.",
            )

    registration = db.session.scalar(
        select(PositionRegistration).where(
            PositionRegistration.account_id == account.id,
            PositionRegistration.instrument_id == instrument.id,
        )
    )
    has_open_statement_registration = db.session.scalar(
        select(PositionRegistration.id)
        .where(
            PositionRegistration.instrument_id == instrument.id,
            PositionRegistration.tracking_mode == "statement_valued",
            or_(
                PositionRegistration.closing_date.is_(None),
                PositionRegistration.closing_date >= command.effective_date,
            ),
        )
        .limit(1)
    )
    if has_open_statement_registration is not None:
        raise ActivityValidationError(
            "instrument_id",
            "This instrument is tracked by statement value in an open position. "
            "Update its dated statement value instead of recording a Buy or Sell.",
        )
    if registration is not None and registration.tracking_mode != "transaction_tracked":
        raise ActivityValidationError(
            "instrument_id",
            "This position is tracked by statement value. Update its dated "
            "statement value instead of recording a Buy or Sell.",
        )
    if command.activity_type == "sell" and registration is None:
        raise ActivityValidationError(
            "instrument_id", "This account has no quantity-tracked position to sell."
        )

    quantity_before = (
        quantity_as_of(registration, command.effective_date)
        if registration is not None
        else Decimal("0")
    )
    assert quantity_before is not None
    if command.quantity_mode == "entire_holding":
        if quantity_before <= 0:
            raise ActivityValidationError(
                "quantity_mode",
                f"No units are available to sell on "
                f"{command.effective_date.isoformat()}.",
            )
        resolved_quantity = quantity_before
    else:
        assert command.quantity is not None
        resolved_quantity = command.quantity
    quantity_delta = (
        resolved_quantity
        if command.activity_type == "buy"
        else -resolved_quantity
    )
    quantity_after = quantity_before + quantity_delta
    if quantity_after < 0:
        raise ActivityValidationError(
            "quantity",
            f"Only {quantity_before} units are available on "
            f"{command.effective_date.isoformat()}.",
        )

    if command.activity_type == "sell":
        # This new transaction follows existing same-day entries. Validate every
        # subsequent effect, not only the final balance: a later buy must not hide
        # an intervening oversell caused by this backdated sale.
        remaining = quantity_after
        later_effects = db.session.execute(
            select(Posting.quantity_delta, Transaction.effective_date)
            .join(Transaction, Transaction.id == Posting.transaction_id)
            .where(
                Posting.account_id == account.id,
                Posting.instrument_id == instrument.id,
                Posting.posting_kind == "instrument",
                Posting.quantity_delta.is_not(None),
                Transaction.status == "posted",
                Transaction.reverses_transaction_id.is_(None),
                Transaction.effective_date > command.effective_date,
            )
            .order_by(Transaction.effective_date, Transaction.id, Posting.id)
        )
        for delta, effective_date in later_effects:
            remaining += delta
            if remaining < 0:
                raise ActivityValidationError(
                    "quantity_mode" if command.quantity_mode == "entire_holding" else "quantity",
                    f"This sale would leave a negative holding on {effective_date.isoformat()} "
                    "because of later recorded sales. Reduce the quantity or correct "
                    "the later activity first.",
                )

    require_precision(resolved_quantity, 6, ActivityValidationError, 'quantity')
    require_precision(command.unit_price, 6, ActivityValidationError, 'unit_price')
    require_precision(command.fee_amount, currency_places(settlement_currency), ActivityValidationError, 'fee_amount')
    require_precision(command.fee_amount, 3, ActivityValidationError, 'fee_amount')
    gross_amount = money(resolved_quantity * command.unit_price, settlement_currency)
    require_precision(gross_amount, 3, ActivityValidationError, 'unit_price')
    cash_effect = (
        -(gross_amount + command.fee_amount)
        if command.activity_type == "buy"
        else gross_amount - command.fee_amount
    )
    require_precision(cash_effect, 3, ActivityValidationError, 'fee_amount')
    warnings = []
    if cash_account.cash_tracking_mode == "included_in_aggregate":
        warnings.append(
            "Cash is included in this account's aggregate value. The trade's "
            "cash effect will not be valued as a separate cash balance."
        )
    backdated_warning = backdated_cash_warning(
        portfolio.id,
        cash_account.id,
        settlement_currency,
        command.effective_date,
    )
    if backdated_warning is not None:
        warnings.append(backdated_warning)

    return TradePreview(
        activity_type=command.activity_type,
        effective_date=command.effective_date,
        account_id=account.id,
        account_name=account.name,
        cash_account_id=cash_account.id,
        cash_account_name=cash_account.name,
        instrument_id=instrument.id,
        instrument_name=instrument.name,
        settlement_currency=settlement_currency,
        quantity_mode=command.quantity_mode,
        quantity=resolved_quantity,
        unit_price=command.unit_price,
        gross_amount=gross_amount,
        fee_amount=command.fee_amount,
        cash_effect=cash_effect,
        quantity_before=quantity_before,
        quantity_after=quantity_after,
        registration_id=registration.id if registration is not None else None,
        creates_registration=registration is None,
        warnings=tuple(warnings),
    )


def post_trade(command: TradeCommand) -> PostedTrade:
    """Validate and atomically persist a Buy/Sell plus explicit typed postings."""

    with ledger_write(ActivityValidationError, "account_id"):
        preview = preview_trade(command)
        transaction, registration = _add_trade_preview(
            preview,
            portfolio_id=command.portfolio_id,
        )
        result = PostedTrade(transaction.id, registration.id, preview)
        db.session.commit()
        return result


def _add_trade_preview(
    preview: TradePreview,
    *,
    portfolio_id: int,
    activity_group_id: str | None = None,
) -> tuple[Transaction, PositionRegistration]:
    """Add validated trade rows without committing; the caller owns atomicity."""

    registration = (
        db.session.get(PositionRegistration, preview.registration_id)
        if preview.registration_id is not None
        else None
    )
    if registration is None:
        registration = PositionRegistration(
            account_id=preview.account_id,
            instrument_id=preview.instrument_id,
            tracking_mode="transaction_tracked",
            opening_date=preview.effective_date,
        )
        db.session.add(registration)
        db.session.flush()
    elif (
        registration.opening_date is None
        or preview.effective_date < registration.opening_date
    ):
        registration.opening_date = preview.effective_date

    transaction = Transaction(
        portfolio_id=portfolio_id,
        transaction_type=preview.activity_type,
        effective_date=preview.effective_date,
        status="posted",
        activity_group_id=activity_group_id,
    )
    db.session.add(transaction)
    db.session.flush()

    quantity_delta = (
        preview.quantity if preview.activity_type == "buy" else -preview.quantity
    )
    clearing_amount = (
        preview.gross_amount
        if preview.activity_type == "buy"
        else -preview.gross_amount
    )
    postings = [
        Posting(
            transaction_id=transaction.id,
            account_id=preview.account_id,
            posting_kind="instrument",
            instrument_id=preview.instrument_id,
            currency_code=preview.settlement_currency,
            quantity_delta=quantity_delta,
            unit_price=preview.unit_price,
            price_currency_code=preview.settlement_currency,
        ),
        Posting(
            transaction_id=transaction.id,
            account_id=preview.cash_account_id,
            posting_kind="cash",
            currency_code=preview.settlement_currency,
            cash_amount_delta=preview.cash_effect,
        ),
        Posting(
            transaction_id=transaction.id,
            account_id=preview.account_id,
            posting_kind="clearing",
            instrument_id=preview.instrument_id,
            currency_code=preview.settlement_currency,
            cash_amount_delta=clearing_amount,
        ),
    ]
    if preview.fee_amount:
        postings.append(
            Posting(
                transaction_id=transaction.id,
                account_id=preview.account_id,
                posting_kind="expense",
                instrument_id=preview.instrument_id,
                currency_code=preview.settlement_currency,
                cash_amount_delta=preview.fee_amount,
            )
        )
    db.session.add_all(postings)
    return transaction, registration


def get_trade_receipt(
    transaction_id: int, *, portfolio_id: int
) -> TradeReceipt | None:
    """Rebuild the success-panel contract from persisted source postings."""

    transaction = db.session.scalar(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.portfolio_id == portfolio_id,
            Transaction.transaction_type.in_(TRADE_TYPES),
            Transaction.status == "posted",
        )
    )
    if transaction is None:
        return None

    instrument_posting = next(
        (row for row in transaction.postings if row.posting_kind == "instrument"),
        None,
    )
    cash_posting = next(
        (row for row in transaction.postings if row.posting_kind == "cash"), None
    )
    expense_posting = next(
        (row for row in transaction.postings if row.posting_kind == "expense"),
        None,
    )
    clearing_posting = next(
        (row for row in transaction.postings if row.posting_kind == "clearing"),
        None,
    )
    if instrument_posting is None or cash_posting is None:
        return None

    account = db.session.get(Account, instrument_posting.account_id)
    cash_account = db.session.get(Account, cash_posting.account_id)
    instrument = db.session.get(Instrument, instrument_posting.instrument_id)
    if account is None or cash_account is None or instrument is None:
        return None

    quantity = abs(instrument_posting.quantity_delta or Decimal("0"))
    unit_price = instrument_posting.unit_price or Decimal("0")
    return TradeReceipt(
        transaction_id=transaction.id,
        activity_type=transaction.transaction_type,
        effective_date=transaction.effective_date,
        account_id=account.id,
        account_name=account.name,
        cash_account_id=cash_account.id,
        cash_account_name=cash_account.name,
        instrument_id=instrument.id,
        instrument_name=instrument.name,
        settlement_currency=cash_posting.currency_code,
        quantity=quantity,
        unit_price=unit_price,
        gross_amount=(
            abs(clearing_posting.cash_amount_delta)
            if clearing_posting is not None
            and clearing_posting.cash_amount_delta is not None
            else quantity * unit_price
        ),
        fee_amount=(
            expense_posting.cash_amount_delta
            if expense_posting is not None
            else Decimal("0")
        ),
        cash_effect=cash_posting.cash_amount_delta or Decimal("0"),
    )


def get_replacement_context(
    sale_transaction_id: int, *, portfolio_id: int
) -> ReplacementContext | None:
    """Resolve safe carry-forward context from one posted sale."""

    receipt = get_trade_receipt(sale_transaction_id, portfolio_id=portfolio_id)
    if (
        receipt is None
        or receipt.activity_type != "sell"
        or receipt.cash_effect <= 0
    ):
        return None
    return ReplacementContext(
        sale_transaction_id=receipt.transaction_id,
        account_id=receipt.account_id,
        account_name=receipt.account_name,
        effective_date=receipt.effective_date,
        settlement_currency=receipt.settlement_currency,
        available_proceeds=receipt.cash_effect,
    )


def _validate_command_shape(command: TradeCommand) -> None:
    if command.activity_type not in TRADE_TYPES:
        raise ActivityValidationError(
            "activity_type", "Choose Buy or Sell for this activity."
        )
    if isinstance(command.effective_date, datetime) or not isinstance(
        command.effective_date, date
    ):
        raise ActivityValidationError("effective_date", "Enter a valid activity date.")
    if command.quantity_mode not in {"entered", "entire_holding"}:
        raise ActivityValidationError(
            "quantity_mode", "Choose how to set the sale quantity."
        )
    if command.quantity_mode == "entire_holding":
        if command.activity_type != "sell":
            raise ActivityValidationError(
                "quantity_mode",
                "The entire-holding choice is available only for a Sell.",
            )
    else:
        _require_positive_decimal(command.quantity, "quantity", "Quantity")
    _require_positive_decimal(command.unit_price, "unit_price", "Price per unit")
    if not isinstance(command.fee_amount, Decimal) or not command.fee_amount.is_finite():
        raise ActivityValidationError("fee_amount", "Fee must be a decimal number.")
    if command.fee_amount < 0:
        raise ActivityValidationError("fee_amount", "Fee must not be negative.")


def _require_positive_decimal(value: Decimal, field: str, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ActivityValidationError(field, f"{label} must be a decimal number.")
    if value <= 0:
        raise ActivityValidationError(field, f"{label} must be greater than zero.")
