"""Buy/Sell UI: chooser, keyboard flow, preview, receipt, enhancement and request budget."""

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
from app.models import Account, Institution, Instrument, Portfolio, Posting
from sqlalchemy import select


def _make_account(portfolio_id: int, institution_id: int, **overrides) -> Account:
    values = {
        "portfolio_id": portfolio_id,
        "institution_id": institution_id,
        "name": "Brokerage EUR",
        "account_type": "brokerage",
        "default_currency_code": "EUR",
        "is_multicurrency": False,
        "cash_tracking_mode": "separate_cash",
        "portfolio_share_decimal": Decimal("1"),
        "present_access_decimal": Decimal("1"),
        "relationship_eligible": True,
        "is_active": True,
    }
    values.update(overrides)
    return Account(**values)


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
        account = _make_account(portfolio.id, institution.id)
        instrument = Instrument(
            portfolio_id=portfolio.id, name="Global Fund",
            ticker_or_isin="FUND123", instrument_type="fund",
            valuation_currency_code="EUR", is_active=True,
        )
        db.session.add_all([account, instrument])
        db.session.commit()
        return {
            "portfolio_id": portfolio.id,
            "institution_id": institution.id,
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


# --- Type chooser ---

def test_chooser_offers_large_choices(client: FlaskClient, records: dict) -> None:
    body = client.get("/activity/new").get_data(as_text=True)
    assert "<h1>Add activity</h1>" in body
    choices = body.split('class="activity-choices"', 1)[1]
    # Buy and Sell are real links; unavailable types are disabled labels.
    assert 'class="activity-choice" href="/activity/new?type=buy"' in choices
    assert 'class="activity-choice" href="/activity/new?type=sell"' in choices
    disabled = re.findall(r'<span class="activity-choice" aria-disabled="true">', choices)
    assert len(disabled) == 2
    assert "Transfer / FX" in choices and "More…" in choices
    assert 'href="/activity/new?type=transfer"' not in choices
    # The chooser does not render the trade form.
    assert 'id="quantity"' not in body
    assert_no_inline_script(body)


def test_switcher_marks_current_type(client: FlaskClient, records: dict) -> None:
    body = client.get("/activity/new?type=sell").get_data(as_text=True)
    switcher = body.split('aria-label="Activity type"', 1)[1].split("</nav>", 1)[0]
    assert "<strong aria-current=\"page\">Sell</strong>" in switcher
    assert 'href="/activity/new?type=buy"' in switcher


# --- Compact form keyboard contract ---

def test_form_fields_in_entry_order_with_primary_action_first(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/activity/new?type=buy").get_data(as_text=True)
    positions = [
        body.index(f'id="{field}"')
        for field in (
            "effective_date", "account_id", "instrument_id",
            "quantity", "unit_price", "fee_amount",
        )
    ]
    assert positions == sorted(positions)
    # Tab order: Record (primary) comes before Preview and Cancel; Enter in
    # the form submits the first submit control, which is Record.
    assert body.index('id="post_trade"') < body.index('id="preview_trade"')
    assert body.index('id="preview_trade"') < body.index(">Cancel<")


def test_first_empty_required_field_is_focused_on_load(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/activity/new?type=buy").get_data(as_text=True)
    # Date defaults to today, so Account is the first empty required field.
    assert body.count("autofocus") == 1
    account_tag = re.search(r'<select[^>]*id="account_id"[^>]*>', body).group(0)
    assert "autofocus" in account_tag


def test_labels_state_required_and_fee_is_optional(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/activity/new?type=buy").get_data(as_text=True)
    for field in ("effective_date", "account_id", "instrument_id", "quantity", "unit_price"):
        label = re.search(rf'<label for="{field}">(.*?)</label>', body).group(1)
        assert "(required)" in label
    fee_label = re.search(r'<label for="fee_amount">(.*?)</label>', body).group(1)
    assert "(required)" not in fee_label
    assert "leave blank for no fee" in body


def test_pickers_are_typeahead_enhanced_with_select_fallback(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/activity/new?type=buy").get_data(as_text=True)
    for field in ("account_id", "instrument_id"):
        tag = re.search(rf'<select[^>]*id="{field}"[^>]*>', body).group(0)
        assert 'data-typeahead="true"' in tag
    # Without JavaScript the plain selects submit the same ids.
    assert ">Broker A — Brokerage EUR</option>" in body
    assert "Global Fund (FUND123)" in body


# --- Server-rendered preview presentation ---

def test_preview_sentence_and_currency_carrying_labels(
    client: FlaskClient, records: dict
) -> None:
    body = client.post(
        "/activity/preview", data=_trade_data(records)
    ).get_data(as_text=True)

    region = re.search(r'<div id="preview-region"[^>]*>', body).group(0)
    assert 'role="status"' in region and 'aria-live="polite"' in region
    assert "Buys" in body
    assert "<strong>1,000 units</strong>" in body
    assert '<span class="ccy">EUR</span> 10,250.00' in body
    assert '<span class="ccy">EUR</span> 25.00' in body
    assert '<span class="ccy">EUR</span> -10,275.00' in body
    assert "Quantity after: <strong>1,000 units</strong>" in body
    # A first buy in the account discloses that it opens a position.
    assert "opens a new transaction-tracked position" in body
    # Once the server resolves the settlement currency, labels state it.
    assert "Price per unit, EUR" in body
    assert "Fee, EUR" in body
    assert_no_inline_script(body)


def test_sell_preview_shows_positive_cash_effect(
    client: FlaskClient, records: dict
) -> None:
    client.post("/activity/new", data=_trade_data(records))
    body = client.post(
        "/activity/preview",
        data=_trade_data(
            records, activity_type="sell", quantity="300",
            unit_price="11.10", fee_amount="20",
        ),
    ).get_data(as_text=True)
    assert "Sells" in body
    assert '<span class="ccy">EUR</span> 3,310.00' in body
    assert "Quantity after: <strong>700 units</strong>" in body
    assert "opens a new transaction-tracked position" not in body


def test_included_in_aggregate_warning_renders_as_amber_banner(
    app: Flask, client: FlaskClient, records: dict
) -> None:
    with app.app_context():
        account = _make_account(
            records["portfolio_id"], records["institution_id"],
            name="Managed EUR", cash_tracking_mode="included_in_aggregate",
        )
        db.session.add(account)
        db.session.commit()
        account_id = account.id

    body = client.post(
        "/activity/preview", data=_trade_data(records, account_id=account_id)
    ).get_data(as_text=True)
    warning = re.search(r'<p class="banner-warning">(.*?)</p>', body)
    assert warning and "not be valued as a separate cash balance" in warning.group(1)


def test_validation_error_summary_links_and_focuses_first_field(
    client: FlaskClient, records: dict
) -> None:
    body = client.post(
        "/activity/new", data=_trade_data(records, quantity="0")
    ).get_data(as_text=True)
    assert "There is a problem" in body
    assert 'href="#quantity"' in body
    quantity_tag = re.search(r'<input[^>]*id="quantity"[^>]*>', body).group(0)
    assert "autofocus" in quantity_tag
    assert 'aria-invalid="true"' in quantity_tag
    # Nothing was recorded.
    assert client.get("/activity/1/success").status_code == 404


# --- Success receipt ---

def _post_buy(client: FlaskClient, records: dict) -> str:
    response = client.post("/activity/new", data=_trade_data(records))
    assert response.status_code == 302
    return client.get(response.headers["Location"]).get_data(as_text=True)


def test_success_panel_receipt_and_actions(
    client: FlaskClient, records: dict
) -> None:
    body = _post_buy(client, records)
    panel = re.search(r'<div class="success-panel"[^>]*>', body).group(0)
    assert 'tabindex="-1"' in panel
    assert "<h1>Buy recorded</h1>" in body
    assert "Bought" in body
    assert "<strong>1,000 units</strong>" in body
    assert '<span class="ccy">EUR</span> 10,250.00' in body
    assert '<span class="ccy">EUR</span> 25.00' in body
    assert '<span class="ccy">EUR</span> -10,275.00' in body
    assert "4 Aug 2026" in body
    # Delivered exits: Add another, View Holdings, and Undo — the last links
    # to the GET reversal confirmation, never a direct POST.
    assert 'href="/activity/new?type=buy">Add another' in body
    assert 'href="/holdings">View Holdings' in body
    assert 'href="/activity/1/reverse">Undo</a>' in body
    assert "proceeds" not in body
    assert_no_inline_script(body)


def test_sell_success_names_the_sale(client: FlaskClient, records: dict) -> None:
    client.post("/activity/new", data=_trade_data(records))
    response = client.post(
        "/activity/new",
        data=_trade_data(
            records, activity_type="sell", quantity="300",
            unit_price="11.10", fee_amount="20",
        ),
    )
    body = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "<h1>Sell recorded</h1>" in body
    assert "Sold" in body
    assert 'href="/activity/new?type=sell">Add another' in body


# --- Progressive enhancement ---

def test_enhancement_hooks_present_and_server_side_fallbacks_intact(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/activity/new?type=buy").get_data(as_text=True)
    form_tag = re.search(r"<form[^>]*>", body).group(0)
    assert 'data-live-preview="true"' in form_tag
    assert 'id="preview-region"' in body

    script = client.get("/static/js/enhance.js").get_data(as_text=True)
    assert "data-live-preview" in script
    assert "success-panel" in script
    # The no-JS path never depends on the enhancement: a real Preview submit
    # button posts to the preview route.
    preview_tag = re.search(r'<input[^>]*id="preview_trade"[^>]*>', body).group(0)
    assert 'formaction="/activity/preview"' in preview_tag


def test_typeahead_guards_identifiers_and_accepts_typed_text(
    client: FlaskClient, records: dict
) -> None:
    script = client.get("/static/js/enhance.js").get_data(as_text=True)
    # Identifiers are not prose: no browser autocorrect on typeahead inputs.
    assert 'autocorrect", "off"' in script
    assert 'autocapitalize", "none"' in script
    assert 'spellcheck", "false"' in script
    # A typed ticker resolves by unique substring match, no mouse required.
    assert "function resolve(" in script
    assert "indexOf(query)" in script
    # Focus selects the prefilled first option so typing replaces it.
    assert "input.select()" in script


# --- Observed timing ---

def test_compact_buy_completes_within_request_budget(
    client: FlaskClient, records: dict
) -> None:
    """Observed-timing guard for the speed-critical flow: the complete
    no-JS path is exactly three page loads (form, post-redirect, receipt)
    and stays well inside a generous wall-clock budget locally."""
    app = client.application
    app.config["CURRENT_DATE_PROVIDER"] = lambda: date(2026, 8, 4)

    started = time.perf_counter()
    form = client.get("/activity/new?type=buy")
    assert form.status_code == 200
    posted = client.post("/activity/new", data=_trade_data(records))
    assert posted.status_code == 302
    receipt = client.get(posted.headers["Location"])
    assert receipt.status_code == 200
    elapsed = time.perf_counter() - started

    assert "Buy recorded" in receipt.get_data(as_text=True)
    assert elapsed < 2.0


# --- Sell the entire holding  ---

def test_sell_offers_quantity_choices_buy_does_not(
    client: FlaskClient, records: dict
) -> None:
    sell = client.get("/activity/new?type=sell").get_data(as_text=True)
    assert "Sell the entire holding" in sell
    entered = re.search(
        r'<input type="radio" name="quantity_mode" value="entered"[^>]*>', sell, re.S
    ).group(0)
    assert 'id="quantity_mode"' in entered and "checked" in entered
    assert re.search(
        r'<input type="radio" name="quantity_mode" value="entire_holding"[^>]*>', sell, re.S
    )
    # The choice precedes the quantity input, whose requirement is stated in
    # text only — no HTML required attribute blocks a blank entire-holding sale.
    assert sell.index('id="quantity_mode"') < sell.index('id="quantity"')
    quantity_tag = re.search(r'<input[^>]*id="quantity"[^>]*>', sell).group(0)
    assert "required" not in quantity_tag
    label = re.search(r'<label for="quantity">(.*?)</label>', sell).group(1)
    assert "required for “Enter quantity”" in label
    assert_no_inline_script(sell)

    buy = client.get("/activity/new?type=buy").get_data(as_text=True)
    assert "Sell the entire holding" not in buy
    assert 'type="radio" name="quantity_mode"' not in buy.replace(
        'name="quantity_mode" type="radio"', "")
    assert '<input type="hidden" name="quantity_mode" value="entered">' in buy
    # The Buy label states the quantity requirement; the server validates it.
    label = re.search(r'<label for="quantity">(.*?)</label>', buy).group(1)
    assert "(required)" in label


def test_entire_holding_preview_resolves_exact_quantity(
    client: FlaskClient, records: dict
) -> None:
    client.post("/activity/new", data=_trade_data(records))  # buys 1,000
    body = client.post("/activity/preview", data=_trade_data(
        records, activity_type="sell", quantity_mode="entire_holding", quantity="",
    )).get_data(as_text=True)
    assert re.search(
        r"Entire holding: sells all\s*<strong>1,000 units</strong>", body
    )
    assert "Quantity after: <strong>0 units</strong>" in body
    # The choice survives the preview round trip.
    assert re.search(
        r'<input type="radio" name="quantity_mode" value="entire_holding"[^>]*checked',
        body, re.S,
    )


def test_entire_holding_preview_to_record_without_javascript(
    client: FlaskClient, records: dict, app: Flask
) -> None:
    client.post("/activity/new", data=_trade_data(records))
    posted = client.post("/activity/new", data=_trade_data(
        records, activity_type="sell", quantity_mode="entire_holding",
        quantity="", effective_date="2026-08-05",
    ))
    assert posted.status_code == 302, posted.get_data(as_text=True)
    receipt = client.get(posted.headers["Location"]).get_data(as_text=True)
    # The receipt names the concrete resolved quantity like any other sale.
    assert re.search(r"Sold\s*<strong>1,000 units</strong>", receipt)
    with app.app_context():
        posting = db.session.scalar(
            select(Posting).where(Posting.posting_kind == "instrument")
            .order_by(Posting.id.desc())
        )
        assert posting is not None
        assert posting.quantity_delta == Decimal("-1000")


def test_entered_quantity_still_required_for_sell(
    client: FlaskClient, records: dict
) -> None:
    client.post("/activity/new", data=_trade_data(records))
    body = client.post("/activity/preview", data=_trade_data(
        records, activity_type="sell", quantity_mode="entered", quantity="",
    )).get_data(as_text=True)
    assert "Quantity is required." in body
    assert 'href="#quantity"' in body
    quantity_tag = re.search(r'<input[^>]*id="quantity"[^>]*>', body).group(0)
    assert "autofocus" in quantity_tag


def test_entire_holding_errors_focus_the_choice_control(
    client: FlaskClient, records: dict
) -> None:
    client.post("/activity/new", data=_trade_data(records))
    client.post("/activity/new", data=_trade_data(
        records, activity_type="sell", quantity="1000", effective_date="2026-08-05",
    ))
    # Nothing left on the sale date: the error belongs to the choice control.
    body = client.post("/activity/preview", data=_trade_data(
        records, activity_type="sell", quantity_mode="entire_holding",
        quantity="", effective_date="2026-08-06",
    )).get_data(as_text=True)
    assert "No units are available to sell" in body
    assert 'href="#quantity_mode"' in body
    radio = re.search(
        r'<input type="radio" name="quantity_mode"[^>]*id="quantity_mode"[^>]*>',
        body, re.S,
    ).group(0)
    assert "autofocus" in radio and 'aria-invalid="true"' in radio

    # An unsupported submitted value is rejected at the same control.
    body = client.post("/activity/preview", data=_trade_data(
        records, activity_type="sell", quantity_mode="bogus", quantity="1",
    )).get_data(as_text=True)
    assert 'href="#quantity_mode"' in body
    radio = re.search(
        r'<input type="radio" name="quantity_mode"[^>]*id="quantity_mode"[^>]*>',
        body, re.S,
    ).group(0)
    assert "autofocus" in radio
    assert_no_inline_script(body)


# --- Picker contract by flow ---

def test_trade_picker_excludes_statement_only_instrument_types(
    client: FlaskClient, app: Flask, records: dict
) -> None:
    """Buy/Sell post quantities, so fixed deposits and cash never appear in
    the instrument picker — whether open or closed. Quantity-tracked former
    instruments stay available for rebuys."""
    with app.app_context():
        db.session.add(Instrument(
            portfolio_id=records["portfolio_id"], name="FD_USD_204",
            instrument_type="fixed_deposit", valuation_currency_code="EUR",
            is_active=True,
        ))
        db.session.commit()
    for activity_type in ("buy", "sell"):
        body = client.get(f"/activity/new?type={activity_type}").get_data(as_text=True)
        picker = re.search(
            r'<select[^>]*id="instrument_id".*?</select>', body, re.S
        ).group(0)
        assert "Global Fund (FUND123)" in picker
        assert "FD_USD_204" not in picker
        assert_no_inline_script(body)


# --- Contextual Cancel (return_to, whitelisted) ---

def test_cancel_honors_whitelisted_return_to(
    client: FlaskClient, records: dict
) -> None:
    body = client.get(
        "/activity/new?type=buy&return_to=activity_history"
    ).get_data(as_text=True)
    assert 'href="/activity">Cancel</a>' in body
    # The context survives a failed POST: the form action carries it.
    assert 'action="/activity/new?return_to=activity_history"' in body
    failed = client.post(
        "/activity/new?return_to=activity_history",
        data=_trade_data(records, quantity=""),
    ).get_data(as_text=True)
    assert "There is a problem" in failed
    assert 'href="/activity">Cancel</a>' in failed


def test_cancel_defaults_to_overview_and_rejects_unknown_targets(
    client: FlaskClient, records: dict
) -> None:
    for url in (
        "/activity/new?type=buy",
        "/activity/new?type=buy&return_to=https://evil.example",
    ):
        body = client.get(url).get_data(as_text=True)
        assert 'href="/overview">Cancel</a>' in body


def test_chooser_forwards_return_context(client: FlaskClient, records: dict) -> None:
    body = client.get("/activity/new?return_to=activity_history").get_data(as_text=True)
    assert 'href="/activity/new?type=buy&amp;return_to=activity_history"' in body
    assert 'href="/activity/new?type=sell&amp;return_to=activity_history"' in body
    assert 'href="/activity/dividend/new?return_to=activity_history"' in body


# --- Typeahead accessibility contract ---

def test_typeahead_field_keeps_the_id_and_aria_contract(
    client: FlaskClient, records: dict
) -> None:
    """The server-rendered select carries id + aria wiring; enhance.js moves
    that whole contract onto the visible typeahead input (the select becomes
    #instrument_id-native and keeps only the submitted name)."""
    body = client.post(
        "/activity/new", data=_trade_data(records, instrument_id="")
    ).get_data(as_text=True)
    # The error summary anchors to the field id…
    assert 'href="#instrument_id"' in body
    select = re.search(r'<select[^>]*id="instrument_id"[^>]*>', body).group(0)
    # …which resolves to a focusable element after enhancement because the
    # visible input takes the id over.
    assert 'aria-invalid="true"' in select
    assert re.search(r'aria-describedby="[^"]*instrument_id-error[^"]*"', select)
    assert 'data-typeahead="true"' in select
    assert 'id="instrument_id-error"' in body

    script = client.get("/static/js/enhance.js").get_data(as_text=True)
    assert 'input.id = fid' in script
    assert 'select.id = fid + "-native"' in script
    assert 'getAttribute("aria-invalid")' in script
    assert 'getAttribute("aria-describedby")' in script
