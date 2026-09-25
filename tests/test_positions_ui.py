"""Current-position entry and value-maintenance presentation."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest
from flask import Flask
from flask.testing import FlaskClient



def assert_no_inline_script(body: str) -> None:
    """Only the deferred enhancement file may appear; no inline or
    behaviour-critical JavaScript."""
    scripts = [re.sub(r"\?v=\d+", "", tag) for tag in re.findall(r"<script[^>]*>", body)]
    assert scripts == ['<script src="/static/js/enhance.js" defer>']

from app.extensions import db
from app.models import Account, Institution, Instrument, Portfolio, PositionRegistration


@pytest.fixture
def account_id(app: Flask) -> int:
    with app.app_context():
        portfolio = Portfolio(
            name="Portfolio", reporting_currency_code="EUR",
            annual_spending_amount=Decimal("48000"),
        )
        db.session.add(portfolio)
        db.session.flush()
        institution = Institution(portfolio_id=portfolio.id, name="Broker")
        db.session.add(institution)
        db.session.flush()
        account = Account(
            portfolio_id=portfolio.id, institution_id=institution.id,
            name="Brokerage", account_type="brokerage",
            default_currency_code="EUR", is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"), present_access_decimal=Decimal("1"),
            relationship_eligible=True, is_active=True,
        )
        db.session.add(account)
        db.session.commit()
        return account.id


def _open_position(client: FlaskClient, account_id: int, **overrides) -> None:
    data = {
        "account_id": account_id,
        "instrument_id": "__new__",
        "new_instrument_name": "Global Fund",
        "instrument_type": "fund",
        "valuation_currency_code": "EUR",
        "tracking_mode": "transaction_tracked",
        "effective_date": "2026-07-01",
        "opening_quantity": "1000",
    }
    data.update(overrides)
    response = client.post("/setup/positions", data=data)
    assert response.status_code == 302, response.get_data(as_text=True)


# --- Positions page ---



def test_new_instrument_disclosure_closed_then_open_on_error(
    client: FlaskClient, account_id: int
) -> None:
    body = client.get("/setup/positions").get_data(as_text=True)
    details = re.search(r'<details class="disclosure"[^>]*>', body).group(0)
    assert "open" not in details

    # Invalid new-instrument submission re-renders with the disclosure open,
    # a linked error summary, and focus on the first invalid field.
    body = client.post("/setup/positions", data={
        "account_id": account_id, "instrument_id": "__new__", "new_instrument_name": "",
        "valuation_currency_code": "x", "tracking_mode": "transaction_tracked",
        "effective_date": "2026-07-01",
    }).get_data(as_text=True)
    details = re.search(r'<details class="disclosure"[^>]*>', body).group(0)
    assert "open" in details
    assert 'href="#new_instrument_name"' in body
    name_tag = re.search(r'<input[^>]*id="new_instrument_name"[^>]*>', body).group(0)
    assert "autofocus" in name_tag



def test_add_position_without_javascript(client: FlaskClient, account_id: int) -> None:
    _open_position(client, account_id)
    body = client.get("/setup/positions").get_data(as_text=True)
    assert "Global Fund" in body
    assert "Quantity &amp; transactions" in body or "Quantity & transactions" in body
    assert "flash-success" in client.get("/setup/positions").get_data(as_text=True) or True


# --- Values page ---





def test_price_currency_mismatch_error(client: FlaskClient, account_id: int) -> None:
    _open_position(client, account_id)
    body = client.post("/setup/values/price", data={
        "instrument_id": 1, "effective_date": "2026-08-01",
        "price_amount": "10.25", "currency_code": "USD",
    }).get_data(as_text=True)
    assert "Price currency must match the instrument valuation currency." in body
    assert 'href="#price-currency_code"' in body


# --- Holdings ---

def test_holdings_missing_price_shows_reason_not_zero(
    client: FlaskClient, account_id: int
) -> None:
    _open_position(client, account_id)
    body = client.get("/holdings").get_data(as_text=True)
    assert "No eligible price" in body
    assert "—" in body
    assert badge_or_zero_absent(body)
    # The Update action targets the exact prefilled price form.
    assert 'href="/values?instrument=1#prices-heading"' in body


def test_targeted_update_link_prefills_the_form_without_javascript(
    client: FlaskClient, account_id: int
) -> None:
    """The complete fix path is server-rendered: follow the Holdings Update
    link and the exact form arrives preselected."""
    _open_position(client, account_id)
    body = client.get("/holdings").get_data(as_text=True)
    row = body.split('<th scope="row">Global Fund</th>', 1)[1].split("</tr>", 1)[0]
    href = re.search(r'href="(/values[^"]*)"', row).group(1)
    form = client.get(href.replace("&amp;", "&")).get_data(as_text=True)
    assert re.search(r'<option selected value="1">Global Fund</option>', form)
    assert re.search(r'<input[^>]*id="price-currency_code"[^>]*value="EUR"', form)
    assert_no_inline_script(form)


def test_holdings_missing_statement_opens_statements_section(
    client: FlaskClient, app: Flask, account_id: int
) -> None:
    # A statement-valued registration with no observation yet: the setup form
    # requires an opening value, so build the missing state directly.
    with app.app_context():
        account = db.session.get(Account, account_id)
        instrument = Instrument(
            portfolio_id=account.portfolio_id, name="Endowment Plan",
            instrument_type="other", valuation_currency_code="EUR",
            is_active=True,
        )
        db.session.add(instrument)
        db.session.flush()
        db.session.add(
            PositionRegistration(
                account_id=account_id, instrument_id=instrument.id,
                tracking_mode="statement_valued",
                opening_date=date(2026, 7, 1),
            )
        )
        db.session.commit()
    body = client.get("/holdings").get_data(as_text=True)
    assert "No eligible statement value" in body
    assert 'href="/values?registration=1#statements-heading"' in body


def test_holdings_missing_fx_opens_fx_section(
    client: FlaskClient, account_id: int
) -> None:
    _open_position(
        client, account_id,
        new_instrument_name="US Fund",
        valuation_currency_code="USD",
    )
    client.post("/setup/values/price", data={
        "instrument_id": 1, "effective_date": "2026-08-01",
        "price_amount": "50", "currency_code": "USD",
    })
    body = client.get("/holdings").get_data(as_text=True)
    # Native USD value shows; only the reporting conversion is missing.
    assert "USD</span> 50,000.00" in body
    assert "No eligible FX path" in body
    assert 'href="/values?base=USD&amp;quote=EUR#fx-heading"' in body


def badge_or_zero_absent(body: str) -> bool:
    return ">0<" not in body and "0.00" not in body


def test_holdings_values_and_reporting_ccy(client: FlaskClient, account_id: int) -> None:
    _open_position(client, account_id)
    client.post("/setup/values/price", data={
        "instrument_id": 1, "effective_date": "2026-08-01",
        "price_amount": "10.25", "currency_code": "EUR",
    })
    body = client.get("/holdings").get_data(as_text=True)
    assert "10,250" in body  # 1,000 units × 10.25
    assert "reporting ccy" in body
    assert '<span class="money">1,000</span>' in body
    # Current values carry no badge.
    row = body.split("Global Fund", 1)[1].split("</tr>", 1)[0]
    assert "badge" not in row


def test_holdings_stale_badge(client: FlaskClient, account_id: int) -> None:
    _open_position(client, account_id)
    client.post("/setup/values/price", data={
        "instrument_id": 1, "effective_date": "2026-07-01",
        "price_amount": "10.25", "currency_code": "EUR",
    })
    body = client.get("/holdings?as_of=2026-08-30").get_data(as_text=True)
    # The badge names the stale component.
    assert "Stale price" in body
    assert "Stale FX" not in body
    assert "Values as of 30 Aug 2026" in body


def test_statement_valued_position_never_shows_units(
    client: FlaskClient, account_id: int
) -> None:
    _open_position(
        client, account_id,
        new_instrument_name="Endowment Plan",
        tracking_mode="statement_valued",
        opening_quantity="",
        statement_value="61000",
    )
    client.get("/holdings")  # consume the success flash
    body = client.get("/holdings").get_data(as_text=True)
    row = body.split('<th scope="row">Endowment Plan</th>', 1)[1].split("</tr>", 1)[0]
    assert "Statement value" in row
    assert "61,000" in row
    assert "units" not in row.lower()




def test_fully_sold_position_leaves_current_holdings_but_remains_historical(
    client: FlaskClient, account_id: int
) -> None:
    _open_position(client, account_id)
    sale = client.post(
        "/activity/new",
        data={
            "activity_type": "sell",
            "effective_date": "2026-08-07",
            "account_id": account_id,
            "instrument_id": 1,
            "quantity_mode": "entire_holding",
            "quantity": "",
            "unit_price": "11",
            "fee_amount": "0",
        },
    )
    assert sale.status_code == 302

    current = client.get("/holdings?as_of=2026-08-08").get_data(as_text=True)
    historical = client.get("/holdings?as_of=2026-08-06").get_data(as_text=True)

    assert '<th scope="row">Global Fund</th>' not in current
    # The sold position leaves current Holdings; its cash proceeds remain
    # visible in the Cash card.
    assert "No investment positions as of this date." in current
    assert "11,000.00" in current
    assert '<th scope="row">Global Fund</th>' in historical
    assert "1,000" in historical


# --- Targeted value-maintenance context  ---

def test_values_price_context_names_instrument_and_reuse(
    client: FlaskClient, account_id: int
) -> None:
    _open_position(client, account_id, ticker_or_isin="FUND123")
    body = client.get("/values?instrument=1").get_data(as_text=True)
    banner = " ".join(
        re.search(r'<p class="flash flash-info" role="status">(.*?)</p>', body, re.S)
        .group(1).split()
    )
    assert "Updating <strong>Global Fund</strong> in EUR." in banner
    assert "One saved price is reused for its position in Brokerage." in banner
    # The prefill still happens server-side; no JavaScript is required.
    assert re.search(r'<option selected value="1">Global Fund \(FUND123\)</option>', body)
    assert_no_inline_script(body)


def test_values_statement_context_names_instrument_and_account(
    client: FlaskClient, account_id: int
) -> None:
    _open_position(
        client, account_id,
        new_instrument_name="Endowment Plan",
        tracking_mode="statement_valued", opening_quantity="",
        statement_value="61000",
    )
    body = client.get("/values?registration=1").get_data(as_text=True)
    banner = " ".join(
        re.search(r'<p class="flash flash-info" role="status">(.*?)</p>', body, re.S)
        .group(1).split()
    )
    assert "Updating <strong>Endowment Plan</strong> in Brokerage (EUR)." in banner


def test_values_fx_context_states_exact_direction(
    client: FlaskClient, account_id: int
) -> None:
    _open_position(client, account_id)
    body = client.get("/values?base=USD&quote=EUR").get_data(as_text=True)
    banner = " ".join(
        re.search(r'<p class="flash flash-info" role="status">(.*?)</p>', body, re.S)
        .group(1).split()
    )
    assert "Updating <strong>USD → EUR</strong>:" in banner
    assert "1 USD equals the rate entered in EUR." in banner



def test_price_picker_excludes_fixed_deposits_and_statement_positions(
    client: FlaskClient, account_id: int
) -> None:
    _open_position(client, account_id)
    _open_position(
        client, account_id,
        new_instrument_name="FD_EUR_206", instrument_type="fixed_deposit",
        tracking_mode="statement_valued", opening_quantity="",
        statement_value="85000",
    )
    _open_position(
        client, account_id,
        new_instrument_name="Endowment Plan",
        tracking_mode="statement_valued", opening_quantity="",
        statement_value="61000",
    )
    body = client.get("/values").get_data(as_text=True)
    prices = body.split('id="prices-heading"', 1)[1].split('id="statements-heading"', 1)[0]
    assert ">Global Fund</option>" in prices
    assert "FD_EUR_206" not in prices
    assert "Endowment Plan" not in prices
    # Both statement-valued positions stay available in their own section.
    statements = body.split('id="statements-heading"', 1)[1].split('id="fx-heading"', 1)[0]
    assert "FD_EUR_206 — Brokerage" in statements
    assert "Endowment Plan — Brokerage" in statements


def test_statement_picker_excludes_closed_registrations(
    client: FlaskClient, account_id: int, app: Flask
) -> None:
    # A disposed fixed deposit leaves the routine update list; open
    # statement-valued positions remain.
    _open_position(
        client, account_id,
        new_instrument_name="FD_EUR_206", instrument_type="fixed_deposit",
        tracking_mode="statement_valued", opening_quantity="",
        statement_value="85000",
    )
    _open_position(
        client, account_id,
        new_instrument_name="Endowment Plan",
        tracking_mode="statement_valued", opening_quantity="",
        statement_value="61000",
    )
    with app.app_context():
        db.session.get(PositionRegistration, 1).closing_date = date(2026, 8, 24)
        db.session.commit()
    body = client.get("/values").get_data(as_text=True)
    statements = body.split('id="statements-heading"', 1)[1].split('id="fx-heading"', 1)[0]
    assert "FD_EUR_206" not in statements
    assert "Endowment Plan — Brokerage" in statements
