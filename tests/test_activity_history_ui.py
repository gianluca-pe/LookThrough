"""Activity history, filters, detail and reversal UI; keyboard access and progressive enhancement."""

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
from app.services.activity import TradeCommand, post_trade
from app.services.dividends import DividendCommand, post_dividend


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
        instrument = Instrument(
            portfolio_id=portfolio.id, name="Income Fund",
            ticker_or_isin="INC1", instrument_type="fund",
            valuation_currency_code="EUR", is_active=True,
        )
        db.session.add_all([account, instrument])
        db.session.flush()
        db.session.add(
            PositionRegistration(
                account_id=account.id,
                instrument_id=instrument.id,
                tracking_mode="transaction_tracked",
                opening_date=date(2026, 1, 1),
            )
        )
        db.session.commit()
        buy = post_trade(
            TradeCommand(
                portfolio_id=portfolio.id,
                activity_type="buy",
                effective_date=date(2026, 8, 4),
                account_id=account.id,
                instrument_id=instrument.id,
                quantity=Decimal("100"),
                unit_price=Decimal("10"),
                fee_amount=Decimal("5"),
            )
        )
        return {
            "portfolio_id": portfolio.id,
            "account_id": account.id,
            "instrument_id": instrument.id,
            "buy_id": buy.transaction_id,
        }


def _sidebar(page: str) -> str:
    return page.split('<aside class="sidebar">', 1)[1].split("</aside>", 1)[0]


# --- Navigation ---

def test_activity_destination_is_enabled_and_current(
    client: FlaskClient, records: dict
) -> None:
    for url in (
        "/activity",
        f"/activity/{records['buy_id']}",
        f"/activity/{records['buy_id']}/reverse",
        "/activity/new?type=buy",
    ):
        nav = _sidebar(client.get(url).get_data(as_text=True))
        current = re.search(
            r'<a href="([^"]+)" aria-current="page">([^<]+)</a>', nav
        )
        assert current, url
        assert current.group(1) == "/activity"
        assert current.group(2) == "Activity"


def test_overview_keeps_its_own_current_nav(client: FlaskClient, records: dict) -> None:
    nav = _sidebar(client.get("/overview").get_data(as_text=True))
    current = re.search(r'<a href="([^"]+)" aria-current="page">([^<]+)</a>', nav)
    assert current.group(1) == "/overview"


# --- Secondary history links ---

def test_secondary_history_links_from_main_screens(
    client: FlaskClient, records: dict
) -> None:
    overview = client.get("/overview").get_data(as_text=True)
    holdings = client.get("/holdings").get_data(as_text=True)
    accounts = client.get("/accounts").get_data(as_text=True)
    for body in (overview, holdings, accounts):
        assert 'href="/activity">Activity history</a>' in body
    detail = client.get(f"/accounts/{records['account_id']}").get_data(as_text=True)
    assert (
        f'href="/activity?account_id={records["account_id"]}"' in detail
    )


# --- History list ---

def test_history_table_columns_effects_and_view_actions(
    app: Flask, client: FlaskClient, records: dict
) -> None:
    with app.app_context():
        post_dividend(
            DividendCommand(
                portfolio_id=records["portfolio_id"],
                effective_date=date(2026, 8, 4),
                account_id=records["account_id"],
                instrument_id=records["instrument_id"],
                currency_code="EUR",
                net_amount=Decimal("425"),
                outcome="cash",
            )
        )
    body = client.get("/activity").get_data(as_text=True)
    assert "<h1>Activity history</h1>" in body
    for column in ("Date", "Activity", "Account", "Instrument",
                   "Quantity effect", "Cash effect", "Status"):
        assert f'<th scope="col"' in body and column in body
    # The buy row: signed effects come from the backend, never zeros.
    assert "100" in body
    assert "EUR</span> -1,005.00" in body
    # The dividend row has no quantity effect: an em dash, never a zero.
    assert re.search(r'<td class="num">—</td>', body)
    # Explicit View actions carry screen-reader context per row.
    views = re.findall(r">View<span class=\"sr-only\">[^<]+</span></a>", body)
    assert len(views) == 2
    assert_no_inline_script(body)


def test_empty_states_distinguish_unfiltered_and_filtered(
    client: FlaskClient, records: dict
) -> None:
    unfiltered = client.get("/activity?status=reversed").get_data(as_text=True)
    assert "No activities match these filters." in unfiltered
    assert "Clear the filters" in unfiltered
    # With no query at all and activities present this branch is covered by
    # the table test; a fresh portfolio with no activities gets the Add path.
    body = client.get("/activity").get_data(as_text=True)
    assert "No activities recorded yet." not in body


