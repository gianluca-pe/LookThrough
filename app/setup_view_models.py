"""Stable template-context builders for the resumable setup screen."""

from __future__ import annotations

from decimal import Decimal

from flask import url_for

from app.models import Account, Institution, Portfolio


SETUP_STEP_LABELS = (
    ("portfolio", "Portfolio"),
    ("accounts", "Accounts"),
    ("positions", "Positions"),
    ("cash", "Cash"),
    ("values", "Values"),
    ("review", "Review"),
)

ACCOUNT_TYPE_LABELS = {
    "cash": "Cash account",
    "brokerage": "Brokerage account",
    "retirement": "Retirement account",
    "deposit": "Deposit account",
    "other": "Other",
}

CASH_TRACKING_MODE_LABELS = {
    "separate_cash": "Cash tracked separately",
    "included_in_aggregate": "Cash included in aggregate statement value",
}


def _percent(value: Decimal) -> Decimal:
    return value * Decimal("100")


def build_setup_view_model(
    *,
    current_step: str,
    portfolio: Portfolio | None,
    institutions: list[Institution],
    accounts: list[Account],
    position_count: int = 0,
    values_started: bool = False,
    cash_complete: bool = False,
) -> dict[str, object]:
    """Build setup state without exposing SQLAlchemy rows to templates."""

    completed = {
        "portfolio": portfolio is not None,
        "accounts": bool(accounts),
        "positions": position_count > 0,
        "cash": cash_complete,
    }
    step_endpoints = {
        "portfolio": ("setup.show", {"step": "portfolio"}),
        "accounts": ("setup.show", {"step": "accounts"}),
        "positions": ("positions.show_positions", {}),
        "values": ("positions.show_setup_values", {}),
    }
    if position_count > 0:
        step_endpoints["review"] = ("overview.review", {})
    if accounts:
        step_endpoints["cash"] = ("accounts.setup_cash", {})
    steps = []
    for code, label in SETUP_STEP_LABELS:
        endpoint = step_endpoints.get(code)
        steps.append(
            {
                "code": code,
                "label": label,
                "status": (
                    "current"
                    if code == current_step
                    else "complete"
                    if completed.get(code, False)
                    else "upcoming"
                ),
                "url": url_for(endpoint[0], **endpoint[1]) if endpoint else None,
            }
        )

    institution_by_id = {institution.id: institution for institution in institutions}

    return {
        "current_step": current_step,
        "steps": steps,
        "portfolio": (
            {
                "id": portfolio.id,
                "name": portfolio.name,
                "reporting_currency_code": portfolio.reporting_currency_code,
                "annual_spending_amount": portfolio.annual_spending_amount,
                "annual_spending_currency_code": (
                    portfolio.annual_spending_currency_code
                    or portfolio.reporting_currency_code
                ),
                "default_as_of_date": portfolio.default_as_of_date,
            }
            if portfolio is not None
            else None
        ),
        "institutions": [
            {
                "id": institution.id,
                "name": institution.name,
                "account_count": sum(
                    account.institution_id == institution.id for account in accounts
                ),
            }
            for institution in institutions
        ],
        "accounts": [
            {
                "id": account.id,
                "name": account.name,
                "reference": account.reference,
                "institution_name": institution_by_id[account.institution_id].name,
                "account_type": account.account_type,
                "account_type_label": ACCOUNT_TYPE_LABELS[account.account_type],
                "default_currency_code": account.default_currency_code,
                "is_multicurrency": account.is_multicurrency,
                "cash_tracking_mode": account.cash_tracking_mode,
                "cash_tracking_mode_label": CASH_TRACKING_MODE_LABELS[
                    account.cash_tracking_mode
                ],
                "portfolio_share_percent": _percent(account.portfolio_share_decimal),
                "present_access_percent": _percent(account.present_access_decimal),
                "earliest_access_date": account.earliest_access_date,
                "access_note": account.access_note,
                "relationship_eligible": account.relationship_eligible,
            }
            for account in accounts
        ],
        "slice_complete": portfolio is not None and bool(accounts),
        "next_step_available": bool(accounts),
        "values_started": values_started,
    }
