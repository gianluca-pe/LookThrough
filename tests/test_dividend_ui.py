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

def test_chooser_links_dividend_with_accumulating_hint(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/activity/new").get_data(as_text=True)
    choices = body.split('class="activity-choices"', 1)[1]
    assert 'class="activity-choice" href="/activity/dividend/new"' in choices
    # The accumulating-share guidance is a muted hint on the chooser.
    assert "Accumulating fund? Nothing to record" in choices
    assert "already in the NAV" in choices


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


def test_dividend_switcher_marks_current_type(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/activity/dividend/new").get_data(as_text=True)
    switcher = body.split('aria-label="Activity type"', 1)[1].split("</nav>", 1)[0]
    assert '<strong aria-current="page">Dividend</strong>' in switcher
    assert 'href="/activity/new?type=buy"' in switcher
    assert 'href="/activity/new?type=sell"' in switcher


# --- Form keyboard contract ---

def test_form_fields_in_entry_order_with_primary_action_first(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/activity/dividend/new").get_data(as_text=True)
    positions = [
        body.index(f'id="{field}"')
        for field in (
            "effective_date", "account_id", "instrument_id",
            "currency_code", "net_amount", "outcome",
        )
    ]
    assert positions == sorted(positions)
    # Tab order: Record (primary) comes before Preview and Cancel; Enter in
    # the form submits the first submit control, which is Record.
    assert body.index('id="post_dividend"') < body.index('id="preview_dividend"')
    assert body.index('id="preview_dividend"') < body.index(">Cancel<")


def test_first_empty_required_field_is_focused_on_load(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/activity/dividend/new").get_data(as_text=True)
    # Date defaults to today, so Account is the first empty required field.
    assert body.count("autofocus") == 1
    account_tag = re.search(r'<select[^>]*id="account_id"[^>]*>', body).group(0)
    assert "autofocus" in account_tag


def test_pickers_are_typeahead_enhanced_with_select_fallback(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/activity/dividend/new").get_data(as_text=True)
    for field in ("account_id", "instrument_id", "reinvestment_instrument_id"):
        tag = re.search(rf'<select[^>]*id="{field}"[^>]*>', body).group(0)
        assert 'data-typeahead="true"' in tag
    # Without JavaScript the plain selects submit the same ids.
    assert ">Broker A — Brokerage EUR</option>" in body
    assert "Income Fund (INC1)" in body
    assert "Bond ETF (BOND1)" in body


def test_currency_field_suggests_known_codes_without_restricting(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/activity/dividend/new").get_data(as_text=True)
    currency_tag = re.search(r'<input[^>]*id="currency_code"[^>]*>', body).group(0)
    assert 'list="currency-codes"' in currency_tag
    datalist = re.search(
        r'<datalist id="currency-codes">(.*?)</datalist>', body, re.DOTALL
    ).group(1)
    # Codes in use (account default + instrument valuation) are suggested.
    assert '<option value="EUR">' in datalist


# --- Outcome radio cards ---

def test_outcome_renders_as_radio_cards_not_a_select(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/activity/dividend/new").get_data(as_text=True)
    group = re.search(
        r'<div class="choice-row choice-row-3"', body
    )
    assert group
    radios = re.findall(
        r'<input type="radio" name="outcome" value="([^"]+)"', body
    )
    assert radios == ["cash", "reinvest_same", "reinvest_other"]
    assert "<select" not in re.search(
        r'class="choice-row choice-row-3"(.*?)</fieldset>', body, re.DOTALL
    ).group(1)
    # The fieldset legend names the group; no redundant radiogroup role.
    assert 'role="radiogroup"' not in body
    # The error summary anchor resolves: the first radio carries id="outcome".
    assert 'id="outcome"' in body
    assert 'id="outcome-reinvest_same"' in body
    assert 'id="outcome-reinvest_other"' in body
    # Added to cash is the default checked card.
    cash_tag = re.search(
        r'<input type="radio" name="outcome" value="cash"[^>]*>', body
    ).group(0)
    assert "checked" in cash_tag
    # Each card explains the outcome in product language.
    assert "stays as cash in the account" in body
    assert "more units of the same instrument" in body
    assert "units of a different instrument" in body


def test_submitted_reinvestment_outcome_keeps_its_card_checked(
    client: FlaskClient, records: dict
) -> None:
    body = client.post(
        "/activity/dividend/preview",
        data=_dividend_data(
            records, outcome="reinvest_same", reinvestment_quantity="40",
            reinvestment_unit_price="10.60", reinvestment_fee_amount="1",
        ),
    ).get_data(as_text=True)
    card = re.search(
        r'<input type="radio" name="outcome" value="reinvest_same"[^>]*>', body
    ).group(0)
    assert "checked" in card


# --- Disclosures ---

def test_disclosures_are_native_and_closed_on_fresh_load(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/activity/dividend/new").get_data(as_text=True)
    disclosures = re.findall(r'<details class="disclosure"[^>]*>', body)
    assert len(disclosures) == 2
    assert all("open" not in tag for tag in disclosures)
    assert any("data-reinvestment-details" in tag for tag in disclosures)
    # Native disclosures stay fully reachable without JavaScript.
    assert "<summary>Optional distribution details</summary>" in body
    assert "<summary>Reinvestment details</summary>" in body


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


def test_cash_success_panel_receipt_and_actions(
    client: FlaskClient, records: dict
) -> None:
    body = _post_dividend(client, records)
    panel = re.search(r'<div class="success-panel"[^>]*>', body).group(0)
    assert 'tabindex="-1"' in panel
    assert "<h1>Dividend recorded</h1>" in body
    assert "Income Fund" in body
    assert '<span class="ccy">EUR</span> 500.00' in body
    assert '<span class="ccy">EUR</span> 75.00' in body
    assert '<span class="ccy">EUR</span> 425.00' in body
    assert "4 Aug 2026" in body
    assert "Linked Buy" not in body
    assert 'href="/activity/dividend/new">Add another dividend' in body
    assert 'href="/holdings">View Holdings' in body
    # Undo links the dividend transaction to the GET reversal confirmation.
    assert 'href="/activity/1/reverse">Undo</a>' in body
    assert_no_inline_script(body)


def test_reinvest_success_shows_linked_buy(
    client: FlaskClient, records: dict
) -> None:
    body = _post_dividend(
        client, records, outcome="reinvest_other",
        reinvestment_instrument_id=str(records["target_id"]),
        reinvestment_quantity="3", reinvestment_purchase_amount="100",
        gross_amount="", withholding_amount="",
    )
    assert "Linked Buy" in body
    assert "Bond ETF" in body
    assert "3 units" in body
    assert '<span class="ccy">EUR</span> 100.00' in body
    assert '<span class="ccy">EUR</span> 325.00' in body


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

def test_enhancement_hooks_present_and_server_side_fallbacks_intact(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/activity/dividend/new").get_data(as_text=True)
    form_tag = re.search(r"<form[^>]*>", body).group(0)
    assert 'data-live-preview="true"' in form_tag
    assert 'id="preview-region"' in body
    assert "data-reinvestment-details" in body

    script = client.get("/static/js/enhance.js").get_data(as_text=True)
    assert "data-live-preview" in script
    assert "data-reinvestment-details" in script
    assert "success-panel" in script
    # The no-JS path never depends on the enhancement: a real Preview submit
    # button posts to the preview route.
    preview_tag = re.search(
        r'<input[^>]*id="preview_dividend"[^>]*>', body
    ).group(0)
    assert 'formaction="/activity/dividend/preview"' in preview_tag


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
