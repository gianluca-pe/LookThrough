"""Dividend UI: outcome selection, preview, receipt, enhancement and request budget."""

from __future__ import annotations

import re
import time
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
from app.models import (
    Account,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
)


@pytest.fixture
def records(app: Flask) -> dict:
    with app.app_context():
        portfolio = Portfolio(
            name="Personal portfolio", reporting_currency_code="EUR",
            annual_spending_amount=Decimal("48000"),
        )
        db.session.add(portfolio)
        db.session.flush()
        institution = Institution(portfolio_id=portfolio.id, name="Broker A")
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
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        source = Instrument(
            portfolio_id=portfolio.id, name="Income Fund",
            ticker_or_isin="INC1", instrument_type="fund",
            valuation_currency_code="EUR", is_active=True,
        )
        target = Instrument(
            portfolio_id=portfolio.id, name="Bond ETF",
            ticker_or_isin="BOND1", instrument_type="etf",
            valuation_currency_code="EUR", is_active=True,
        )
        db.session.add_all([account, source, target])
        db.session.flush()
        db.session.add(
            PositionRegistration(
                account_id=account.id,
                instrument_id=source.id,
                tracking_mode="transaction_tracked",
                opening_date=date(2026, 1, 1),
            )
        )
        db.session.commit()
        return {
            "portfolio_id": portfolio.id,
            "account_id": account.id,
            "source_id": source.id,
            "target_id": target.id,
        }


def _dividend_data(records: dict, **overrides) -> dict:
    data = {
        "effective_date": "2026-08-04",
        "account_id": records["account_id"],
        "instrument_id": records["source_id"],
        "currency_code": "EUR",
        "net_amount": "425",
        "outcome": "cash",
        "gross_amount": "500",
        "withholding_amount": "75",
        "reinvestment_instrument_id": "0",
        "reinvestment_quantity": "",
        "reinvestment_unit_price": "",
        "reinvestment_purchase_amount": "",
        "reinvestment_fee_amount": "",
    }
    data.update(overrides)
    return data


# --- Chooser entry point ---


def test_dividend_pickers_exclude_cash_and_fixed_deposits(
    client: FlaskClient, app: Flask, records: dict
) -> None:
    with app.app_context():
        db.session.add_all(
            [
                Instrument(
                    portfolio_id=records["portfolio_id"],
                    name="Cash EUR",
                    instrument_type="cash",
                    valuation_currency_code="EUR",
                    is_active=True,
                ),
                Instrument(
                    portfolio_id=records["portfolio_id"],
                    name="FD EUR 001",
                    instrument_type="fixed_deposit",
                    valuation_currency_code="EUR",
                    is_active=True,
                ),
            ]
        )
        db.session.commit()

    body = client.get("/activity/dividend/new").get_data(as_text=True)
    source_picker = re.search(
        r'<select[^>]*id="instrument_id".*?</select>', body, re.S
    ).group(0)
    reinvestment_picker = re.search(
        r'<select[^>]*id="reinvestment_instrument_id".*?</select>', body, re.S
    ).group(0)
    for picker in (source_picker, reinvestment_picker):
        assert "Income Fund" in picker
        assert "Cash EUR" not in picker
        assert "FD EUR 001" not in picker



# --- Form keyboard contract ---





# --- Outcome radio cards ---



# --- Disclosures ---


def test_reinvestment_disclosure_opens_after_reinvestment_submit(
    client: FlaskClient, records: dict
) -> None:
    body = client.post(
        "/activity/dividend/preview",
        data=_dividend_data(
            records, outcome="reinvest_same", reinvestment_quantity="40",
            reinvestment_unit_price="10.60", reinvestment_fee_amount="1",
        ),
    ).get_data(as_text=True)
    tag = re.search(r"<details[^>]*data-reinvestment-details[^>]*>", body).group(0)
    assert "open" in tag


def test_reinvestment_error_opens_disclosure_and_focuses_material_field(
    client: FlaskClient, records: dict
) -> None:
    body = client.post(
        "/activity/dividend",
        data=_dividend_data(records, outcome="reinvest_other"),
    ).get_data(as_text=True)
    assert "There is a problem" in body
    assert 'href="#reinvestment_instrument_id"' in body
    tag = re.search(r"<details[^>]*data-reinvestment-details[^>]*>", body).group(0)
    assert "open" in tag
    instrument_tag = re.search(
        r'<select[^>]*id="reinvestment_instrument_id"[^>]*>', body
    ).group(0)
    assert "autofocus" in instrument_tag
    assert 'aria-invalid="true"' in instrument_tag


# --- Server-rendered preview presentation ---

