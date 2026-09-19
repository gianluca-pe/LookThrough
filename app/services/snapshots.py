"""Frozen shared-summary snapshots and conservative change comparison."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.extensions import db
from app.models import (
    Account,
    CashBalanceCheckpoint,
    Portfolio,
    PortfolioSnapshot,
    Posting,
    Transaction,
)
from app.services.fx import resolve_fx
from app.services.portfolio_summary import build_portfolio_summary


SNAPSHOT_PAYLOAD_VERSION = 1
EXTERNAL_FLOW_TYPES = frozenset({"deposit", "withdrawal"})


class SnapshotValidationError(ValueError):
    """The requested immutable snapshot cannot be recorded."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"Snapshot payload cannot encode {type(value).__name__}.")


def _movement_status(item_count: int, known_count: int, stale_count: int) -> str:
    if item_count == 0:
        return "current"
    if known_count == 0:
        return "missing"
    if known_count < item_count:
        return "partial"
    if stale_count:
        return "stale"
    return "current"


def _movement_payload(
    portfolio: Portfolio,
    as_of_date: date,
    reporting_currency: str,
) -> dict[str, object]:
    """Freeze cumulative supported movements so backdated corrections remain visible."""

    external_sources: list[dict[str, object]] = []
    transaction_rows = db.session.execute(
        select(Transaction, Posting, Account)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .join(Account, Account.id == Posting.account_id)
        .where(
            Transaction.portfolio_id == portfolio.id,
            Transaction.status == "posted",
            Transaction.reverses_transaction_id.is_(None),
            Transaction.transaction_type.in_(EXTERNAL_FLOW_TYPES),
            Transaction.effective_date <= as_of_date,
            Posting.posting_kind == "cash",
            Posting.cash_amount_delta.is_not(None),
            Account.cash_tracking_mode == "separate_cash",
            Account.cash_settlement_account_id.is_(None),
        )
        .order_by(Transaction.effective_date, Transaction.id, Posting.id)
    )
    for transaction, posting, account in transaction_rows:
        external_sources.append(
            {
                "source_id": posting.id,
                "source_kind": transaction.transaction_type,
                "effective_date": transaction.effective_date,
                "native_amount": posting.cash_amount_delta,
                "native_currency": posting.currency_code,
                "account_id": account.id,
                "account_name": account.name,
            }
        )

    correction_sources: list[dict[str, object]] = []
    checkpoint_rows = db.session.execute(
        select(CashBalanceCheckpoint, Account)
        .join(Account, Account.id == CashBalanceCheckpoint.account_id)
        .where(
            Account.portfolio_id == portfolio.id,
            Account.cash_tracking_mode == "separate_cash",
            CashBalanceCheckpoint.superseded_at.is_(None),
            CashBalanceCheckpoint.effective_date <= as_of_date,
            CashBalanceCheckpoint.correction_amount.is_not(None),
        )
        .order_by(
            CashBalanceCheckpoint.effective_date,
            CashBalanceCheckpoint.id,
        )
    )
    for checkpoint, account in checkpoint_rows:
        correction_sources.append(
            {
                "source_id": checkpoint.id,
                "source_kind": "cash_confirmation_correction",
                "effective_date": checkpoint.effective_date,
                "native_amount": checkpoint.correction_amount,
                "native_currency": checkpoint.currency_code,
                "account_id": account.id,
                "account_name": account.name,
            }
        )

    return {
        "external_cash_flow": _convert_movements(
            portfolio, external_sources, reporting_currency
        ),
        "cash_reconciliation": _convert_movements(
            portfolio, correction_sources, reporting_currency
        ),
    }


def _convert_movements(
    portfolio: Portfolio,
    sources: list[dict[str, object]],
    reporting_currency: str,
) -> dict[str, object]:
    amount = Decimal("0")
    known_count = 0
    stale_count = 0
    rows: list[dict[str, object]] = []
    for source in sources:
        fx = resolve_fx(
            source["native_currency"],
            reporting_currency,
            source["effective_date"],
            stale_days=portfolio.fx_stale_days,
        )
        reporting_amount = (
            source["native_amount"] * fx.rate if fx.rate is not None else None
        )
        if reporting_amount is not None:
            amount += reporting_amount
            known_count += 1
        if fx.status == "stale":
            stale_count += 1
        rows.append(
            {
                **source,
                "reporting_amount": reporting_amount,
                "reporting_currency": reporting_currency,
                "fx_rate": fx.rate,
                "fx_date": fx.effective_date,
                "fx_path": fx.path,
                "status": fx.status,
                "missing_reason": fx.missing_reason,
            }
        )
    item_count = len(rows)
    return {
        "reporting_amount": amount,
        "reporting_currency": reporting_currency,
        "status": _movement_status(item_count, known_count, stale_count),
        "item_count": item_count,
        "known_count": known_count,
        "missing_count": item_count - known_count,
        "stale_count": stale_count,
        "sources": rows,
    }


