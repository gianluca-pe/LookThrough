"""Inline-instrument and sale-to-replacement flows: context, focus, validation and tamper resistance."""

from __future__ import annotations

import re
import time
from datetime import date
from decimal import Decimal

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select


def assert_no_inline_script(body: str) -> None:
    """Only the deferred enhancement file may appear; no inline or
    behaviour-critical JavaScript."""
    scripts = [re.sub(r"\?v=\d+", "", tag) for tag in re.findall(r"<script[^>]*>", body)]
    assert scripts == ['<script src="/static/js/enhance.js" defer>']


from app.extensions import db
from app.models import Account, Institution, Instrument, Portfolio, Transaction


@pytest.fixture
def records(app: Flask) -> dict:
    app.config["CURRENT_DATE_PROVIDER"] = lambda: date(2026, 8, 4)
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
            portfolio_id=portfolio.id, institution_id=institution.id,
            name="Brokerage EUR", account_type="brokerage",
            default_currency_code="EUR", is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"), present_access_decimal=Decimal("1"),
            relationship_eligible=True, is_active=True,
        )
        instrument = Instrument(
            portfolio_id=portfolio.id, name="Global Fund",
            ticker_or_isin="FUND123", instrument_type="fund",
            valuation_currency_code="EUR", is_active=True,
        )
        db.session.add_all([account, instrument])
        db.session.commit()
        return {
            "portfolio_id": portfolio.id,
            "account_id": account.id,
            "instrument_id": instrument.id,
        }


def _trade_data(records: dict, **overrides) -> dict:
    data = {
        "activity_type": "buy",
        "effective_date": "2026-08-04",
        "account_id": records["account_id"],
        "instrument_id": records["instrument_id"],
        "quantity": "1000",
        "unit_price": "10.25",
        "fee_amount": "25",
    }
    data.update(overrides)
    return data


def _identity_data(records: dict, **overrides) -> dict:
    data = _trade_data(
        records,
        instrument_id=0,
        new_instrument_name="Short Bond ETF",
        new_ticker_or_isin="BOND1",
        new_valuation_currency_code="EUR",
        new_instrument_type="etf",
        create_instrument="Save and use instrument",
    )
    data.update(overrides)
    return data


def _sell(client: FlaskClient, records: dict, **overrides) -> str:
    _buy(client, records)
    response = client.post(
        "/activity/new",
        data=_trade_data(
            records, activity_type="sell", quantity="300",
            unit_price="11.10", fee_amount="20", **overrides
        ),
    )
    assert response.status_code == 302
    return response.headers["Location"]


def _buy(client: FlaskClient, records: dict, **overrides) -> None:
    response = client.post("/activity/new", data=_trade_data(records, **overrides))
    assert response.status_code == 302, response.get_data(as_text=True)


# --- Inline instrument disclosure ---

def test_buy_form_offers_compact_identity_disclosure(
    client: FlaskClient, records: dict
) -> None:
    buy = client.get("/activity/new?type=buy").get_data(as_text=True)
    sell = client.get("/activity/new?type=sell").get_data(as_text=True)

    assert ">Add a new instrument</option>" in buy
    assert ">Add a new instrument</option>" not in sell
    details = re.search(r'<details class="disclosure"[^>]*>', buy).group(0)
    assert "open" not in details
    assert "<summary>Add a new instrument</summary>" in buy
    for field_id in (
        "new_instrument_name", "new_ticker_or_isin",
        "new_valuation_currency_code", "new_instrument_type",
    ):
        assert f'id="{field_id}"' in buy
        assert f'id="{field_id}"' not in sell
    assert "classify it later" in buy
    # Enter submits the primary trade action: Record precedes both the
    # Preview and the identity-save submit in DOM order.
    assert buy.index('id="post_trade"') < buy.index('id="preview_trade"')
    assert buy.index('id="post_trade"') < buy.index('id="create_instrument"')
    assert_no_inline_script(buy)


