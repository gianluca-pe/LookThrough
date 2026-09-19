"""Configuration and custody source-record tests."""

from datetime import UTC, date
from decimal import Decimal

from flask import Flask
from sqlalchemy import text

from app.extensions import db
from app.models import Account, Institution, Portfolio


def test_setup_records_round_trip_decimals_dates_and_utc_timestamps(app: Flask) -> None:
    with app.app_context():
        portfolio = Portfolio(
            name="FIRE Portfolio",
            reporting_currency_code="EUR",
            annual_spending_amount=Decimal("48000.13"),
            annual_inflation_decimal=Decimal("0.03125"),
            default_as_of_date=date(2026, 8, 2),
        )
        db.session.add(portfolio)
        db.session.flush()

        institution = Institution(portfolio_id=portfolio.id, name="Example Bank")
        db.session.add(institution)
        db.session.flush()

        account = Account(
            portfolio_id=portfolio.id,
            institution_id=institution.id,
            name="Brokerage EUR",
            account_type="brokerage",
            default_currency_code="EUR",
            is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("0.62500000"),
            present_access_decimal=Decimal("0.80000000"),
            earliest_access_date=date(2030, 1, 1),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add(account)
        db.session.commit()

        db.session.expire_all()
        stored = db.session.get(Account, account.id)
        stored_portfolio = db.session.get(Portfolio, portfolio.id)

        assert stored is not None
        assert stored_portfolio is not None
        assert stored.portfolio_share_decimal == Decimal("0.62500000")
        assert stored.present_access_decimal == Decimal("0.80000000")
        assert stored.earliest_access_date == date(2030, 1, 1)
        assert stored_portfolio.annual_spending_amount == Decimal("48000.13")
        assert stored_portfolio.annual_spending_currency_code == "EUR"
        assert stored_portfolio.annual_inflation_decimal == Decimal("0.03125000")
        assert stored.created_at.tzinfo is UTC
        assert stored.updated_at.tzinfo is UTC


def test_sqlite_foreign_key_enforcement_is_enabled(app: Flask) -> None:
    with app.app_context():
        enabled = db.session.execute(text("PRAGMA foreign_keys")).scalar_one()

    assert enabled == 1