def test_cash_preview_sentence_is_exact(
    client: FlaskClient, records: dict
) -> None:
    body = client.post(
        "/activity/dividend/preview", data=_dividend_data(records)
    ).get_data(as_text=True)

    region = re.search(r'<div id="preview-region"[^>]*>', body).group(0)
    assert 'role="status"' in region and 'aria-live="polite"' in region
    assert '<span class="ccy">EUR</span> 500.00' in body
    assert "entered withholding" in body
    assert '<span class="ccy">EUR</span> 75.00' in body
    assert '<span class="ccy">EUR</span> 425.00' in body
    assert "Net account cash effect" in body
    assert "Linked Buy" not in body
    assert_no_inline_script(body)


def test_reinvest_preview_shows_linked_buy_and_zero_cash_effect(
    client: FlaskClient, records: dict
) -> None:
    body = client.post(
        "/activity/dividend/preview",
        data=_dividend_data(
            records, outcome="reinvest_same", reinvestment_quantity="40",
            reinvestment_unit_price="10.60", reinvestment_fee_amount="1",
        ),
    ).get_data(as_text=True)
    assert "Linked Buy" in body
    assert "40 units" in body
    assert "Income Fund" in body
    assert '<span class="ccy">EUR</span> 424.00' in body
    assert '<span class="ccy">EUR</span> 1.00' in body
    assert "Net account cash effect" in body
    assert '<span class="ccy">EUR</span> 0.00' in body


def test_negative_residual_explains_cash_reduction(
    client: FlaskClient, records: dict
) -> None:
    body = client.post(
        "/activity/dividend/preview",
        data=_dividend_data(
            records, net_amount="100", gross_amount="",
            withholding_amount="", outcome="reinvest_same",
            reinvestment_quantity="40", reinvestment_unit_price="10.60",
        ),
    ).get_data(as_text=True)
    assert '<span class="ccy">EUR</span> -324.00' in body
    assert "the difference reduces account cash" in body


def test_positive_residual_has_no_reduction_copy(
    client: FlaskClient, records: dict
) -> None:
    body = client.post(
        "/activity/dividend/preview", data=_dividend_data(records)
    ).get_data(as_text=True)
    assert "reduces account cash" not in body


# --- Success receipt ---

def _post_dividend(client: FlaskClient, records: dict, **overrides) -> str:
    response = client.post(
        "/activity/dividend", data=_dividend_data(records, **overrides)
    )
    assert response.status_code == 302
    return client.get(response.headers["Location"]).get_data(as_text=True)




def test_reinvest_success_with_negative_residual_shows_reduction_copy(
    client: FlaskClient, records: dict
) -> None:
    body = _post_dividend(
        client, records, net_amount="100", gross_amount="",
        withholding_amount="", outcome="reinvest_same",
        reinvestment_quantity="40", reinvestment_unit_price="10.60",
    )
    assert '<span class="ccy">EUR</span> -324.00' in body
    assert "the difference reduced account cash" in body


# --- Progressive enhancement ---


# --- Observed timing ---

def test_cash_dividend_completes_within_request_budget(
    client: FlaskClient, records: dict
) -> None:
    """Observed-timing guard: the complete no-JS path is exactly three page
    loads (form, post-redirect, receipt) and stays well inside a generous
    wall-clock budget locally."""
    app = client.application
    app.config["CURRENT_DATE_PROVIDER"] = lambda: date(2026, 8, 4)

    started = time.perf_counter()
    form = client.get("/activity/dividend/new")
    assert form.status_code == 200
    posted = client.post("/activity/dividend", data=_dividend_data(records))
    assert posted.status_code == 302
    receipt = client.get(posted.headers["Location"])
    assert receipt.status_code == 200
    elapsed = time.perf_counter() - started

    assert "Dividend recorded" in receipt.get_data(as_text=True)
    assert elapsed < 2.0


# --- Contextual Cancel (return_to, whitelisted) ---

def test_dividend_cancel_honors_whitelisted_return_to(
    client: FlaskClient, records: dict
) -> None:
    body = client.get(
        "/activity/dividend/new?return_to=holdings"
    ).get_data(as_text=True)
    assert 'href="/holdings">Cancel</a>' in body
    # The context survives a failed POST: the form action carries it.
    assert 'action="/activity/dividend?return_to=holdings"' in body
    failed = client.post(
        "/activity/dividend?return_to=holdings",
        data=_dividend_data(records, net_amount=""),
    ).get_data(as_text=True)
    assert "There is a problem" in failed
    assert 'href="/holdings">Cancel</a>' in failed

    default = client.get("/activity/dividend/new").get_data(as_text=True)
    assert 'href="/overview">Cancel</a>' in default