def build_snapshot_payload(
    portfolio: Portfolio, as_of_date: date
) -> dict[str, object]:
    """Build the exact, self-contained payload before it is made immutable."""

    if isinstance(as_of_date, datetime) or not isinstance(as_of_date, date):
        raise SnapshotValidationError("as_of_date", "Enter a valid snapshot date.")
    summary = build_portfolio_summary(portfolio, as_of_date)
    headline_fields = (
        "position_count",
        "reporting_valued_count",
        "missing_count",
        "stale_count",
        "investment_reporting_amount",
        "investment_status",
        "cash_balance_count",
        "cash_reporting_valued_count",
        "cash_missing_count",
        "cash_stale_count",
        "cash_reporting_amount",
        "cash_status",
        "total_item_count",
        "total_reporting_valued_count",
        "total_missing_count",
        "total_stale_count",
        "gross_reporting_amount",
        "gross_status",
        "included_reporting_amount",
        "included_status",
        "accessible_reporting_amount",
        "accessible_status",
        "term_liquidity_reporting_amount",
        "excluded_by_share_reporting_amount",
        "restricted_reporting_amount",
        "role_classified_denominator_amount",
        "role_unclassified_investment_amount",
        "bucket_classified_denominator_amount",
        "bucket_unclassified_investment_amount",
        "bucket_unassigned_cash_amount",
    )
    holding_fields = (
        "registration_id",
        "instrument_id",
        "instrument_name",
        "account_id",
        "account_name",
        "institution_name",
        "tracking_mode",
        "quantity",
        "native_amount",
        "native_currency",
        "reporting_amount",
        "included_reporting_amount",
        "accessible_reporting_amount",
        "reporting_currency",
        "value_date",
        "fx_date",
        "source_mode",
        "status",
        "missing_reason",
        "missing_source",
        "stale_sources",
        "fx_path",
        "role_weights",
        "fire_bucket_code",
    )
    cash_fields = (
        "account_id",
        "account_name",
        "institution_name",
        "native_amount",
        "native_currency",
        "reporting_amount",
        "included_reporting_amount",
        "accessible_reporting_amount",
        "reporting_currency",
        "status",
        "source_mode",
        "checkpoint_date",
        "latest_cash_effect_date",
        "fx_date",
        "fx_path",
        "missing_reason",
    )
    payload = {
        "schema_version": SNAPSHOT_PAYLOAD_VERSION,
        "portfolio": {
            "portfolio_id": portfolio.id,
            "portfolio_name": portfolio.name,
            "as_of_date": as_of_date,
            "reporting_currency": summary["reporting_currency"],
            **{field: summary[field] for field in headline_fields},
        },
        "role_allocation": [
            {
                key: row[key]
                for key in (
                    "code",
                    "included_reporting_amount",
                    "accessible_reporting_amount",
                    "included_percentage",
                    "accessible_percentage",
                )
            }
            for row in summary["role_allocation"]
        ],
        "bucket_allocation": [
            {
                key: row[key]
                for key in (
                    "code",
                    "included_reporting_amount",
                    "accessible_reporting_amount",
                    "included_percentage",
                    "accessible_percentage",
                )
            }
            for row in summary["bucket_allocation"]
        ],
        "movement_totals": _movement_payload(
            portfolio, as_of_date, summary["reporting_currency"]
        ),
        "source_evidence": {
            "holdings": [
                {field: row.get(field) for field in holding_fields}
                for row in summary["holdings"]
            ],
            "cash_balances": [
                {field: row.get(field) for field in cash_fields}
                for row in summary["cash_balances"]
            ],
        },
    }
    return _encode(payload)


def save_snapshot(
    portfolio: Portfolio,
    as_of_date: date,
    *,
    note: str | None = None,
    commit: bool = True,
) -> PortfolioSnapshot:
    """Persist one immutable snapshot after all derived values are built."""

    existing = db.session.scalar(
        select(PortfolioSnapshot.id).where(
            PortfolioSnapshot.portfolio_id == portfolio.id,
            PortfolioSnapshot.as_of_date == as_of_date,
        )
    )
    if existing is not None:
        raise SnapshotValidationError(
            "as_of_date", "A snapshot already exists for this date."
        )
    clean_note = note.strip() if isinstance(note, str) else None
    if clean_note and len(clean_note) > 1000:
        raise SnapshotValidationError(
            "note", "Snapshot note must be 1,000 characters or fewer."
        )
    payload = build_snapshot_payload(portfolio, as_of_date)
    row = PortfolioSnapshot(
        portfolio_id=portfolio.id,
        as_of_date=as_of_date,
        reporting_currency_code=payload["portfolio"]["reporting_currency"],
        summary_status=payload["portfolio"]["gross_status"],
        payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        note=clean_note or None,
    )
    db.session.add(row)
    db.session.flush()
    if commit:
        db.session.commit()
    return row


