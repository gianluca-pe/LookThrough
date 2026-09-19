"""Convert owner-entered annual spending through the shared FX path."""

from __future__ import annotations

from datetime import date

from app.conventions import normalize_currency_code
from app.models import Portfolio
from app.services.fx import resolve_fx
from app.services.retirement_plans import adopted_plan, tier_amounts


def build_spending_value(
    portfolio: Portfolio,
    as_of_date: date,
    reporting_currency: str,
    *, core_only: bool = False,
) -> dict[str, object]:
    plan = adopted_plan(portfolio.id)
    native_currency = plan.currency_code if plan else portfolio.annual_spending_currency_code
    amounts = tier_amounts(plan, as_of_date) if plan else None
    native_amount = (amounts['core'] if core_only else sum(amounts.values())) if amounts is not None else portfolio.annual_spending_amount
    if plan and amounts is None:
        return {"native_amount": None, "native_currency": native_currency,
                "reporting_amount": None, "reporting_currency": reporting_currency,
                "fx_rate": None, "fx_date": None, "fx_source_mode": "missing", "fx_path": None,
                "status": "missing", "missing_reason": "The selected date precedes the retirement plan base date"}
    if native_currency is None:
        return {
            "native_amount": native_amount,
            "native_currency": None,
            "reporting_amount": None,
            "reporting_currency": reporting_currency,
            "fx_rate": None,
            "fx_date": None,
            "fx_source_mode": "missing",
            "fx_path": None,
            "status": "missing",
            "missing_reason": "Annual spending currency is not set",
        }
    try:
        normalized_currency = normalize_currency_code(
            native_currency, field_name="Annual spending currency"
        )
    except (TypeError, ValueError) as exc:
        return {
            "native_amount": native_amount,
            "native_currency": native_currency,
            "reporting_amount": None,
            "reporting_currency": reporting_currency,
            "fx_rate": None,
            "fx_date": None,
            "fx_source_mode": "missing",
            "fx_path": None,
            "status": "missing",
            "missing_reason": str(exc),
        }

    fx = resolve_fx(
        normalized_currency,
        reporting_currency,
        as_of_date,
        stale_days=portfolio.fx_stale_days,
    )
    return {
        "native_amount": native_amount,
        "native_currency": normalized_currency,
        "reporting_amount": (
            native_amount * fx.rate
            if fx.rate is not None
            else None
        ),
        "reporting_currency": reporting_currency,
        "fx_rate": fx.rate,
        "fx_date": fx.effective_date,
        "fx_source_mode": fx.source_mode,
        "fx_path": fx.path,
        "status": fx.status,
        "missing_reason": fx.missing_reason,
    }
