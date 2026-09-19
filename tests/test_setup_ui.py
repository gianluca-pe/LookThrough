"""Setup UI tests: presentation, accessibility, and the
resumable browser flow against the backend contract in app/setup.py.
"""

from __future__ import annotations

import re

from flask import Flask
from sqlalchemy import select

from app.extensions import db
from app.models import Portfolio



def assert_no_inline_script(body: str) -> None:
    """Only the deferred enhancement file may appear; no inline or
    behaviour-critical JavaScript."""
    scripts = [re.sub(r"\?v=\d+", "", tag) for tag in re.findall(r"<script[^>]*>", body)]
    assert scripts == ['<script src="/static/js/enhance.js" defer>']

import pytest
from flask.testing import FlaskClient


PORTFOLIO_VALID = {
    "name": "Personal portfolio",
    "reporting_currency_code": "sgd",
    "annual_spending_amount": "48000",
    "annual_spending_currency_code": "sgd",
    "save_and_continue": "Save and continue",
}

ACCOUNT_VALID = {
    "institution_id": "1",
    "name": "Brokerage EUR",
    "account_type": "brokerage",
    "default_currency_code": "eur",
    "cash_tracking_mode": "separate_cash",
    "portfolio_share_percent": "100",
    "present_access_percent": "100",
    "relationship_eligible": "y",
    "add_account": "Add account",
}


def _make_portfolio(client: FlaskClient) -> None:
    response = client.post("/setup/portfolio", data=PORTFOLIO_VALID)
    assert response.status_code == 302


def _make_institution(client: FlaskClient, name: str = "Example Bank") -> None:
    response = client.post("/setup/institutions", data={"name": name})
    assert response.status_code == 302


def _make_account(client: FlaskClient) -> None:
    response = client.post("/setup/accounts", data=ACCOUNT_VALID)
    assert response.status_code == 302


# --- Empty state and step rail ---

def test_empty_setup_shows_portfolio_step(client: FlaskClient) -> None:
    body = client.get("/setup").get_data(as_text=True)
    assert "<h1>Set up your portfolio</h1>" in body
    assert body.count('aria-current="step"') == 1
    rail = body.split('aria-label="Setup progress"', 1)[1].split("</nav>", 1)[0]
    # Initial setup links to Portfolio, Accounts, Positions and Values.
    # Cash and Review are not linked until their prerequisites exist.
    assert rail.count("<a href=") == 4
    for label in ("Positions", "Cash", "Values", "Review"):
        assert f">{label}<" in rail
    assert "(upcoming)" in rail
    assert "(completed)" not in rail


def test_required_fields_stated_in_text(client: FlaskClient) -> None:
    body = client.get("/setup").get_data(as_text=True)
    # Name, reporting currency, spending amount, and spending currency.
    assert body.count('class="req">(required)') == 4


def test_no_javascript_anywhere(client: FlaskClient) -> None:
    _make_portfolio(client)
    for path in ("/setup", "/setup?step=accounts"):
        assert_no_inline_script(client.get(path).get_data(as_text=True))


# --- Validation presentation ---

def test_portfolio_validation_summary_and_focus(client: FlaskClient) -> None:
    body = client.post(
        "/setup/portfolio",
        data={"name": "", "reporting_currency_code": "x", "annual_spending_amount": "-5"},
    ).get_data(as_text=True)

    assert 'class="error-summary" role="alert"' in body
    # Summary items are anchor links to their fields.
    assert 'href="#name"' in body
    assert 'href="#reporting_currency_code"' in body
    assert 'href="#annual_spending_amount"' in body
    assert 'href="#annual_spending_currency_code"' in body
    # Fields carry ARIA error wiring.
    assert body.count('aria-invalid="true"') == 4
    assert 'aria-describedby="name-error name-hint"' in body
    # First invalid field receives focus without JavaScript.
    name_tag = re.search(r'<input[^>]*id="name"[^>]*>', body).group(0)
    assert "autofocus" in name_tag
    # Optional attributes are omitted, never rendered literally.
    assert '="None"' not in body
    # No partial record was created: empty setup still shows the portfolio step.
    assert "<h1>Set up your portfolio</h1>" in body


