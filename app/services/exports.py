"""Exact CSV exports built from the same views used by the browser."""

from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from io import StringIO
from typing import Iterable, Mapping

from app.models import Portfolio
from app.services.activity_history import ActivityHistoryFilters, list_activities
from app.services.portfolio_summary import build_portfolio_summary


HOLDINGS_COLUMNS = (
    "row_type",
    "as_of_date",
    "institution",
    "account",
    "instrument",
    "registration_id",
    "tracking_mode",
    "quantity",
    "native_amount",
    "native_currency",
    "reporting_amount",
    "reporting_currency",
    "value_date",
    "fx_date",
    "fx_path",
    "source_mode",
    "status",
    "missing_reason",
    "portfolio_share_decimal",
    "present_access_decimal",
    "included_reporting_amount",
    "accessible_reporting_amount",
    "term_liquidity_reporting_amount",
    "primary_role",
    "fire_bucket",
    "maturity_status",
    "maturity_date",
)

ACTIVITY_COLUMNS = (
    "transaction_id",
    "effective_date",
    "recorded_at_utc",
    "activity_type",
    "activity_label",
    "status",
    "institution",
    "account",
    "instrument",
    "currency",
    "quantity_effect",
    "cash_effect",
    "fee_amount",
    "withholding_amount",
    "note",
    "activity_group_id",
    "linked_transaction_ids",
    "reverses_transaction_id",
    "reversed_by_transaction_ids",
)


def holdings_csv(
    portfolio: Portfolio,
    as_of_date: date,
    *,
    reporting_currency: str | None = None,
) -> str:
    """Export current investment and cash rows from the shared summary path."""

    summary = build_portfolio_summary(
        portfolio,
        as_of_date,
        reporting_currency=reporting_currency,
    )
    rows: list[dict[str, object]] = []
    for holding in summary["holdings"]:
        fixed_deposit = holding.get("fixed_deposit") or {}
        rows.append(
            {
                "row_type": "investment",
                "as_of_date": as_of_date,
                "institution": holding["institution_name"],
                "account": holding["account_name"],
                "instrument": holding["instrument_name"],
                "registration_id": holding["registration_id"],
                "tracking_mode": holding["tracking_mode"],
                "quantity": holding["quantity"],
                "native_amount": holding["native_amount"],
                "native_currency": holding["native_currency"],
                "reporting_amount": holding["reporting_amount"],
                "reporting_currency": holding["reporting_currency"],
                "value_date": holding["value_date"],
                "fx_date": holding["fx_date"],
                "fx_path": holding["fx_path"],
                "source_mode": holding["source_mode"],
                "status": holding["status"],
                "missing_reason": holding["missing_reason"],
                "portfolio_share_decimal": holding["portfolio_share_decimal"],
                "present_access_decimal": holding["present_access_decimal"],
                "included_reporting_amount": holding[
                    "included_reporting_amount"
                ],
                "accessible_reporting_amount": holding[
                    "accessible_reporting_amount"
                ],
                "term_liquidity_reporting_amount": holding[
                    "term_liquidity_reporting_amount"
                ],
                "primary_role": holding["primary_role_code"],
                "fire_bucket": holding["fire_bucket_code"],
                "maturity_status": fixed_deposit.get("maturity_status"),
                "maturity_date": fixed_deposit.get("maturity_date"),
            }
        )
    for cash in summary["cash_balances"]:
        rows.append(
            {
                "row_type": "cash",
                "as_of_date": as_of_date,
                "institution": cash["institution_name"],
                "account": cash["account_name"],
                "instrument": f'{cash["native_currency"]} cash',
                "registration_id": None,
                "tracking_mode": cash["cash_status"],
                "quantity": None,
                "native_amount": cash["native_amount"],
                "native_currency": cash["native_currency"],
                "reporting_amount": cash["reporting_amount"],
                "reporting_currency": cash["reporting_currency"],
                "value_date": cash["checkpoint_date"]
                or cash["latest_cash_effect_date"],
                "fx_date": cash["fx_date"],
                "fx_path": cash["fx_path"],
                "source_mode": cash["source_mode"],
                "status": cash["status"],
                "missing_reason": cash["missing_reason"],
                "portfolio_share_decimal": cash["portfolio_share_decimal"],
                "present_access_decimal": cash["present_access_decimal"],
                "included_reporting_amount": cash["included_reporting_amount"],
                "accessible_reporting_amount": cash[
                    "accessible_reporting_amount"
                ],
                "term_liquidity_reporting_amount": cash[
                    "term_liquidity_reporting_amount"
                ],
                "primary_role": None,
                "fire_bucket": None,
                "maturity_status": None,
                "maturity_date": None,
            }
        )
    return _write_csv(HOLDINGS_COLUMNS, rows)


def activity_csv(
    portfolio_id: int,
    filters: ActivityHistoryFilters | None = None,
) -> str:
    """Export the same plain-language activity rows as Activity history."""

    rows = []
    for activity in list_activities(portfolio_id, filters):
        source = asdict(activity)
        rows.append(
            {
                "transaction_id": source["transaction_id"],
                "effective_date": source["effective_date"],
                "recorded_at_utc": source["recorded_at"],
                "activity_type": source["activity_type"],
                "activity_label": source["activity_label"],
                "status": source["status"],
                "institution": source["institution_name"],
                "account": source["account_name"],
                "instrument": source["instrument_name"],
                "currency": source["currency_code"],
                "quantity_effect": source["quantity_effect"],
                "cash_effect": source["cash_effect"],
                "fee_amount": source["fee_amount"],
                "withholding_amount": source["withholding_amount"],
                "note": source["note"],
                "activity_group_id": source["activity_group_id"],
                "linked_transaction_ids": source["linked_transaction_ids"],
                "reverses_transaction_id": source["reverses_transaction_id"],
                "reversed_by_transaction_ids": source[
                    "reversed_by_transaction_ids"
                ],
            }
        )
    return _write_csv(ACTIVITY_COLUMNS, rows)


def _write_csv(
    columns: Iterable[str], rows: Iterable[Mapping[str, object]]
) -> str:
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_value(row.get(key)) for key in columns})
    return stream.getvalue()


def _csv_value(value: object) -> str | int:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return " > ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return value
    return str(value)
