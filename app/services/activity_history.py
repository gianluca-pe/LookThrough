"""Plain-language activity history and auditable reversal workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db
from app.models import Account, Instrument, Portfolio, Posting, Transaction
from app.services.activity import ActivityValidationError
from app.services.fixed_deposits import reverse_maturity_disposition_side_effects
from app.services.ledger_writes import ledger_write


ACTIVITY_TYPE_LABELS = {
    "opening_balance": "Opening position",
    "buy": "Buy",
    "sell": "Sell",
    "dividend": "Dividend",
    "fixed_deposit_maturity": "Fixed-deposit maturity",
    "reversal": "Reversal",
}
ACTIVITY_STATUS_LABELS = {"posted": "Posted", "reversed": "Reversed"}


@dataclass(frozen=True)
class ActivityHistoryFilters:
    date_from: date | None = None
    date_to: date | None = None
    account_id: int | None = None
    instrument_id: int | None = None
    activity_type: str | None = None
    currency_code: str | None = None
    status: str | None = None


@dataclass(frozen=True)
class ActivityView:
    transaction_id: int
    effective_date: date
    recorded_at: datetime
    activity_type: str
    activity_label: str
    status: str
    status_label: str
    account_id: int | None
    account_name: str | None
    institution_name: str | None
    instrument_id: int | None
    instrument_name: str | None
    currency_code: str | None
    quantity_effect: Decimal | None
    cash_effect: Decimal | None
    fee_amount: Decimal | None
    withholding_amount: Decimal | None
    note: str | None
    activity_group_id: str | None
    linked_transaction_ids: tuple[int, ...]
    reverses_transaction_id: int | None
    reversed_by_transaction_ids: tuple[int, ...]
    can_reverse: bool


@dataclass(frozen=True)
class ReversalPreview:
    requested_transaction_id: int
    effective_date: date
    activities: tuple[ActivityView, ...]
    original_transaction_ids: tuple[int, ...]
    is_group: bool


@dataclass(frozen=True)
class ReversalCommand:
    portfolio_id: int
    transaction_id: int
    reason: str


@dataclass(frozen=True)
class ReversalResult:
    requested_transaction_id: int
    original_transaction_ids: tuple[int, ...]
    reversal_transaction_ids: tuple[int, ...]
    activity_group_id: str | None
    effective_date: date
    reason: str


def list_activities(
    portfolio_id: int,
    filters: ActivityHistoryFilters | None = None,
) -> tuple[ActivityView, ...]:
    """List persisted activities newest first without exposing posting rows."""

    _require_portfolio(portfolio_id)
    filters = filters or ActivityHistoryFilters()
    _validate_filters(filters)
    statement = (
        select(Transaction)
        .where(Transaction.portfolio_id == portfolio_id)
        .options(selectinload(Transaction.postings))
    )
    if filters.date_from is not None:
        statement = statement.where(Transaction.effective_date >= filters.date_from)
    if filters.date_to is not None:
        statement = statement.where(Transaction.effective_date <= filters.date_to)
    if filters.account_id is not None:
        statement = statement.where(
            Transaction.postings.any(Posting.account_id == filters.account_id)
        )
    if filters.instrument_id is not None:
        statement = statement.where(
            Transaction.postings.any(
                Posting.instrument_id == filters.instrument_id
            )
        )
    if filters.activity_type is not None:
        statement = statement.where(
            Transaction.transaction_type == filters.activity_type
        )
    if filters.currency_code is not None:
        statement = statement.where(
            Transaction.postings.any(
                Posting.currency_code == filters.currency_code
            )
        )
    if filters.status is not None:
        statement = statement.where(Transaction.status == filters.status)
    transactions = list(
        db.session.scalars(
            statement.order_by(
                Transaction.effective_date.desc(), Transaction.id.desc()
            )
        )
    )
    return _build_views(portfolio_id, transactions)


def get_activity(
    transaction_id: int, *, portfolio_id: int
) -> ActivityView | None:
    """Return one portfolio-scoped activity detail view."""

    transaction = db.session.scalar(
        select(Transaction)
        .where(
            Transaction.id == transaction_id,
            Transaction.portfolio_id == portfolio_id,
        )
        .options(selectinload(Transaction.postings))
    )
    if transaction is None:
        return None
    return _build_views(portfolio_id, [transaction])[0]


def preview_reversal(
    transaction_id: int, *, portfolio_id: int
) -> ReversalPreview:
    """Resolve the complete activity or linked group affected by a reversal."""

    targets = _reversal_targets(transaction_id, portfolio_id=portfolio_id)
    views = _build_views(portfolio_id, targets)
    effective_dates = {row.effective_date for row in targets}
    if len(effective_dates) != 1:
        raise ActivityValidationError(
            "transaction_id",
            "Linked activities use different dates and cannot be reversed safely.",
        )
    return ReversalPreview(
        requested_transaction_id=transaction_id,
        effective_date=targets[0].effective_date,
        activities=views,
        original_transaction_ids=tuple(row.id for row in targets),
        is_group=len(targets) > 1,
    )


def reverse_activity(
    command: ReversalCommand,
    *,
    group_id_provider=lambda: uuid4().hex,
) -> ReversalResult:
    """Atomically reverse one activity or its complete linked dividend group."""

    reason = command.reason.strip() if isinstance(command.reason, str) else ""
    if not reason:
        raise ActivityValidationError("reason", "Enter a reason for the reversal.")
    if len(reason) > 1000:
        raise ActivityValidationError(
            "reason", "Reason must be 1,000 characters or fewer."
        )

    with ledger_write(ActivityValidationError, "reason"):
        targets = _reversal_targets(
            command.transaction_id,
            portfolio_id=command.portfolio_id,
        )
        _validate_quantities_after_reversal(command.portfolio_id, targets)
        reversal_group_id = group_id_provider() if len(targets) > 1 else None
        reversal_ids: list[int] = []
        for original in targets:
            reversal = Transaction(
                portfolio_id=command.portfolio_id,
                transaction_type="reversal",
                effective_date=original.effective_date,
                status="posted",
                reverses_transaction_id=original.id,
                activity_group_id=reversal_group_id,
                note=reason,
            )
            db.session.add(reversal)
            db.session.flush()
            db.session.add_all(
                [
                    Posting(
                        transaction_id=reversal.id,
                        account_id=posting.account_id,
                        posting_kind=posting.posting_kind,
                        instrument_id=posting.instrument_id,
                        currency_code=posting.currency_code,
                        quantity_delta=(
                            -posting.quantity_delta
                            if posting.quantity_delta is not None
                            else None
                        ),
                        cash_amount_delta=(
                            -posting.cash_amount_delta
                            if posting.cash_amount_delta is not None
                            else None
                        ),
                        unit_price=posting.unit_price,
                        price_currency_code=posting.price_currency_code,
                    )
                    for posting in original.postings
                ]
            )
            reverse_maturity_disposition_side_effects(original)
            original.status = "reversed"
            reversal_ids.append(reversal.id)
        result = ReversalResult(
            requested_transaction_id=command.transaction_id,
            original_transaction_ids=tuple(row.id for row in targets),
            reversal_transaction_ids=tuple(reversal_ids),
            activity_group_id=reversal_group_id,
            effective_date=targets[0].effective_date,
            reason=reason,
        )
        db.session.commit()
        return result


def _require_portfolio(portfolio_id: int) -> None:
    if db.session.get(Portfolio, portfolio_id) is None:
        raise ActivityValidationError("portfolio_id", "Portfolio was not found.")


def _validate_filters(filters: ActivityHistoryFilters) -> None:
    if filters.date_from is not None and filters.date_to is not None:
        if filters.date_from > filters.date_to:
            raise ActivityValidationError(
                "date_to", "End date must be on or after the start date."
            )
    if (
        filters.activity_type is not None
        and filters.activity_type not in ACTIVITY_TYPE_LABELS
    ):
        raise ActivityValidationError(
            "activity_type", "Choose a supported activity type."
        )
    if filters.status is not None and filters.status not in ACTIVITY_STATUS_LABELS:
        raise ActivityValidationError("status", "Choose a supported status.")
    if filters.currency_code is not None:
        currency = filters.currency_code
        if len(currency) != 3 or not currency.isalpha() or not currency.isupper():
            raise ActivityValidationError(
                "currency_code", "Currency must be a three-letter code."
            )


def _reversal_targets(
    transaction_id: int, *, portfolio_id: int
) -> list[Transaction]:
    requested = db.session.scalar(
        select(Transaction)
        .where(
            Transaction.id == transaction_id,
            Transaction.portfolio_id == portfolio_id,
        )
        .options(selectinload(Transaction.postings))
    )
    if requested is None:
        raise ActivityValidationError("transaction_id", "Activity was not found.")
    if requested.reverses_transaction_id is not None:
        raise ActivityValidationError(
            "transaction_id", "A reversal cannot itself be reversed."
        )
    if requested.status != "posted":
        raise ActivityValidationError(
            "transaction_id", "This activity has already been reversed."
        )

    targets = [requested]
    if requested.activity_group_id is not None:
        targets = list(
            db.session.scalars(
                select(Transaction)
                .where(
                    Transaction.portfolio_id == portfolio_id,
                    Transaction.activity_group_id == requested.activity_group_id,
                )
                .options(selectinload(Transaction.postings))
                .order_by(Transaction.id)
            )
        )
        if any(
            row.status != "posted" or row.reverses_transaction_id is not None
            for row in targets
        ):
            raise ActivityValidationError(
                "transaction_id",
                "This linked activity is already reversed or incomplete.",
            )
    if not targets or any(not row.postings for row in targets):
        raise ActivityValidationError(
            "transaction_id", "This activity has no reversible effects."
        )
    return targets


def _validate_quantities_after_reversal(
    portfolio_id: int, targets: list[Transaction]
) -> None:
    target_ids = {row.id for row in targets}
    affected = {
        (posting.account_id, posting.instrument_id)
        for transaction in targets
        for posting in transaction.postings
        if posting.posting_kind == "instrument"
        and posting.instrument_id is not None
        and posting.quantity_delta is not None
    }
    for account_id, instrument_id in affected:
        active_rows = db.session.execute(
            select(Posting, Transaction)
            .join(Transaction, Transaction.id == Posting.transaction_id)
            .where(
                Transaction.portfolio_id == portfolio_id,
                Transaction.status == "posted",
                Transaction.reverses_transaction_id.is_(None),
                Posting.posting_kind == "instrument",
                Posting.account_id == account_id,
                Posting.instrument_id == instrument_id,
                Posting.transaction_id.not_in(target_ids),
            )
            .order_by(Transaction.effective_date, Transaction.id, Posting.id)
        )
        quantity = Decimal("0")
        for posting, _ in active_rows:
            quantity += posting.quantity_delta or Decimal("0")
            if quantity < 0:
                raise ActivityValidationError(
                    "transaction_id",
                    "Reverse later dependent sales first; this reversal would "
                    "otherwise create a negative position.",
                )


def _build_views(
    portfolio_id: int, transactions: list[Transaction]
) -> tuple[ActivityView, ...]:
    if not transactions:
        return ()
    links = list(
        db.session.execute(
            select(
                Transaction.id,
                Transaction.transaction_type,
                Transaction.status,
                Transaction.activity_group_id,
                Transaction.reverses_transaction_id,
            ).where(Transaction.portfolio_id == portfolio_id)
        )
    )
    transaction_types = {row.id: row.transaction_type for row in links}
    transaction_statuses = {row.id: row.status for row in links}
    group_members: dict[str, list[int]] = {}
    reversed_by: dict[int, list[int]] = {}
    for row in links:
        if row.activity_group_id is not None:
            group_members.setdefault(row.activity_group_id, []).append(row.id)
        if row.reverses_transaction_id is not None:
            reversed_by.setdefault(row.reverses_transaction_id, []).append(row.id)

    postings = [posting for row in transactions for posting in row.postings]
    account_ids = {posting.account_id for posting in postings}
    instrument_ids = {
        posting.instrument_id
        for posting in postings
        if posting.instrument_id is not None
    }
    accounts = {
        row.id: row
        for row in db.session.scalars(
            select(Account)
            .where(Account.id.in_(account_ids))
            .options(joinedload(Account.institution))
        )
    }
    instruments = {
        row.id: row
        for row in db.session.scalars(
            select(Instrument).where(Instrument.id.in_(instrument_ids))
        )
    }

    views = []
    for transaction in transactions:
        maturity_original = next(
            (
                posting
                for posting in transaction.postings
                if transaction.transaction_type == "fixed_deposit_maturity"
                and posting.posting_kind == "clearing"
                and posting.instrument_id is not None
                and posting.cash_amount_delta is not None
                and posting.cash_amount_delta < 0
            ),
            None,
        )
        primary_posting = next(
            (
                posting
                for posting in transaction.postings
                if posting.posting_kind in {"instrument", "income"}
            ),
            None,
        )
        account_id = (
            maturity_original.account_id
            if maturity_original is not None
            else primary_posting.account_id
            if primary_posting is not None
            else _one_or_none({posting.account_id for posting in transaction.postings})
        )
        instrument_id = (
            maturity_original.instrument_id
            if maturity_original is not None
            else _one_or_none(
                {
                    posting.instrument_id
                    for posting in transaction.postings
                    if posting.instrument_id is not None
                }
            )
        )
        currency_code = _one_or_none(
            {posting.currency_code for posting in transaction.postings}
        )
        account = accounts.get(account_id) if account_id is not None else None
        instrument = (
            instruments.get(instrument_id) if instrument_id is not None else None
        )
        quantity_values = [
            posting.quantity_delta
            for posting in transaction.postings
            if posting.posting_kind == "instrument"
            and posting.quantity_delta is not None
        ]
        cash_values = [
            posting.cash_amount_delta
            for posting in transaction.postings
            if posting.posting_kind == "cash"
            and posting.cash_amount_delta is not None
        ]
        expense_values = [
            posting.cash_amount_delta
            for posting in transaction.postings
            if posting.posting_kind == "expense"
            and posting.cash_amount_delta is not None
        ]
        linked_ids = tuple(
            sorted(
                row_id
                for row_id in group_members.get(
                    transaction.activity_group_id or "", []
                )
                if row_id != transaction.id
            )
        )
        origin_type = transaction_types.get(transaction.reverses_transaction_id)
        label = ACTIVITY_TYPE_LABELS.get(
            transaction.transaction_type,
            transaction.transaction_type.replace("_", " ").title(),
        )
        if origin_type is not None:
            origin_label = ACTIVITY_TYPE_LABELS.get(
                origin_type, origin_type.replace("_", " ").title()
            )
            label = f"Reversal of {origin_label}"
        group_is_reversible = all(
            transaction_statuses[member_id] == "posted"
            and not reversed_by.get(member_id)
            for member_id in group_members.get(
                transaction.activity_group_id or "", [transaction.id]
            )
        )
        can_reverse = (
            transaction.status == "posted"
            and transaction.reverses_transaction_id is None
            and bool(transaction.postings)
            and not reversed_by.get(transaction.id)
            and group_is_reversible
        )
        views.append(
            ActivityView(
                transaction_id=transaction.id,
                effective_date=transaction.effective_date,
                recorded_at=transaction.created_at,
                activity_type=transaction.transaction_type,
                activity_label=label,
                status=transaction.status,
                status_label=ACTIVITY_STATUS_LABELS[transaction.status],
                account_id=account_id,
                account_name=account.name if account is not None else None,
                institution_name=(
                    account.institution.name if account is not None else None
                ),
                instrument_id=instrument_id,
                instrument_name=(
                    instrument.name if instrument is not None else None
                ),
                currency_code=currency_code,
                quantity_effect=(
                    sum(quantity_values, Decimal("0"))
                    if quantity_values
                    else None
                ),
                cash_effect=(
                    sum(cash_values, Decimal("0")) if cash_values else None
                ),
                fee_amount=(
                    sum(expense_values, Decimal("0"))
                    if expense_values
                    and transaction.transaction_type in {"buy", "sell"}
                    else None
                ),
                withholding_amount=(
                    sum(expense_values, Decimal("0"))
                    if expense_values and transaction.transaction_type == "dividend"
                    else None
                ),
                note=transaction.note,
                activity_group_id=transaction.activity_group_id,
                linked_transaction_ids=linked_ids,
                reverses_transaction_id=transaction.reverses_transaction_id,
                reversed_by_transaction_ids=tuple(
                    sorted(reversed_by.get(transaction.id, []))
                ),
                can_reverse=can_reverse,
            )
        )
    return tuple(views)


def _one_or_none(values: set[int] | set[str]) -> int | str | None:
    return next(iter(values)) if len(values) == 1 else None
