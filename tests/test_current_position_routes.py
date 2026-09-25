"""Atomic route tests for opening positions and manual values."""

import re
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    Account, FxRate, Institution, Instrument, Portfolio, PositionRegistration,
    Posting, Price, Transaction, ValuationObservation,
)


def _setup_account():
    portfolio = Portfolio(name="Portfolio", reporting_currency_code="EUR", annual_spending_amount=Decimal("48000"))
    db.session.add(portfolio); db.session.flush()
    institution = Institution(portfolio_id=portfolio.id, name="Broker")
    db.session.add(institution); db.session.flush()
    account = Account(portfolio_id=portfolio.id, institution_id=institution.id, name="Brokerage", account_type="brokerage", default_currency_code="EUR", is_multicurrency=False, cash_tracking_mode="separate_cash", portfolio_share_decimal=Decimal("1"), present_access_decimal=Decimal("1"), relationship_eligible=True, is_active=True)
    db.session.add(account); db.session.commit()
    return account


def test_transaction_tracked_opening_is_atomic(app: Flask, client: FlaskClient) -> None:
    with app.app_context(): account_id = _setup_account().id
    response = client.post("/setup/positions", data={
        "account_id": account_id, "instrument_id": "__new__", "new_instrument_name": "Global Fund",
        "instrument_type": "fund", "valuation_currency_code": "eur",
        "tracking_mode": "transaction_tracked", "effective_date": "2026-08-01",
        "opening_quantity": "1000", "save_position": "Add position",
    })
    assert response.status_code == 302
    with app.app_context():
        assert db.session.scalar(select(func.count(Instrument.id))) == 1
        assert db.session.scalar(select(func.count(PositionRegistration.id))) == 1
        assert db.session.scalar(select(func.count(Transaction.id))) == 1
        posting = db.session.scalar(select(Posting))
        assert posting.quantity_delta == Decimal("1000.000000000000")
        assert posting.cash_amount_delta is None


def test_statement_opening_creates_observation_not_activity(app: Flask, client: FlaskClient) -> None:
    with app.app_context(): account_id = _setup_account().id
    response = client.post("/setup/positions", data={
        "account_id": account_id, "instrument_id": "__new__", "new_instrument_name": "Opaque Wrapper",
        "instrument_type": "other", "valuation_currency_code": "usd",
        "tracking_mode": "statement_valued", "effective_date": "2026-07-31",
        "statement_value": "25000", "save_position": "Add position",
    })
    assert response.status_code == 302
    with app.app_context():
        observation = db.session.scalar(select(ValuationObservation))
        assert observation.native_value_amount == Decimal("25000.000000000000")
        assert db.session.scalar(select(func.count(Transaction.id))) == 0
        assert db.session.scalar(select(func.count(Posting.id))) == 0


def test_invalid_opening_creates_no_partial_instrument(app: Flask, client: FlaskClient) -> None:
    with app.app_context(): account_id = _setup_account().id
    response = client.post("/setup/positions", data={
        "account_id": account_id, "instrument_id": "__new__", "new_instrument_name": "Broken",
        "instrument_type": "fund", "valuation_currency_code": "EUR",
        "tracking_mode": "transaction_tracked", "effective_date": "2026-08-01",
        "opening_quantity": "0",
    })
    assert response.status_code == 200
    with app.app_context():
        assert db.session.scalar(select(func.count(Instrument.id))) == 0