def test_spending_currency_round_trip_independent_of_reporting(
    client: FlaskClient, app: Flask
) -> None:
    # Amount and spending currency persist
    # independently; editing the saved reporting currency never silently
    # moves the spending currency.
    client.post("/setup/portfolio", data={
        **PORTFOLIO_VALID,
        "reporting_currency_code": "eur",
        "annual_spending_currency_code": "usd",
    })
    with app.app_context():
        portfolio = db.session.scalar(select(Portfolio))
        assert portfolio.reporting_currency_code == "EUR"
        assert portfolio.annual_spending_currency_code == "USD"

    client.post("/setup/portfolio", data={
        **PORTFOLIO_VALID,
        "reporting_currency_code": "sgd",
        "annual_spending_currency_code": "usd",
    })
    with app.app_context():
        portfolio = db.session.scalar(select(Portfolio))
        assert portfolio.reporting_currency_code == "SGD"
        assert portfolio.annual_spending_currency_code == "USD"

    body = client.get("/setup?step=portfolio").get_data(as_text=True)
    spending_input = re.search(
        r'<input[^>]*id="annual_spending_currency_code"[^>]*>', body
    ).group(0)
    assert 'value="USD"' in spending_input
    reporting_input = re.search(
        r'<input[^>]*id="reporting_currency_code"[^>]*>', body
    ).group(0)
    assert 'value="SGD"' in reporting_input
    # The meaning distinction is stated next to the fields.
    assert "saved default" in body
    assert "never changes its meaning" in body


def test_institution_duplicate_error_is_inline(client: FlaskClient) -> None:
    _make_portfolio(client)
    _make_institution(client)
    body = client.post("/setup/institutions", data={"name": "example bank"}).get_data(as_text=True)
    assert "This institution is already in the portfolio." in body
    assert 'id="institution-name-error"' in body
    name_tag = re.search(r'<input[^>]*id="institution-name"[^>]*>', body).group(0)
    assert "autofocus" in name_tag


# --- Flow, disclosure, resumption ---

def test_save_and_continue_lands_on_accounts_with_name_in_shell(client: FlaskClient) -> None:
    _make_portfolio(client)
    body = client.get("/setup?step=accounts").get_data(as_text=True)
    assert '<span class="portfolio-name">· Personal portfolio</span>' in body
    rail = body.split('aria-label="Setup progress"', 1)[1].split("</nav>", 1)[0]
    assert 'aria-current="step"' in rail
    # Portfolio, Accounts, Positions, and Values are linked; Cash and Review
    # remain unlinked until an account and a position exist, respectively.
    assert rail.count("<a href=") == 4
    assert "(completed)" in rail


def test_account_form_progressive_disclosure(client: FlaskClient) -> None:
    _make_portfolio(client)
    _make_institution(client)
    body = client.get("/setup?step=accounts").get_data(as_text=True)

    details = body.split('<details class="disclosure">', 1)[1].split("</details>", 1)[0]
    # Optional access/relationship fields are under disclosure…
    for field_id in ("earliest_access_date", "access_note", "relationship_eligible"):
        assert f'name="{field_id}"' in details
    # …and no required field is hidden inside it.
    for field_id in ("institution_id", "account_type", "default_currency_code",
                     "cash_tracking_mode", "portfolio_share_percent", "present_access_percent"):
        assert f'name="{field_id}"' not in details
    assert "Access and relationship details (optional)" in body


