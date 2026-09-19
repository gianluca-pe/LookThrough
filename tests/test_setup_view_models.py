"""Presentation tests for setup view models."""

from decimal import Decimal

from flask import Flask

from app.models import Account, Institution, Portfolio
from app.setup_view_models import build_setup_view_model


def test_setup_view_model_exposes_values_not_database_rows(app: Flask) -> None:
    portfolio = Portfolio(
        id=1,
        name="FIRE Portfolio",
        reporting_currency_code="EUR",
        annual_spending_amount=Decimal("48000"),
    )
    institution = Institution(id=2, portfolio_id=1, name="Example Bank")
    account = Account(
        id=3,
        portfolio_id=1,
        institution_id=2,
        name="Brokerage EUR",
        account_type="brokerage",
        default_currency_code="EUR",
        is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal("0.625"),
        present_access_decimal=Decimal("0.8"),
        relationship_eligible=True,
        is_active=True,
    )

    with app.test_request_context():
        view_model = build_setup_view_model(
            current_step="accounts",
            portfolio=portfolio,
            institutions=[institution],
            accounts=[account],
        )

    assert isinstance(view_model["portfolio"], dict)
    assert view_model["institutions"] == [
        {"id": 2, "name": "Example Bank", "account_count": 1}
    ]
    assert view_model["accounts"][0]["portfolio_share_percent"] == Decimal("62.500")
    assert view_model["accounts"][0]["present_access_percent"] == Decimal("80.0")
    assert view_model["accounts"][0]["account_type_label"] == "Brokerage account"
    assert (
        view_model["accounts"][0]["cash_tracking_mode_label"]
        == "Cash tracked separately"
    )
    assert view_model["slice_complete"] is True
    assert view_model["next_step_available"] is True

    steps = view_model["steps"]
    assert [step["code"] for step in steps] == [
        "portfolio",
        "accounts",
        "positions",
        "cash",
        "values",
        "review",
    ]
    assert steps[0]["status"] == "complete"
    assert steps[1]["status"] == "current"
    assert steps[2]["url"].endswith("/setup/positions")
    assert steps[3]["url"].endswith("/setup/cash")
    assert steps[4]["url"].endswith("/setup/values")
    assert steps[5]["url"] is None