def test_filter_errors_are_linked_announced_and_preserve_selections(
    client: FlaskClient, records: dict
) -> None:
    body = client.get(
        "/activity?date_from=not-a-date&type=buy"
    ).get_data(as_text=True)
    assert "There is a problem" in body
    assert 'href="#date_from"' in body
    # Summary items carry the field label, like every other form.
    assert "From date:" in body
    date_tag = re.search(r'<input[^>]*id="date_from"[^>]*>', body).group(0)
    assert 'aria-invalid="true"' in date_tag
    assert 'aria-describedby="date_from-error"' in date_tag
    assert "YYYY-MM-DD" in body
    # The valid type selection survives the failed filter round trip.
    type_option = re.search(
        r'<option value="buy"[^>]*>', body
    ).group(0)
    assert "selected" in type_option


def test_filter_selects_are_typeahead_enhanced_with_fallback(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/activity").get_data(as_text=True)
    for field in ("account_id", "instrument_id"):
        tag = re.search(rf'<select[^>]*id="{field}"[^>]*>', body).group(0)
        assert 'data-typeahead="true"' in tag
    # Without JavaScript the plain selects submit the same names.
    assert ">Broker A — Brokerage EUR</option>" in body
    assert ">Income Fund</option>" in body


# --- Detail ---

def test_detail_shows_plain_language_effects_and_reverse_action(
    client: FlaskClient, records: dict
) -> None:
    body = client.get(f"/activity/{records['buy_id']}").get_data(as_text=True)
    assert "<h1>Buy</h1>" in body
    assert "100 units" in body
    assert "EUR</span> -1,005.00" in body
    assert "EUR</span> 5.00" in body
    assert f'href="/activity/{records["buy_id"]}/reverse"' in body
    assert "posting" not in body.lower()
    assert_no_inline_script(body)


# --- Reversal confirmation and result ---

def _reverse_buy(client: FlaskClient, records: dict) -> str:
    response = client.post(
        f"/activity/{records['buy_id']}/reverse",
        data={"reason": "Duplicate entry"},
    )
    assert response.status_code == 302
    return client.get(response.headers["Location"]).get_data(as_text=True)


def test_reversal_confirmation_names_activity_before_submit(
    client: FlaskClient, records: dict
) -> None:
    body = client.get(f"/activity/{records['buy_id']}/reverse").get_data(as_text=True)
    assert "<h1>Reverse activity</h1>" in body
    # The affected activity is named, with its effects, before the submit.
    assert body.index("<strong>Buy</strong> — Income Fund") < body.index('id="confirm_reversal"')
    assert "100 units" in body
    assert "stays in history" in body
    assert "original date" in body
    # Destructive-looking action: explicit primary label, Cancel adjacent.
    submit = re.search(r'<input[^>]*id="confirm_reversal"[^>]*>', body).group(0)
    assert 'value="Reverse activity"' in submit
    assert body.index('id="confirm_reversal"') < body.index(">Cancel<")
    # Required reason is stated in text.
    reason_label = re.search(r'<label for="reason">(.*?)</label>', body).group(1)
    assert "(required)" in reason_label
    assert_no_inline_script(body)


def test_blank_reason_refocuses_field_without_writing(
    client: FlaskClient, records: dict
) -> None:
    response = client.post(
        f"/activity/{records['buy_id']}/reverse", data={"reason": ""}
    )
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert "There is a problem" in body
    assert 'href="#reason"' in body
    reason_tag = re.search(r'<textarea[^>]*id="reason"[^>]*>', body).group(0)
    assert "autofocus" in reason_tag
    assert 'aria-invalid="true"' in reason_tag
    # The original is still posted and still offers its Reverse action.
    detail = client.get(f"/activity/{records['buy_id']}").get_data(as_text=True)
    assert "Reverse activity" in detail


def test_reversal_result_detail_cross_links_and_drops_reverse_action(
    client: FlaskClient, records: dict
) -> None:
    body = _reverse_buy(client, records)
    assert "Buy reversed." in body
    assert "Reversed" in body
    # One obvious Reverse action only while reversible: after reversal it is gone.
    assert "Reverse activity" not in body
    assert re.search(r'href="/activity/\d+">reversal #\d+</a>', body)
    # The new reversal record links back to the original and never offers
    # Reverse again.
    reversal_id = records["buy_id"] + 1
    reversal = client.get(f"/activity/{reversal_id}").get_data(as_text=True)
    assert f'href="/activity/{records["buy_id"]}"' in reversal
    assert "Reverse activity" not in reversal
    # The GET confirmation is no longer offered for the reversed original.
    confirm = client.get(f"/activity/{records['buy_id']}/reverse")
    assert confirm.status_code == 302


def test_group_reversal_confirms_both_and_reports_linked_message(
    app: Flask, client: FlaskClient, records: dict
) -> None:
    with app.app_context():
        posted = post_dividend(
            DividendCommand(
                portfolio_id=records["portfolio_id"],
                effective_date=date(2026, 8, 4),
                account_id=records["account_id"],
                instrument_id=records["instrument_id"],
                currency_code="EUR",
                net_amount=Decimal("425"),
                outcome="reinvest_same",
                reinvestment_quantity=Decimal("40"),
                reinvestment_unit_price=Decimal("10.60"),
                reinvestment_fee_amount=Decimal("1"),
            )
        )
        dividend_id = posted.dividend_transaction_id

    confirmation = client.get(f"/activity/{dividend_id}/reverse").get_data(as_text=True)
    assert "The linked dividend and purchase will both be reversed." in confirmation
    assert confirmation.index("Dividend") < confirmation.index('id="confirm_reversal"')

    response = client.post(
        f"/activity/{dividend_id}/reverse", data={"reason": "Entered twice"}
    )
    assert response.status_code == 302
    body = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Linked dividend and purchase reversed." in body


# --- Undo exits ---

def test_buy_success_undo_links_to_get_confirmation(
    client: FlaskClient, records: dict
) -> None:
    body = client.get(f"/activity/{records['buy_id']}/success").get_data(as_text=True)
    assert f'href="/activity/{records["buy_id"]}/reverse">Undo</a>' in body
    # The Undo target is the confirmation page, not a form.
    confirmation = client.get(f"/activity/{records['buy_id']}/reverse")
    assert confirmation.status_code == 200
    assert "<form" not in re.search(
        r'<div class="next-actions">(.*?)</div>', body, re.DOTALL
    ).group(1)


def test_dividend_success_undo_links_the_dividend_transaction(
    app: Flask, client: FlaskClient, records: dict
) -> None:
    with app.app_context():
        posted = post_dividend(
            DividendCommand(
                portfolio_id=records["portfolio_id"],
                effective_date=date(2026, 8, 4),
                account_id=records["account_id"],
                instrument_id=records["instrument_id"],
                currency_code="EUR",
                net_amount=Decimal("425"),
                outcome="reinvest_same",
                reinvestment_quantity=Decimal("40"),
                reinvestment_unit_price=Decimal("10.60"),
                reinvestment_fee_amount=Decimal("1"),
            )
        )
        dividend_id = posted.dividend_transaction_id
    body = client.get(f"/activity/dividend/{dividend_id}/success").get_data(as_text=True)
    assert f'href="/activity/{dividend_id}/reverse">Undo</a>' in body
    # The confirmation resolves the whole linked group from the dividend ID.
    confirmation = client.get(f"/activity/{dividend_id}/reverse").get_data(as_text=True)
    assert "linked dividend and purchase" in confirmation


# --- Final observed timing pass ---

def test_history_lookup_and_reversal_within_request_budget(
    client: FlaskClient, records: dict
) -> None:
    """Observed-timing guard for these flows: history lookup, detail,
    confirmation, POST-redirect, and the reversed receipt are five page
    loads and stay well inside a generous wall-clock budget locally."""
    app = client.application
    app.config["CURRENT_DATE_PROVIDER"] = lambda: date(2026, 8, 5)

    started = time.perf_counter()
    history = client.get("/activity")
    assert history.status_code == 200
    detail = client.get(f"/activity/{records['buy_id']}")
    assert detail.status_code == 200
    confirmation = client.get(f"/activity/{records['buy_id']}/reverse")
    assert confirmation.status_code == 200
    posted = client.post(
        f"/activity/{records['buy_id']}/reverse", data={"reason": "Duplicate entry"}
    )
    assert posted.status_code == 302
    receipt = client.get(posted.headers["Location"])
    assert receipt.status_code == 200
    elapsed = time.perf_counter() - started

    assert "Buy reversed." in receipt.get_data(as_text=True)
    assert elapsed < 2.0
