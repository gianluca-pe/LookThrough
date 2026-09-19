"""Minimal Overview and final setup review."""

from __future__ import annotations

from datetime import date

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from app.classification_forms import ECONOMIC_ROLE_LABELS
from app.conventions import normalize_currency_code, parse_iso_date
from app.services.portfolio_summary import build_portfolio_summary
from app.services.funding import build_funding_summary
from app.services.relationships import (
    active_relationship_snapshots,
    relationship_attention_item,
)
from app.setup import _current_portfolio, _setup_progress, _setup_records
from app.setup_view_models import build_setup_view_model
from app.template_filters import money_amount


overview_blueprint = Blueprint("overview", __name__)


def _today() -> date:
    return current_app.config["CURRENT_DATE_PROVIDER"]()


def _as_of_date(portfolio) -> date:
    if request.args.get("as_of"):
        return parse_iso_date(request.args["as_of"])
    return portfolio.default_as_of_date or _today()


def _setup_state(portfolio):
    institutions, accounts = _setup_records(portfolio)
    position_count, values_started, cash_complete = _setup_progress(portfolio)
    return institutions, accounts, position_count, values_started, cash_complete


def _reporting_currency(portfolio, choices) -> tuple[str, str | None, str]:
    raw = request.args.get("ccy")
    if not raw:
        return portfolio.reporting_currency_code, None, portfolio.reporting_currency_code
    try:
        selected = normalize_currency_code(raw, field_name="Reporting currency")
        if selected not in choices:
            raise ValueError("Choose a currency used by your accounts or holdings, or your saved reporting currency.")
    except (TypeError, ValueError) as exc:
        return portfolio.reporting_currency_code, str(exc), raw
    return selected, None, selected


def _allocation_chart(summary) -> dict[str, object] | None:
    """Presentation payload for the allocation bar chart.

    Maps shared-summary rows to display percentages; computes nothing. The
    table beside the chart is the equivalent — the chart never carries a
    figure the table does not show.
    """

    if not summary["role_classified_denominator_amount"]:
        return None
    rows = [
        row
        for row in summary["role_allocation"]
        if row["included_reporting_amount"] or row["range_state"] is not None
    ]
    chart: dict[str, object] = {
        "labels": [ECONOMIC_ROLE_LABELS[row["code"]] for row in rows],
        "actual": [
            round(float(row["included_percentage"]) * 100, 1)
            if row["included_percentage"] is not None
            else 0
            for row in rows
        ],
    }
    if summary["allocation_targets_complete"] and summary[
        "allocation_target_calculation_complete"
    ]:
        chart["target_min"] = [
            round(float(row["target_minimum_decimal"]) * 100, 1) for row in rows
        ]
        chart["target_max"] = [
            round(float(row["target_maximum_decimal"]) * 100, 1) for row in rows
        ]
    return chart


def _value_breakdown_chart(
    rows: list[dict[str, object]],
    *,
    label_key: str,
    reporting_currency: str,
) -> dict[str, object] | None:
    """Map exact shared-summary totals to a presentation-only chart payload."""

    known = [row for row in rows if row["included_reporting_amount"] is not None]
    if not known:
        return None
    return {
        "labels": [row[label_key] for row in known],
        "values": [
            round(float(row["included_reporting_amount"]), 2) for row in known
        ],
        "percentages": [
            round(float(row["included_percentage"]) * 100, 1)
            if row["included_percentage"] is not None
            else None
            for row in known
        ],
        "currency": reporting_currency,
    }


@overview_blueprint.get("/overview")
def show() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    _, accounts, position_count, _, _ = _setup_state(portfolio)
    if not accounts:
        return redirect(url_for("setup.show", step="accounts"))
    if position_count == 0:
        return redirect(url_for("positions.show_positions"))

    as_of_date = _as_of_date(portfolio)
    from app.services.fx_reference import reporting_choices
    currency_choices = reporting_choices(portfolio, as_of_date)
    reporting_currency, reporting_currency_error, reporting_currency_input = (
        _reporting_currency(portfolio, currency_choices)
    )
    summary = build_portfolio_summary(
        portfolio,
        as_of_date,
        reporting_currency=reporting_currency,
    )
    funding = build_funding_summary(
        portfolio,
        summary,
        as_of_date,
        reporting_currency=reporting_currency,
    )
    relationship_snapshots = active_relationship_snapshots(portfolio, as_of_date)
    relationship_attention_items = tuple(
        item
        for snapshot in relationship_snapshots
        if (item := relationship_attention_item(snapshot)) is not None
    )
    funding_chart = None
    if funding['assessment']:
        rows = funding['assessment']['bucket_targets']
        percentages = rows[0]['current_percent'] is not None
        funding_chart = {
            'labels': ['Now · 3 years', 'Bridge · 7 years'],
            'unit': 'percent' if percentages else 'amount',
            'actual': [money_amount(row['current_percent'] if percentages else row['current_amount'], 1 if percentages else 2, currency=None if percentages else reporting_currency).replace(',', '') for row in rows],
            'required': [money_amount(row['required_percent'] if percentages else row['required_amount'], 1 if percentages else 2, currency=None if percentages else reporting_currency).replace(',', '') for row in rows],
            'actual_amounts': [money_amount(row['current_amount'], currency=reporting_currency).replace(',', '') for row in rows],
            'required_amounts': [money_amount(row['required_amount'], currency=reporting_currency).replace(',', '') for row in rows],
            # Keep the familiar 0–100% scale, extending it only when a spending
            # requirement exceeds all modelled capital. Never clip a shortfall.
            'maximum': str(max(100, *(row['required_percent'] for row in rows))) if percentages else None,
        }
    return render_template(
        "overview/index.html",
        summary=summary,
        funding=funding,
        funding_chart=funding_chart,
        allocation_chart=_allocation_chart(summary),
        currency_chart=_value_breakdown_chart(
            summary["reporting_totals_by_currency"],
            label_key="native_currency",
            reporting_currency=reporting_currency,
        ),
        institution_chart=_value_breakdown_chart(
            summary["reporting_totals_by_institution"],
            label_key="institution_name",
            reporting_currency=reporting_currency,
        ),
        reporting_currency_error=reporting_currency_error,
        reporting_currency_input=reporting_currency_input,
        reporting_currency_choices=currency_choices,
        relationship_snapshots=relationship_snapshots,
        relationship_attention_items=relationship_attention_items,
        portfolio_name=portfolio.name,
    )


@overview_blueprint.get("/setup/review")
def review() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    institutions, accounts, position_count, values_started, cash_complete = _setup_state(
        portfolio
    )
    if not accounts:
        return redirect(url_for("setup.show", step="accounts"))
    if position_count == 0:
        return redirect(url_for("positions.show_positions"))

    setup = build_setup_view_model(
        current_step="review",
        portfolio=portfolio,
        institutions=institutions,
        accounts=accounts,
        position_count=position_count,
        values_started=values_started,
        cash_complete=cash_complete,
    )
    summary = build_portfolio_summary(portfolio, _as_of_date(portfolio))
    return render_template(
        "setup/review.html",
        setup=setup,
        summary=summary,
        portfolio_name=portfolio.name,
    )