def test_recording_with_unsaved_new_instrument_instructs_save(
    client: FlaskClient, records: dict
) -> None:
    body = client.post(
        "/activity/new", data=_trade_data(records, instrument_id=0)
    ).get_data(as_text=True)
    assert "Save the new instrument before recording this Buy" in body
    assert 'href="#instrument_id"' in body
    instrument_tag = re.search(
        r'<select[^>]*id="instrument_id"[^>]*>', body
    ).group(0)
    assert "autofocus" in instrument_tag


def test_identity_save_returns_focused_populated_buy(
    client: FlaskClient, records: dict
) -> None:
    response = client.post(
        "/activity/instruments",
        data=_identity_data(records, quantity="125", unit_price="20.5", fee_amount="5"),
    )
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/activity/new?type=buy")

    returned = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "flash-success" in returned
    assert "Short Bond ETF saved. Finish recording the Buy." in returned
    selected = re.search(
        r'<option selected value="(\d+)">Short Bond ETF \(BOND1\)</option>',
        returned,
    )
    assert selected
    # Entered trade values survived the round trip.
    assert 'value="125"' in returned
    assert 'value="20.5"' in returned
    assert 'value="5"' in returned
    # The disclosure is closed again and every field is filled, so nothing
    # steals focus on the return page.
    details = re.search(r'<details class="disclosure"[^>]*>', returned).group(0)
    assert "open" not in details
    assert returned.count("autofocus") == 0
    assert_no_inline_script(returned)


def test_invalid_identity_save_opens_disclosure_and_focuses(
    client: FlaskClient, records: dict
) -> None:
    body = client.post(
        "/activity/instruments",
        data=_identity_data(records, new_instrument_name=""),
    ).get_data(as_text=True)
    details = re.search(r'<details class="disclosure"[^>]*>', body).group(0)
    assert "open" in details
    assert 'href="#new_instrument_name"' in body
    name_tag = re.search(
        r'<input[^>]*id="new_instrument_name"[^>]*>', body
    ).group(0)
    assert "autofocus" in name_tag
    assert 'aria-invalid="true"' in name_tag


# --- Sale-to-replacement continuation ---

def test_sale_success_offers_replacement_only_when_eligible(
    client: FlaskClient, records: dict
) -> None:
    sale_location = _sell(client, records)
    success = client.get(sale_location).get_data(as_text=True)
    action = re.search(
        r'<a class="btn btn-primary" href="(/activity/\d+/replacement)">'
        r"Use sale proceeds to buy another instrument</a>",
        success,
    )
    assert action
    # The replacement action is the first exit offered.
    assert success.index("/replacement") < success.index("Add another")

    # A Buy receipt never offers it.
    buy_response = client.post("/activity/new", data=_trade_data(records))
    buy_success = client.get(buy_response.headers["Location"]).get_data(as_text=True)
    assert "Use sale proceeds" not in buy_success
    assert_no_inline_script(success)


def test_replacement_form_context_panel_and_safe_prefill(
    client: FlaskClient, records: dict
) -> None:
    sale_location = _sell(client, records)
    sale_id = sale_location.split("/")[2]
    body = client.get(f"/activity/{sale_id}/replacement").get_data(as_text=True)

    panel = body.split('class="context-panel"', 1)[1].split("</section>", 1)[0]
    assert "Continue from sale" in panel
    assert '<span class="ccy">EUR</span> 3,310.00' in panel
    assert "Brokerage EUR" in panel
    assert "4 Aug 2026" in panel
    assert "separate activity" in panel

    # Account, date, and hidden continuation context arrive prefilled.
    assert f'value="{sale_id}"' in body
    assert re.search(
        r'<option selected value="\d+">Broker A — Brokerage EUR</option>', body
    )
    assert 'value="2026-08-04"' in body
    # The replacement still allows creating a new instrument inline; its
    # currency defaults to the sale's settlement currency.
    assert ">Add a new instrument</option>" in body
    currency_tag = re.search(
        r'<input[^>]*id="new_valuation_currency_code"[^>]*>', body
    ).group(0)
    assert 'value="EUR"' in currency_tag
    assert_no_inline_script(body)