def test_manual_price_and_fx_feed_holdings_through_one_valuation_path(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        account_id = _setup_account().id
    client.post("/setup/positions", data={
        "account_id": account_id, "instrument_id": "__new__",
        "new_instrument_name": "Global Fund", "instrument_type": "fund",
        "valuation_currency_code": "USD", "tracking_mode": "transaction_tracked",
        "effective_date": "2026-07-01", "opening_quantity": "100",
    })
    with app.app_context(): instrument_id = db.session.scalar(select(Instrument.id))

    price_response = client.post("/setup/values/price", data={
        "instrument_id": instrument_id, "effective_date": "2026-08-01",
        "price_amount": "25.50", "currency_code": "USD",
    })
    fx_response = client.post("/setup/values/fx", data={
        "base_currency_code": "USD", "quote_currency_code": "EUR",
        "effective_date": "2026-08-01", "quote_per_base_amount": "0.90",
    })

    assert price_response.status_code == 302
    assert fx_response.status_code == 302
    with app.app_context():
        assert db.session.scalar(select(Price)).price_amount == Decimal("25.500000000000")
        assert db.session.scalar(select(FxRate)).quote_per_base_amount == Decimal("0.900000000000")

    holdings = client.get("/holdings?as_of=2026-08-02").get_data(as_text=True)
    assert "Global Fund" in holdings
    # Decimal exactness is asserted above at the persistence layer; the
    # Holdings UI applies the contracted two-decimal display rounding.
    assert "2,295.00" in holdings


def test_accumulating_fund_nav_update_creates_no_activity_or_cash_effect(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        account_id = _setup_account().id
    client.post(
        "/setup/positions",
        data={
            "account_id": account_id,
            "instrument_id": "__new__",
            "new_instrument_name": "Accumulating Fund",
            "instrument_type": "fund",
            "valuation_currency_code": "EUR",
            "tracking_mode": "transaction_tracked",
            "effective_date": "2026-07-01",
            "opening_quantity": "100",
        },
    )
    with app.app_context():
        instrument_id = db.session.scalar(select(Instrument.id))
        transaction_count = db.session.scalar(select(func.count(Transaction.id)))
        posting_count = db.session.scalar(select(func.count(Posting.id)))

    response = client.post(
        "/values/price",
        data={
            "instrument_id": instrument_id,
            "effective_date": "2026-08-28",
            "price_amount": "12.50",
            "currency_code": "EUR",
        },
    )

    assert response.status_code == 302
    with app.app_context():
        assert db.session.scalar(select(func.count(Price.id))) == 1
        assert db.session.scalar(select(func.count(Transaction.id))) == transaction_count
        assert db.session.scalar(select(func.count(Posting.id))) == posting_count


def test_individual_price_requires_explicit_same_day_correction_and_keeps_audit(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        account_id = _setup_account().id
    client.post(
        "/setup/positions",
        data={
            "account_id": account_id,
            "instrument_id": "__new__",
            "new_instrument_name": "Global Fund",
            "instrument_type": "fund",
            "valuation_currency_code": "EUR",
            "tracking_mode": "transaction_tracked",
            "effective_date": "2026-07-01",
            "opening_quantity": "100",
        },
    )
    with app.app_context():
        instrument_id = db.session.scalar(select(Instrument.id))

    initial = {
        "instrument_id": instrument_id,
        "effective_date": "2026-09-02",
        "price_amount": "25.50",
        "currency_code": "EUR",
    }
    assert client.post("/values/price", data=initial).status_code == 302

    attempted = client.post(
        "/values/price", data={**initial, "price_amount": "24.75"}
    )
    body = attempted.get_data(as_text=True)
    assert attempted.status_code == 200
    assert "Confirm replacement of the existing price for this date" in body
    assert "Replace the existing price for this instrument and date" in body
    checkbox = re.search(
        r'<input[^>]*id="price-confirm_same_day_correction"[^>]*>', body
    ).group(0)
    assert 'aria-invalid="true"' in checkbox
    assert "autofocus" in checkbox
    with app.app_context():
        saved = db.session.scalar(select(Price))
        assert saved.price_amount == Decimal("25.500000000000")
        assert saved.source_note is None

    corrected = client.post(
        "/values/price",
        data={
            **initial,
            "price_amount": "24.75",
            "confirm_same_day_correction": "y",
        },
    )
    assert corrected.status_code == 302
    with app.app_context():
        assert db.session.scalar(select(func.count(Price.id))) == 1
        saved = db.session.scalar(select(Price))
        assert saved.price_amount == Decimal("24.750000000000")
        assert saved.source_note == (
            "Corrected 2026-09-02: previous unit price 25.5 EUR."
        )

    setup_correction = client.post(
        "/setup/values/price",
        data={
            **initial,
            "price_amount": "24.50",
            "confirm_same_day_correction": "y",
        },
    )
    assert setup_correction.status_code == 302
    with app.app_context():
        saved = db.session.scalar(select(Price))
        assert saved.price_amount == Decimal("24.500000000000")
        assert saved.source_note.splitlines() == [
            "Corrected 2026-09-02: previous unit price 25.5 EUR.",
            "Corrected 2026-09-02: previous unit price 24.75 EUR.",
        ]


def test_later_statement_value_adds_observation_without_activity(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context(): account_id = _setup_account().id
    client.post("/setup/positions", data={
        "account_id": account_id, "instrument_id": "__new__",
        "new_instrument_name": "Opaque Wrapper", "instrument_type": "other",
        "valuation_currency_code": "EUR", "tracking_mode": "statement_valued",
        "effective_date": "2026-07-01", "statement_value": "10000",
    })
    with app.app_context(): registration_id = db.session.scalar(select(PositionRegistration.id))

    response = client.post("/setup/values/statement", data={
        "registration_id": registration_id, "effective_date": "2026-08-01",
        "native_value_amount": "10500", "currency_code": "EUR",
    })

    assert response.status_code == 302
    with app.app_context():
        assert db.session.scalar(select(func.count(ValuationObservation.id))) == 2
        assert db.session.scalar(select(func.count(Transaction.id))) == 0