def test_account_reference_is_optional_and_labelled(client: FlaskClient) -> None:
    _make_portfolio(client)
    _make_institution(client)
    body = client.get("/setup?step=accounts").get_data(as_text=True)
    assert '<label for="account-reference">' in body
    label = re.search(
        r'<label for="account-reference">(.*?)</label>', body
    ).group(1)
    assert "(required)" not in label
    field = re.search(r'<input[^>]*id="account-reference"[^>]*>', body).group(0)
    assert "required" not in field


def test_full_flow_and_resumption(client: FlaskClient) -> None:
    _make_portfolio(client)
    _make_institution(client)
    _make_account(client)

    # Leaving and reopening /setup resumes at Accounts with the saved account.
    body = client.get("/setup").get_data(as_text=True)
    assert "<h1>Add accounts</h1>" in body
    assert "Brokerage EUR" in body
    assert "Example Bank" in body
    assert "Brokerage account" in body
    assert "EUR" in body
    assert "Cash tracked separately" in body
    assert ">100%</td>" in body  # share and access rendered as entered percentages
    assert "Current accounts are saved" in body

    # Institution list shows the account count.
    institutions = body.split('aria-labelledby="institutions-heading"', 1)[1]
    assert "1 account<" in institutions or "1 account\n" in institutions or "1 account" in institutions


def test_account_needs_institution_first(client: FlaskClient) -> None:
    _make_portfolio(client)
    body = client.get("/setup?step=accounts").get_data(as_text=True)
    assert "Add an institution above before adding an account." in body
    assert 'name="portfolio_share_percent"' not in body


def test_labels_match_inputs(client: FlaskClient) -> None:
    _make_portfolio(client)
    _make_institution(client)
    for path in ("/setup?step=portfolio", "/setup?step=accounts"):
        body = client.get(path).get_data(as_text=True)
        labels = set(re.findall(r'<label for="([^"]+)"', body))
        controls = set(re.findall(r'<(?:input|select|textarea)[^>]* id="([^"]+)"', body))
        assert labels <= controls
        ids = re.findall(r'<(?:input|select|textarea)[^>]* id="([^"]+)"', body)
        assert len(ids) == len(set(ids)), f"duplicate element ids on {path}"


def test_setup_account_disclosure_reopens_on_error(client: FlaskClient) -> None:
    """A failing access field re-opens its disclosure server-side."""
    _make_portfolio(client)
    _make_institution(client)
    body = client.post(
        "/setup/accounts",
        data={**ACCOUNT_VALID, "earliest_access_date": "31/08/2026"},
    ).get_data(as_text=True)
    assert "There is a problem" in body
    details = re.search(r'<details class="disclosure"[^>]*>', body).group(0)
    assert "open" in details
    assert 'id="account-earliest_access_date-error"' in body


def test_setup_position_disclosure_reopens_on_ticker_error(
    client: FlaskClient,
) -> None:
    """With an existing instrument chosen, a ticker error alone re-opens
    the new-instrument disclosure (instrument_id is not the new-instrument
    sentinel, so only the error can open it)."""
    _make_portfolio(client)
    _make_institution(client)
    _make_account(client)
    created = client.post("/setup/positions", data={
        "account_id": "1", "instrument_id": "__new__",
        "new_instrument_name": "Global Fund", "instrument_type": "fund",
        "valuation_currency_code": "eur", "tracking_mode": "transaction_tracked",
        "effective_date": "2026-08-01", "opening_quantity": "1000",
        "save_position": "Add position",
    })
    assert created.status_code == 302

    body = client.post("/setup/positions", data={
        "account_id": "1", "instrument_id": "1",
        "new_instrument_name": "", "ticker_or_isin": "X" * 65,
        "instrument_type": "fund", "valuation_currency_code": "eur",
        "tracking_mode": "transaction_tracked", "effective_date": "2026-08-01",
        "opening_quantity": "500", "save_position": "Add position",
    }).get_data(as_text=True)
    assert "There is a problem" in body
    details = re.search(r'<details class="disclosure"[^>]*>', body).group(0)
    assert "open" in details
    assert 'id="ticker_or_isin-error"' in body