def test_replacement_route_rejects_tampered_or_buy_sources(
    client: FlaskClient, records: dict
) -> None:
    assert client.get("/activity/999/replacement").status_code == 404
    _buy(client, records)
    assert client.get("/activity/1/replacement").status_code == 404


def test_replacement_buy_is_a_separate_ungrouped_activity(
    app: Flask, client: FlaskClient, records: dict
) -> None:
    sale_location = _sell(client, records)
    sale_id = sale_location.split("/")[2]
    replacement = client.get(f"/activity/{sale_id}/replacement")
    assert replacement.status_code == 200

    buy_response = client.post(
        "/activity/new",
        data=_trade_data(
            records, quantity="100", unit_price="30", fee_amount="5",
            replacement_sale_id=sale_id,
        ),
    )
    assert buy_response.status_code == 302
    buy_success = client.get(buy_response.headers["Location"]).get_data(as_text=True)
    assert "Buy recorded" in buy_success

    # The source sale is untouched and neither side is grouped.
    with app.app_context():
        transactions = list(
            db.session.scalars(select(Transaction).order_by(Transaction.id))
        )
        assert len(transactions) == 3  # opening buy, sale, replacement buy
        assert all(row.activity_group_id is None for row in transactions)
    sale_success = client.get(sale_location).get_data(as_text=True)
    assert "Sell recorded" in sale_success
    assert_no_inline_script(buy_success)


# --- Observed timing ---

def test_sell_to_replacement_observed_timing(
    client: FlaskClient, records: dict
) -> None:
    """The no-JS continuation (receipt, prefilled form, post, receipt) stays
    within a generous wall-clock budget."""
    sale_location = _sell(client, records)
    sale_id = sale_location.split("/")[2]

    started = time.perf_counter()
    success = client.get(sale_location)
    assert success.status_code == 200
    form = client.get(f"/activity/{sale_id}/replacement")
    assert form.status_code == 200
    posted = client.post(
        "/activity/new",
        data=_trade_data(
            records, quantity="100", unit_price="30", fee_amount="5",
            replacement_sale_id=sale_id,
        ),
    )
    assert posted.status_code == 302
    receipt = client.get(posted.headers["Location"])
    assert receipt.status_code == 200
    elapsed = time.perf_counter() - started

    assert "Buy recorded" in receipt.get_data(as_text=True)
    assert elapsed < 2.0


def test_inline_instrument_buy_observed_timing(
    client: FlaskClient, records: dict
) -> None:
    """Observed-timing guard for the minimal-instrument-inside-Buy flow:
    form, identity save, returned form, post-redirect, and receipt are five
    page loads and stay well inside a generous wall-clock budget locally."""
    started = time.perf_counter()
    form = client.get("/activity/new?type=buy")
    assert form.status_code == 200
    saved = client.post(
        "/activity/instruments",
        data=_identity_data(records, quantity="125", unit_price="20.5"),
    )
    assert saved.status_code == 302
    returned = client.get(saved.headers["Location"])
    assert returned.status_code == 200
    new_id = re.search(
        r'<option selected value="(\d+)">Short Bond ETF',
        returned.get_data(as_text=True),
    ).group(1)
    posted = client.post(
        "/activity/new",
        data=_trade_data(records, instrument_id=new_id, quantity="125",
                         unit_price="20.5", fee_amount=""),
    )
    assert posted.status_code == 302
    receipt = client.get(posted.headers["Location"])
    assert receipt.status_code == 200
    elapsed = time.perf_counter() - started

    assert "Buy recorded" in receipt.get_data(as_text=True)
    assert "Short Bond ETF" in receipt.get_data(as_text=True)
    assert elapsed < 2.0
