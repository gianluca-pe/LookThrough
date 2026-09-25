"""Browser routes for Overview and the setup summary."""

from datetime import date
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient

from app.extensions import db
from app.models import (
    Account,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    ValuationObservation,
)


def _portfolio() -> Portfolio:
    portfolio = Portfolio(
        name="Personal portfolio",
        reporting_currency_code="EUR",
        annual_spending_amount=Decimal("48000"),
        default_as_of_date=date(2026, 8, 2),
    )
    db.session.add(portfolio)
    db.session.commit()
    return portfolio


def _account(portfolio: Portfolio) -> Account:
    institution = Institution(portfolio_id=portfolio.id, name="Broker")
    db.session.add(institution)
    db.session.flush()
    account = Account(
        portfolio_id=portfolio.id,
        institution_id=institution.id,
        name="Brokerage",
        account_type="brokerage",
        default_currency_code="EUR",
        is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        relationship_eligible=True,
        is_active=True,
    )
    db.session.add(account)
    db.session.commit()
    return account


def _statement_position(portfolio: Portfolio, account: Account, *, with_value: bool):
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name="Opaque wrapper",
        instrument_type="other",
        valuation_currency_code="EUR",
        is_active=True,
    )
    db.session.add(instrument)
    db.session.flush()
    registration = PositionRegistration(
        account_id=account.id,
        instrument_id=instrument.id,
        tracking_mode="statement_valued",
        opening_date=date(2026, 8, 1),
    )
    db.session.add(registration)
    db.session.flush()
    if with_value:
        db.session.add(
            ValuationObservation(
                position_registration_id=registration.id,
                effective_date=date(2026, 8, 1),
                native_value_amount=Decimal("12345.67"),
                currency_code="EUR",
            )
        )
    db.session.commit()
    return registration




def test_overview_review_and_holdings_show_the_same_reporting_total(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, with_value=True)

    for path in ("/overview", "/setup/review", "/holdings"):
        response = client.get(path)
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "12,345.67" in body


def test_review_remains_available_with_missing_values_and_never_shows_zero(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, with_value=False)

    body = client.get("/setup/review").get_data(as_text=True)
    rail = body.split('aria-label="Setup progress"', 1)[1].split("</nav>", 1)[0]
    assert "<h1>Review current values</h1>" in body
    assert 'aria-current="step"' in rail
    assert 'href="/setup/review"' in rail
    assert ">Cash<" in rail
    assert "No eligible statement value" in body
    assert "No position has a reporting value" not in body
    assert "EUR 0.00" not in body
    assert 'href="/overview"' in body
