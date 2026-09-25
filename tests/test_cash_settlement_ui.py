"""Cash-settlement UI: linked-account selection, custody/destination naming, previews and receipts."""

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
from app.models import (
    Account,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    Posting,
    Transaction,
)


@pytest.fixture
def linked(app: Flask) -> dict:
    """Brokerage custody account whose activity cash settles in a linked Settlement
    cash account at the same institution, plus an aggregate wrapper account."""
    app.config["CURRENT_DATE_PROVIDER"] = lambda: date(2026, 8, 4)
    with app.app_context():
        portfolio = Portfolio(
            name="Personal portfolio", reporting_currency_code="EUR",
            annual_spending_amount=Decimal("48000"),
        )
        db.session.add(portfolio)
        db.session.flush()
        institution = Institution(portfolio_id=portfolio.id, name="Example Bank")
        db.session.add(institution)
        db.session.flush()

        def account(name: str, account_type: str, mode: str) -> Account:
            row = Account(
                portfolio_id=portfolio.id, institution_id=institution.id,
                name=name, account_type=account_type,
                default_currency_code="EUR", is_multicurrency=False,
                cash_tracking_mode=mode,
                portfolio_share_decimal=Decimal("1"), present_access_decimal=Decimal("1"),
                relationship_eligible=True, is_active=True,
            )
            db.session.add(row)
            db.session.flush()
            return row

        brokerage = account("Brokerage SGD", "brokerage", "separate_cash")
        wma = account("Settlement SGD", "cash", "separate_cash")
        aggregate = account("Pension wrapper", "retirement", "included_in_aggregate")
        brokerage.cash_settlement_account_id = wma.id

        instrument = Instrument(
            portfolio_id=portfolio.id, name="Global Fund",
            instrument_type="fund", valuation_currency_code="EUR", is_active=True,
        )
        db.session.add(instrument)
        db.session.flush()
        registration = PositionRegistration(
            account_id=brokerage.id, instrument_id=instrument.id,
            tracking_mode="transaction_tracked", opening_date=date(2026, 7, 1),
        )
        db.session.add(registration)
        db.session.flush()
        transaction = Transaction(
            portfolio_id=portfolio.id, transaction_type="opening_balance",
            effective_date=date(2026, 7, 1), status="posted",
        )
        db.session.add(transaction)
        db.session.flush()
        db.session.add(Posting(
            transaction_id=transaction.id, account_id=brokerage.id,
            posting_kind="instrument", instrument_id=instrument.id,
            currency_code="EUR", quantity_delta=Decimal("1000"),
        ))
        db.session.commit()
        return {
            "brokerage_id": brokerage.id,
            "wma_id": wma.id,
            "aggregate_id": aggregate.id,
            "instrument_id": instrument.id,
        }


def _trade_data(linked: dict, **overrides) -> dict:
    data = {
        "activity_type": "buy",
        "effective_date": "2026-08-04",
        "account_id": linked["brokerage_id"],
        "instrument_id": linked["instrument_id"],
        "quantity": "100",
        "unit_price": "10",
        "fee_amount": "0",
    }
    data.update(overrides)
    return data


