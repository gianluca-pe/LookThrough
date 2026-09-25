"""Integration tests for resumable portfolio and account setup."""

from datetime import date
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import func, select

from app import create_app
from app.extensions import db
from app.models import Account, Institution, Portfolio


def _create_portfolio() -> Portfolio:
    portfolio = Portfolio(
        name="FIRE Portfolio",
        reporting_currency_code="EUR",
        annual_spending_amount=Decimal("48000"),
    )
    db.session.add(portfolio)
    db.session.commit()
    return portfolio


def _create_institution(portfolio: Portfolio, name: str = "Example Bank") -> Institution:
    institution = Institution(portfolio_id=portfolio.id, name=name)
    db.session.add(institution)
    db.session.commit()
    return institution




def test_invalid_portfolio_submission_creates_no_record(
    app: Flask, client: FlaskClient
) -> None:
    response = client.post(
        "/setup/portfolio",
        data={
            "name": "   ",
            "reporting_currency_code": "EU",
            "annual_spending_amount": "0",
            "save_and_continue": "Save and continue",
        },
    )

    assert response.status_code == 200
    assert "There is a problem" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.scalar(select(func.count(Portfolio.id))) == 0


def test_portfolio_save_normalizes_currency_and_redirects_to_accounts(
    app: Flask, client: FlaskClient
) -> None:
    response = client.post(
        "/setup/portfolio",
        data={
            "name": "My Portfolio",
            "reporting_currency_code": " eur ",
            "annual_spending_amount": "48000.13",
            "annual_spending_currency_code": " aed ",
            "default_as_of_date": "2026-08-02",
            "save_and_continue": "Save and continue",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/setup?step=accounts")

    with app.app_context():
        portfolio = db.session.scalar(select(Portfolio))
        assert portfolio is not None
        assert portfolio.name == "My Portfolio"
        assert portfolio.reporting_currency_code == "EUR"
        assert portfolio.annual_spending_amount == Decimal("48000.13")
        assert portfolio.annual_spending_currency_code == "AED"
        assert portfolio.default_as_of_date == date(2026, 8, 2)



def test_older_portfolio_form_preserves_spending_currency_when_reporting_changes(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio = _create_portfolio()
        portfolio.annual_spending_currency_code = "AED"
        db.session.commit()

    response = client.post(
        "/setup/portfolio",
        data={
            "name": "FIRE Portfolio",
            "reporting_currency_code": "USD",
            "annual_spending_amount": "48000",
            "save_and_finish_later": "Save and finish later",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        stored = db.session.scalar(select(Portfolio))
        assert stored.reporting_currency_code == "USD"
        assert stored.annual_spending_currency_code == "AED"



def test_add_institution_persists_and_redirects(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _create_portfolio()

    response = client.post(
        "/setup/institutions",
        data={"name": "Example Bank", "add_institution": "Add institution"},
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/setup?step=accounts")
    with app.app_context():
        institution = db.session.scalar(select(Institution))
        assert institution is not None
        assert institution.name == "Example Bank"


def test_add_institution_blocks_case_insensitive_duplicate(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio = _create_portfolio()
        _create_institution(portfolio)

    response = client.post(
        "/setup/institutions",
        data={"name": " example bank ", "add_institution": "Add institution"},
    )

    assert response.status_code == 200
    assert "already in the portfolio" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.scalar(select(func.count(Institution.id))) == 1


def test_invalid_account_percentage_creates_no_record(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio = _create_portfolio()
        institution = _create_institution(portfolio)
        institution_id = institution.id

    response = client.post(
        "/setup/accounts",
        data={
            "institution_id": str(institution_id),
            "name": "Brokerage EUR",
            "account_type": "brokerage",
            "default_currency_code": "EUR",
            "cash_tracking_mode": "separate_cash",
            "portfolio_share_percent": "101",
            "present_access_percent": "100",
            "relationship_eligible": "y",
            "add_account": "Add account",
        },
    )

    assert response.status_code == 200
    assert "Portfolio share must be between 0 and 100" in response.get_data(
        as_text=True
    )
    with app.app_context():
        assert db.session.scalar(select(func.count(Account.id))) == 0


def test_add_account_converts_percentages_once_and_setup_resumes(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio = _create_portfolio()
        institution = _create_institution(portfolio)
        institution_id = institution.id

    response = client.post(
        "/setup/accounts",
        data={
            "institution_id": str(institution_id),
            "name": "Brokerage EUR",
            "reference": "ACC-203",
            "account_type": "brokerage",
            "default_currency_code": " eur ",
            "is_multicurrency": "y",
            "cash_tracking_mode": "separate_cash",
            "portfolio_share_percent": "62.5",
            "present_access_percent": "80",
            "earliest_access_date": "2030-01-01",
            "access_note": "Available after the bridge period.",
            "relationship_eligible": "y",
            "add_account": "Add account",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith(
        "/accounts/1/cash-confirmations/new?currency=EUR&return_to=setup_accounts"
    )

    with app.app_context():
        account = db.session.scalar(select(Account))
        assert account is not None
        assert account.reference == "ACC-203"
        assert account.default_currency_code == "EUR"
        assert account.portfolio_share_decimal == Decimal("0.62500000")
        assert account.present_access_decimal == Decimal("0.80000000")
        assert account.earliest_access_date == date(2030, 1, 1)
        assert account.relationship_eligible is True

    resumed = client.get("/setup")
    body = resumed.get_data(as_text=True)
    assert resumed.status_code == 200
    assert "Add accounts" in body
    assert "Brokerage EUR" in body
    assert "Current accounts are saved" in body


def test_aggregate_account_returns_to_setup_without_cash_continuation(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio = _create_portfolio()
        institution_id = _create_institution(portfolio).id

    response = client.post(
        "/setup/accounts",
        data={
            "institution_id": str(institution_id),
            "name": "Pension wrapper",
            "account_type": "retirement",
            "default_currency_code": "EUR",
            "cash_tracking_mode": "included_in_aggregate",
            "portfolio_share_percent": "100",
            "present_access_percent": "0",
            "add_account": "Add account",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/setup?step=accounts")


def test_setup_writes_require_csrf_token(tmp_path) -> None:
    database_path = tmp_path / "csrf-test.sqlite3"
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "csrf-test-key",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database_path}",
        }
    )
    with application.app_context():
        db.create_all()

    response = application.test_client().post(
        "/setup/portfolio",
        data={
            "name": "My Portfolio",
            "reporting_currency_code": "EUR",
            "annual_spending_amount": "48000",
        },
    )

    assert response.status_code == 400
    with application.app_context():
        assert db.session.scalar(select(func.count(Portfolio.id))) == 0