def snapshot_payload(snapshot: PortfolioSnapshot) -> dict[str, object]:
    payload = json.loads(snapshot.payload_json)
    if payload.get("schema_version") != SNAPSHOT_PAYLOAD_VERSION:
        raise ValueError("Snapshot payload version is not supported.")
    return payload


def snapshots_for_portfolio(portfolio_id: int) -> tuple[PortfolioSnapshot, ...]:
    return tuple(
        db.session.scalars(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.portfolio_id == portfolio_id)
            .order_by(
                PortfolioSnapshot.as_of_date.desc(),
                PortfolioSnapshot.id.desc(),
            )
        )
    )


def previous_snapshot(snapshot: PortfolioSnapshot) -> PortfolioSnapshot | None:
    return db.session.scalar(
        select(PortfolioSnapshot)
        .where(
            PortfolioSnapshot.portfolio_id == snapshot.portfolio_id,
            PortfolioSnapshot.as_of_date < snapshot.as_of_date,
        )
        .order_by(
            PortfolioSnapshot.as_of_date.desc(), PortfolioSnapshot.id.desc()
        )
        .limit(1)
    )


def _decimal(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def compare_snapshots(
    earlier: PortfolioSnapshot, later: PortfolioSnapshot
) -> dict[str, object]:
    """Compare frozen records without claiming the remainder is performance."""

    if earlier.portfolio_id != later.portfolio_id:
        raise ValueError("Snapshots must belong to the same portfolio.")
    if earlier.as_of_date >= later.as_of_date:
        raise ValueError("The later snapshot must have a later as-of date.")
    prior = snapshot_payload(earlier)
    current = snapshot_payload(later)
    prior_portfolio = prior["portfolio"]
    current_portfolio = current["portfolio"]
    currency = current_portfolio["reporting_currency"]
    compatible = currency == prior_portfolio["reporting_currency"]

    def difference(field: str) -> Decimal | None:
        old = _decimal(prior_portfolio[field])
        new = _decimal(current_portfolio[field])
        return new - old if compatible and old is not None and new is not None else None

    result: dict[str, object] = {
        "earlier_snapshot_id": earlier.id,
        "later_snapshot_id": later.id,
        "earlier_as_of_date": earlier.as_of_date,
        "later_as_of_date": later.as_of_date,
        "reporting_currency": currency if compatible else None,
        "status": "incompatible_currency" if not compatible else "current",
        "gross_value_change": difference("gross_reporting_amount"),
        "included_value_change": difference("included_reporting_amount"),
        "accessible_value_change": difference("accessible_reporting_amount"),
        "external_cash_flow": None,
        "cash_reconciliation": None,
        "unattributed_value_change": None,
    }
    if not compatible:
        return result

    movement_statuses: list[str] = []
    movement_deltas: dict[str, Decimal] = {}
    for key in ("external_cash_flow", "cash_reconciliation"):
        old = prior["movement_totals"][key]
        new = current["movement_totals"][key]
        movement_statuses.extend((old["status"], new["status"]))
        if old["status"] in {"current", "stale"} and new["status"] in {
            "current",
            "stale",
        }:
            movement_deltas[key] = Decimal(new["reporting_amount"]) - Decimal(
                old["reporting_amount"]
            )

    summaries_complete = prior_portfolio["gross_status"] in {
        "current",
        "stale",
    } and current_portfolio["gross_status"] in {"current", "stale"}
    movements_complete = len(movement_deltas) == 2
    if not summaries_complete or not movements_complete:
        result["status"] = "partial"
        return result

    result["external_cash_flow"] = movement_deltas["external_cash_flow"]
    result["cash_reconciliation"] = movement_deltas["cash_reconciliation"]
    result["unattributed_value_change"] = (
        result["gross_value_change"]
        - result["external_cash_flow"]
        - result["cash_reconciliation"]
    )
    if "stale" in movement_statuses or "stale" in {
        prior_portfolio["gross_status"],
        current_portfolio["gross_status"],
    }:
        result["status"] = "stale"
    return result