def _dividend_data(linked: dict, **overrides) -> dict:
    data = {
        "effective_date": "2026-08-04",
        "account_id": linked["brokerage_id"],
        "instrument_id": linked["instrument_id"],
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


# --- Account edit settlement select ---

def test_edit_form_prefills_settlement_and_round_trips(
    client: FlaskClient, linked: dict
) -> None:
    body = client.get(f"/accounts/{linked['brokerage_id']}/edit").get_data(as_text=True)
    # The linked destination is the selected choice; the copy scopes the
    # change to future activity cash only.
    assert re.search(
        rf'<option selected value="{linked["wma_id"]}">Settlement SGD \(EUR\)</option>', body
    )
    assert "future Buy/Sell/Dividend activity" in body
    assert "past activities stay where they are" in body

    # Unlink via the explicit zero choice, then link again.
    unlinked = client.post(f"/accounts/{linked['brokerage_id']}/edit", data={
        "name": "Brokerage SGD", "cash_tracking_mode": "separate_cash",
        "cash_settlement_account_id": "0", "save_account": "Save account",
    })
    assert unlinked.status_code == 302, unlinked.get_data(as_text=True)
    detail = client.get(unlinked.headers["Location"]).get_data(as_text=True)
    assert "does not add a separate cash balance" not in detail

    relinked = client.post(f"/accounts/{linked['brokerage_id']}/edit", data={
        "name": "Brokerage SGD", "cash_tracking_mode": "separate_cash",
        "cash_settlement_account_id": str(linked["wma_id"]),
        "save_account": "Save account",
    })
    assert relinked.status_code == 302, relinked.get_data(as_text=True)
    detail = client.get(relinked.headers["Location"]).get_data(as_text=True)
    assert "Activity cash settles in" in detail
    assert_no_inline_script(body)


def test_edit_rejects_ineligible_destination_with_linked_focus(
    client: FlaskClient, linked: dict
) -> None:
    body = client.post(f"/accounts/{linked['brokerage_id']}/edit", data={
        "name": "Brokerage SGD", "cash_tracking_mode": "separate_cash",
        "cash_settlement_account_id": str(linked["aggregate_id"]),
        "save_account": "Save account",
    }).get_data(as_text=True)
    assert "There is a problem" in body
    assert 'href="#cash_settlement_account_id"' in body
    select_tag = re.search(
        r'<select[^>]*id="cash_settlement_account_id"[^>]*>', body
    ).group(0)
    assert 'aria-invalid="true"' in select_tag
    assert "autofocus" in select_tag
    assert_no_inline_script(body)


def test_receiving_account_must_keep_separate_cash(
    client: FlaskClient, linked: dict
) -> None:
    body = client.post(f"/accounts/{linked['wma_id']}/edit", data={
        "name": "Settlement SGD", "cash_tracking_mode": "included_in_aggregate",
        "save_account": "Save account",
    }).get_data(as_text=True)
    assert "must keep separate cash tracking" in body
    assert 'href="#cash_tracking_mode"' in body
    select_tag = re.search(r'<select[^>]*id="cash_tracking_mode"[^>]*>', body).group(0)
    assert "autofocus" in select_tag


# --- settled_elsewhere display state ---

def test_index_names_destination_and_suppresses_confirmation(
    client: FlaskClient, linked: dict
) -> None:
    body = client.get("/accounts").get_data(as_text=True)
    row = body.split("Brokerage SGD", 1)[1].split("</tr>", 1)[0]
    assert "activity cash settles in" in row
    assert f'href="/accounts/{linked["wma_id"]}">Settlement SGD</a>' in row
    # Never aggregate, never zero, and no confirmation action while linked.
    assert "included in aggregate" not in row
    assert "0.00" not in row
    assert "Set cash balance" not in row
    # The receiving account keeps its confirmation action.
    assert body.count("Set cash balance") == 1
    assert_no_inline_script(body)


def test_detail_renders_settled_elsewhere_distinctly(
    client: FlaskClient, linked: dict
) -> None:
    body = client.get(f"/accounts/{linked['brokerage_id']}").get_data(as_text=True)
    assert "Settles in Settlement SGD" in body
    assert "does not add a separate cash balance" in body
    assert "Set current cash balance" not in body
    assert "included in aggregate" not in body.lower() or "Included in aggregate" not in body
    assert "0.00" not in body
    assert_no_inline_script(body)



# --- Activity preview and receipt naming ---

def test_trade_preview_and_receipt_name_cash_destination(
    client: FlaskClient, linked: dict
) -> None:
    preview = client.post("/activity/preview", data=_trade_data(linked))
    body = preview.get_data(as_text=True)
    assert "settles in <strong>Settlement SGD</strong>" in body

    posted = client.post("/activity/new", data=_trade_data(linked))
    assert posted.status_code == 302, posted.get_data(as_text=True)
    receipt = client.get(posted.headers["Location"]).get_data(as_text=True)
    assert "settles in <strong>Settlement SGD</strong>" in receipt

    # Same-account activity keeps the existing compact presentation.
    same = client.post("/activity/new", data=_trade_data(
        linked, account_id=linked["wma_id"], effective_date="2026-08-03",
    ))
    assert same.status_code == 302, same.get_data(as_text=True)
    receipt = client.get(same.headers["Location"]).get_data(as_text=True)
    assert "settles in" not in receipt
    assert_no_inline_script(body)


def test_dividend_preview_and_receipt_name_cash_destination(
    client: FlaskClient, linked: dict
) -> None:
    preview = client.post("/activity/dividend/preview", data=_dividend_data(linked))
    body = preview.get_data(as_text=True)
    assert "settles in <strong>Settlement SGD</strong>" in body

    posted = client.post("/activity/dividend", data=_dividend_data(linked))
    assert posted.status_code == 302, posted.get_data(as_text=True)
    receipt = client.get(posted.headers["Location"]).get_data(as_text=True)
    assert "settles in <strong>Settlement SGD</strong>" in receipt


def test_entire_holding_sale_keeps_settlement_copy(
    client: FlaskClient, linked: dict
) -> None:
    """An entire-holding sale from the routed Brokerage names the resolution
    and the Settlement cash destination."""
    body = client.post("/activity/preview", data=_trade_data(
        linked, activity_type="sell", quantity_mode="entire_holding", quantity="",
    )).get_data(as_text=True)
    assert re.search(
        r"Entire holding: sells all\s*<strong>1,000 units</strong>", body
    )
    assert "settles in <strong>Settlement SGD</strong>" in body
