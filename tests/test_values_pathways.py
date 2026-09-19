"""Guided setup versus ordinary values maintenance."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import re

from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    Account,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    Posting,
    Price,
    Transaction,
)


def assert_no_inline_script(body: str) -> None:
    scripts = [re.sub(r"\?v=\d+", "", tag) for tag in re.findall(r"<script[^>]*>", body)]
    assert scripts == ['<script src="/static/js/enhance.js" defer>']


def _current_position(app: Flask) -> int:
    with app.app_context():
        portfolio = Portfolio(
            name="Portfolio",
            reporting_currency_code="EUR",
            annual_spending_amount=Decimal("48000"),
            annual_spending_currency_code="EUR",
            default_as_of_date=date(2026, 8, 9),
        )
        institution = Institution(portfolio=portfolio, name="Broker")
        db.session.add_all([portfolio, institution])
        db.session.flush()
        account = Account(
            portfolio_id=portfolio.id,
            institution_id=institution.id,
            name="Brokerage",
            account_type="brokerage",
            default_currency_code="EUR",
            is_multicurrency=False,
            cash_tracking_mode="included_in_aggregate",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        instrument = Instrument(
            portfolio_id=portfolio.id,
            name="Global fund",
            instrument_type="fund",
            valuation_currency_code="EUR",
            is_active=True,
        )
        wrapper = Instrument(
            portfolio_id=portfolio.id,
            name="Statement wrapper",
            instrument_type="other",
            valuation_currency_code="EUR",
            is_active=True,
        )
        db.session.add_all([account, instrument, wrapper])
        db.session.flush()
        registration = PositionRegistration(
            account_id=account.id,
            instrument_id=instrument.id,
            tracking_mode="transaction_tracked",
            opening_date=date(2026, 8, 1),
        )
        statement_registration = PositionRegistration(
            account_id=account.id,
            instrument_id=wrapper.id,
            tracking_mode="statement_valued",
            opening_date=date(2026, 8, 1),
        )
        opening = Transaction(
            portfolio_id=portfolio.id,
            transaction_type="opening_balance",
            effective_date=date(2026, 8, 1),
            status="posted",
        )
        db.session.add_all([registration, statement_registration, opening])
        db.session.flush()
        db.session.add(
            Posting(
                transaction_id=opening.id,
                account_id=account.id,
                posting_kind="instrument",
                instrument_id=instrument.id,
                currency_code="EUR",
                quantity_delta=Decimal("10"),
            )
        )
        db.session.commit()
        return instrument.id


def test_values_routes_share_forms_but_not_setup_orientation(
    client: FlaskClient, app: Flask
) -> None:
    instrument_id = _current_position(app)

    maintenance = client.get(
        "/values", query_string={"instrument": instrument_id}
    ).get_data(as_text=True)
    assert "<h1>Update values</h1>" in maintenance
    assert 'aria-label="Setup progress"' not in maintenance
    assert 'action="/values/price"' in maintenance
    assert 'action="/values/statement"' in maintenance
    assert 'action="/values/fx"' in maintenance
    assert 'href="/setup/review">Review initial setup</a>' in maintenance
    assert f'<option selected value="{instrument_id}">Global fund</option>' in maintenance

    guided = client.get(
        "/setup/values", query_string={"instrument": instrument_id}
    ).get_data(as_text=True)
    assert "<h1>Add current values</h1>" in guided
    assert 'aria-label="Setup progress"' in guided
    assert 'action="/setup/values/price"' in guided
    assert 'action="/setup/values/statement"' in guided
    assert 'action="/setup/values/fx"' in guided
    assert "Review initial setup" not in guided
    assert f'<option selected value="{instrument_id}">Global fund</option>' in guided


def test_successful_saves_return_to_originating_pathway(
    client: FlaskClient, app: Flask
) -> None:
    instrument_id = _current_position(app)
    maintenance = client.post(
        "/values/price",
        data={
            "instrument_id": instrument_id,
            "effective_date": "2026-08-08",
            "price_amount": "10",
            "currency_code": "EUR",
        },
    )
    guided = client.post(
        "/setup/values/price",
        data={
            "instrument_id": instrument_id,
            "effective_date": "2026-08-09",
            "price_amount": "11",
            "currency_code": "EUR",
        },
    )
    assert maintenance.status_code == 302
    assert maintenance.headers["Location"].endswith("/values")
    assert guided.status_code == 302
    assert guided.headers["Location"].endswith("/setup/values")
    with app.app_context():
        assert db.session.scalar(select(func.count(Price.id))) == 2


def test_validation_error_stays_in_originating_pathway(
    client: FlaskClient, app: Flask
) -> None:
    instrument_id = _current_position(app)
    data = {
        "instrument_id": instrument_id,
        "effective_date": "2026-08-08",
        "price_amount": "",
        "currency_code": "EUR",
    }
    maintenance = client.post("/values/price", data=data).get_data(as_text=True)
    guided = client.post(
        "/setup/values/price", data=data
    ).get_data(as_text=True)
    assert "This field is required" in maintenance
    assert 'aria-label="Setup progress"' not in maintenance
    assert 'action="/values/price"' in maintenance
    assert "This field is required" in guided
    assert 'aria-label="Setup progress"' in guided
    assert 'action="/setup/values/price"' in guided
    with app.app_context():
        assert db.session.scalar(select(func.count(Price.id))) == 0


def test_values_pathways_keep_identical_error_focus_without_javascript(
    client: FlaskClient, app: Flask
) -> None:
    instrument_id = _current_position(app)
    data = {
        "instrument_id": instrument_id,
        "effective_date": "2026-08-08",
        "price_amount": "",
        "currency_code": "EUR",
    }
    for path, action in (
        ("/values/price", "/values/price"),
        ("/setup/values/price", "/setup/values/price"),
    ):
        body = client.post(path, data=data).get_data(as_text=True)
        assert 'class="error-summary" role="alert"' in body
        assert 'href="#price-price_amount"' in body
        field = re.search(r'<input[^>]*id="price-price_amount"[^>]*>', body).group(0)
        assert 'aria-invalid="true"' in field and "autofocus" in field
        assert f'action="{action}"' in body
        assert_no_inline_script(body)


def test_setup_entry_and_review_links_stay_in_guided_pathway(
    client: FlaskClient, app: Flask
) -> None:
    instrument_id = _current_position(app)
    redirect_response = client.get("/setup?step=values")
    assert redirect_response.status_code == 302
    assert redirect_response.headers["Location"].endswith("/setup/values")

    review = client.get("/setup/review").get_data(as_text=True)
    assert (
        f'href="/setup/values?instrument={instrument_id}#prices-heading"'
        in review
    )
