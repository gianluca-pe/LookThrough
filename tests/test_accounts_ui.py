"""Account maintenance, cash-confirmation flows, accessible validation and request budget."""

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
    ValuationObservation,
)


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
        separate = Account(
            portfolio_id=portfolio.id, institution_id=institution.id,
            name="Brokerage EUR", account_type="brokerage",
            default_currency_code="EUR", is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"), present_access_decimal=Decimal("1"),
            relationship_eligible=True, is_active=True,
        )
        included = Account(
            portfolio_id=portfolio.id, institution_id=institution.id,
            name="Pension wrapper", account_type="retirement",
            default_currency_code="EUR", is_multicurrency=False,
            cash_tracking_mode="included_in_aggregate",
            portfolio_share_decimal=Decimal("1"), present_access_decimal=Decimal("0"),
            relationship_eligible=False, is_active=True,
        )
        instrument = Instrument(
            portfolio_id=portfolio.id, name="Global Fund",
            instrument_type="fund", valuation_currency_code="EUR", is_active=True,
        )
        db.session.add_all([separate, included, instrument])
        db.session.commit()
        return {
            "account_id": separate.id,
            "included_id": included.id,
            "instrument_id": instrument.id,
        }


def _trade_data(records: dict, **overrides) -> dict:
    data = {
        "activity_type": "buy",
        "effective_date": "2026-06-01",
        "account_id": records["account_id"],
        "instrument_id": records["instrument_id"],
        "quantity": "1000",
        "unit_price": "18.40",
        "fee_amount": "0",
    }
    data.update(overrides)
    return data


def _confirmation_data(**overrides: str) -> dict:
    data = {
        "currency_code": "EUR",
        "effective_date": "2026-06-30",
        "confirmed_balance_amount": "-17950",
        "source_note": "June broker statement",
    }
    data.update(overrides)
    return data


def _buy(client: FlaskClient, records: dict, **overrides) -> None:
    response = client.post("/activity/new", data=_trade_data(records, **overrides))
    assert response.status_code == 302, response.get_data(as_text=True)


def _confirm(client: FlaskClient, records: dict, **overrides) -> None:
    response = client.post(
        f"/accounts/{records['account_id']}/cash-confirmations",
        data=_confirmation_data(**overrides),
    )
    assert response.status_code == 302, response.get_data(as_text=True)


# --- Accounts index ---

def test_index_is_a_sortable_maintenance_surface(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/accounts").get_data(as_text=True)
    assert "<h1>Accounts</h1>" in body
    sidebar = body.split('<aside class="sidebar">', 1)[1].split("</aside>", 1)[0]
    current = re.search(r'<a href="([^"]+)" aria-current="page">([^<]+)</a>', sidebar)
    assert current and current.group(1) == "/accounts" and current.group(2) == "Accounts"

    # Sortable server links with current state exposed accessibly.
    assert 'aria-sort="ascending"' in body  # institution is the default sort
    assert 'href="/accounts?sort=account&amp;direction=asc&amp;as_of=2026-08-04"' in body
    assert 'href="/accounts?sort=cash&amp;direction=asc&amp;as_of=2026-08-04"' in body

    assert '<a href="/accounts/1">Brokerage EUR</a>' in body
    # Exactly one account allows a confirmation action.
    assert body.count("Set cash balance") == 1
    # The aggregate account shows an included state, never a zero balance.
    included_row = body.split("Pension wrapper", 1)[1].split("</tr>", 1)[0]
    assert "— included in aggregate" in included_row
    assert "0.00" not in included_row
    # As-of control re-requests server-side, preserving the current sort.
    form_tag = re.search(r'<form class="asof-form"[^>]*>', body).group(0)
    assert 'data-autosubmit="true"' in form_tag
    assert ">Apply</button>" in body
    assert 'name="sort" value="institution"' in body
    assert 'name="direction" value="asc"' in body
    assert_no_inline_script(body)


def test_index_sorting_and_as_of_preservation(
    client: FlaskClient, records: dict
) -> None:
    body = client.get(
        "/accounts?sort=account&direction=desc&as_of=2026-06-01"
    ).get_data(as_text=True)
    # Descending account order puts the wrapper before the brokerage.
    assert body.index("Pension wrapper") < body.index("Brokerage EUR")
    account_th = re.search(r'<th scope="col" aria-sort="descending">\s*<a[^>]*>Account', body)
    assert account_th
    # Sort links keep the requested as-of date.
    assert 'href="/accounts?sort=institution&amp;direction=asc&amp;as_of=2026-06-01"' in body
    assert 'href="/accounts?sort=account&amp;direction=asc&amp;as_of=2026-06-01"' in body
    # Invalid values fall back safely to the default sort.
    fallback = client.get("/accounts?sort=bogus&direction=sideways").get_data(as_text=True)
    assert fallback.index("Brokerage EUR") < fallback.index("Pension wrapper")


def test_index_exposes_edit_and_add_paths(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/accounts").get_data(as_text=True)
    assert body.count('>Edit</a>') == 2
    assert 'href="/accounts/1/edit"' in body
    assert 'href="/accounts/2/edit"' in body
    assert 'href="/accounts/new">Add account</a>' in body


def test_index_calculated_cash_and_negative_badge_after_buy(
    client: FlaskClient, records: dict
) -> None:
    _buy(client, records)  # cash effect EUR -18,400
    body = client.get("/accounts").get_data(as_text=True)
    row = body.split("Brokerage EUR", 1)[1].split("</tr>", 1)[0]
    assert '<span class="ccy">EUR</span> -18,400.00' in row
    assert "calculated" in row
    assert "Negative cash" in row


# --- Account detail traceability ---

def _trade_then_confirm(client: FlaskClient, records: dict) -> None:
    _buy(client, records)  # 1,000 @ 18.40 on 2026-06-01 → EUR -18,400
    _confirm(client, records)  # confirmed EUR -17,950 on 2026-06-30 → correction +450
    _buy(
        client, records, activity_type="sell", effective_date="2026-07-15",
        quantity="300", unit_price="11.10", fee_amount="20",
    )  # EUR +3,310 after the confirmation


def test_detail_shows_confirmation_traceability(
    client: FlaskClient, records: dict
) -> None:
    _trade_then_confirm(client, records)
    body = client.get(f"/accounts/{records['account_id']}").get_data(as_text=True)

    # Current cash: -17,950 confirmed + 3,310 later effect.
    assert '<span class="ccy">EUR</span> -14,640.00' in body
    assert "Confirmed on 30 Jun 2026" in body
    assert "Latest confirmation" in body
    assert '<span class="ccy">EUR</span> -17,950.00' in body
    assert "Prior calculated balance" in body
    assert '<span class="ccy">EUR</span> -18,400.00' in body
    assert "Reconciliation correction" in body
    assert '<span class="ccy">EUR</span> 450.00' in body
    assert "Later activity effects" in body
    assert '<span class="ccy">EUR</span> 3,310.00' in body
    assert "June broker statement" in body
    assert "not a deposit, withdrawal, income, expense, gain, or loss" in body

    # The effects table lists business activities only — no internal legs.
    table = body.split("Activity effects after the confirmation", 1)[1].split("</table>", 1)[0]
    assert ">Sell<" in table and "3,310.00" in table
    tbody = table.split("<tbody>", 1)[1]
    assert tbody.count("<tr>") == 1
    assert "clearing" not in body.lower()
    assert_no_inline_script(body)


def test_detail_historical_as_of_selects_eligible_effects(
    client: FlaskClient, records: dict
) -> None:
    _trade_then_confirm(client, records)

    before = client.get(
        f"/accounts/{records['account_id']}?as_of=2026-06-15"
    ).get_data(as_text=True)
    assert "Cash as of 15 Jun 2026" in before
    assert '<span class="ccy">EUR</span> -18,400.00' in before
    assert "Latest confirmation" not in before
    assert ">Buy<" in before and ">Sell<" not in before

    on_day = client.get(
        f"/accounts/{records['account_id']}?as_of=2026-06-30"
    ).get_data(as_text=True)
    # Same-day activity is not counted against the end-of-day confirmation.
    assert '<span class="ccy">EUR</span> -17,950.00' in on_day
    assert "Confirmed on 30 Jun 2026" in on_day
    assert ">Buy<" not in on_day and ">Sell<" not in on_day


def test_included_aggregate_detail_explains_without_action(
    client: FlaskClient, records: dict
) -> None:
    body = client.get(f"/accounts/{records['included_id']}").get_data(as_text=True)
    assert "No separate cash balance is added" in body
    assert "cash-confirmations/new" not in body


# --- Account creation continuation  ---

def _account_data(**overrides: str) -> dict:
    data = {
        "institution_id": "1",
        "name": "Everyday savings",
        "reference": "TEST-ACCOUNT-002",
        "account_type": "cash",
        "default_currency_code": "EUR",
        "cash_tracking_mode": "separate_cash",
        "portfolio_share_percent": "100",
        "present_access_percent": "100",
        "relationship_eligible": "y",
        "add_account": "Add account",
    }
    data.update(overrides)
    return data


def test_create_separate_account_continues_to_confirmation_and_returns(
    client: FlaskClient, records: dict
) -> None:
    created = client.post("/setup/accounts", data=_account_data())
    assert created.status_code == 302, created.get_data(as_text=True)
    location = created.headers["Location"]
    assert location.endswith(
        "/accounts/3/cash-confirmations/new?currency=EUR&return_to=setup_accounts"
    )

    body = client.get(location).get_data(as_text=True)
    # The user knows which account they are confirming and where they return.
    assert "Everyday savings" in body
    assert "opening cash balance" in body
    assert "add the next account" in body
    assert 'value="setup_accounts"' in body  # hidden whitelisted return target
    assert 'href="/setup?step=accounts">Back to account setup</a>' in body
    # Keyboard entry: currency/date are prefilled, the statement balance has focus.
    amount_tag = re.search(
        r'<input[^>]*id="confirmed_balance_amount"[^>]*>', body
    ).group(0)
    assert "autofocus" in amount_tag
    assert_no_inline_script(body)

    saved = client.post("/accounts/3/cash-confirmations", data={
        "return_to": "setup_accounts",
        "currency_code": "EUR",
        "effective_date": "2026-08-04",
        "confirmed_balance_amount": "17950",
    })
    assert saved.status_code == 302, saved.get_data(as_text=True)
    assert saved.headers["Location"].endswith("/setup?step=accounts")
    setup_body = client.get(saved.headers["Location"]).get_data(as_text=True)
    assert "Everyday savings" in setup_body
    # Reference is listed separately; the name stays the identity.
    assert "TEST-ACCOUNT-002" in setup_body


def test_create_aggregate_account_explains_no_opening_cash(
    client: FlaskClient, records: dict
) -> None:
    created = client.post("/setup/accounts", data=_account_data(
        name="Pension two", reference="",
        cash_tracking_mode="included_in_aggregate",
        present_access_percent="0",
    ))
    assert created.status_code == 302, created.get_data(as_text=True)
    assert created.headers["Location"].endswith("/setup?step=accounts")
    body = client.get(created.headers["Location"]).get_data(as_text=True)
    assert "no separate opening cash balance is needed" in body


# --- Account maintenance  ---

def test_edit_round_trip_and_accessible_validation(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/accounts/1/edit").get_data(as_text=True)
    name_tag = re.search(r'<input[^>]*id="name"[^>]*>', body).group(0)
    assert 'value="Brokerage EUR"' in name_tag
    # A separate-cash account sees the aggregate-switch consequence up front.
    assert "no longer count while cash is inside the aggregate value" in body
    assert 'href="/accounts/1">Cancel</a>' in body
    assert_no_inline_script(body)

    saved = client.post("/accounts/1/edit", data={
        "name": "Joint savings", "reference": "TEST-ACCOUNT-002",
        "cash_tracking_mode": "separate_cash", "save_account": "Save account",
    })
    assert saved.status_code == 302, saved.get_data(as_text=True)
    detail = client.get(saved.headers["Location"]).get_data(as_text=True)
    assert "<h1>Joint savings</h1>" in detail
    assert "Ref TEST-ACCOUNT-002" in detail
    assert 'href="/accounts/1/edit">Edit account</a>' in detail

    # Reference is shown separately on the list; it never replaces the name.
    index = client.get("/accounts").get_data(as_text=True)
    assert '<a href="/accounts/1">Joint savings</a>' in index
    assert "TEST-ACCOUNT-002" in index

    # Accessible validation: linked summary and focus on the first error.
    invalid = client.post("/accounts/1/edit", data={
        "name": "", "cash_tracking_mode": "separate_cash",
        "save_account": "Save account",
    }).get_data(as_text=True)
    assert "There is a problem" in invalid
    assert 'href="#name"' in invalid
    name_tag = re.search(r'<input[^>]*id="name"[^>]*>', invalid).group(0)
    assert "autofocus" in name_tag
    assert_no_inline_script(invalid)


def test_separate_to_aggregate_states_consequence(
    client: FlaskClient, records: dict
) -> None:
    saved = client.post("/accounts/1/edit", data={
        "name": "Brokerage EUR", "cash_tracking_mode": "included_in_aggregate",
        "save_account": "Save account",
    })
    assert saved.status_code == 302, saved.get_data(as_text=True)
    detail = client.get(saved.headers["Location"]).get_data(as_text=True)
    assert "preserved but no longer count" in detail


def test_aggregate_to_separate_requires_fresh_confirmation_without_javascript(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/accounts/2/edit").get_data(as_text=True)
    # Every confirmation field is available without JavaScript.
    assert '<details class="disclosure" open>' in body
    for field_id in ("currency_code", "confirmed_balance_amount",
                     "effective_date", "source_note"):
        assert f'id="{field_id}"' in body
    assert "required when switching to separate cash tracking" in body
    assert "superseded" in body

    blocked = client.post("/accounts/2/edit", data={
        "name": "Pension wrapper", "cash_tracking_mode": "separate_cash",
        "save_account": "Save account",
    }).get_data(as_text=True)
    assert "Fresh cash currency is required." in blocked
    assert "Fresh confirmed balance is required." in blocked
    assert "Confirmation date is required." in blocked
    assert 'href="#currency_code"' in blocked
    # First missing confirmation field receives focus without JavaScript.
    currency_tag = re.search(r'<input[^>]*id="currency_code"[^>]*>', blocked).group(0)
    assert "autofocus" in currency_tag
    assert_no_inline_script(blocked)


def test_aggregate_to_separate_saves_with_atomic_confirmation(
    client: FlaskClient, records: dict
) -> None:
    saved = client.post("/accounts/2/edit", data={
        "name": "Pension wrapper", "cash_tracking_mode": "separate_cash",
        "currency_code": "EUR", "confirmed_balance_amount": "61000",
        "effective_date": "2026-08-01", "source_note": "July statement",
        "save_account": "Save account",
    })
    assert saved.status_code == 302, saved.get_data(as_text=True)
    detail = client.get(saved.headers["Location"]).get_data(as_text=True)
    assert "fresh cash confirmation" in detail
    assert "cannot reappear as current cash" in detail


# --- Confirmation form ---

def test_confirmation_form_entry_order_and_focus(
    client: FlaskClient, records: dict
) -> None:
    _buy(client, records)
    body = client.get(
        f"/accounts/{records['account_id']}/cash-confirmations/new"
    ).get_data(as_text=True)

    positions = [
        body.index(f'id="{field}"')
        for field in (
            "currency_code", "confirmed_balance_amount",
            "effective_date", "source_note",
        )
    ]
    assert positions == sorted(positions)
    # Currency and date are prefilled, so the statement balance gets focus.
    assert body.count("autofocus") == 1
    amount_tag = re.search(
        r'<input[^>]*id="confirmed_balance_amount"[^>]*>', body
    ).group(0)
    assert "autofocus" in amount_tag

    # Cash confirmation wording: Calculated cash reference, Statement balance, As of.
    assert "Calculated cash" in body
    assert '<span class="ccy">EUR</span> -18,400.00' in body
    assert "Statement balance" in body
    assert "Treated as end-of-day" in body
    # Deliberately no category, reason, or explanation fields.
    assert 'name="category"' not in body and 'name="explanation"' not in body
    assert_no_inline_script(body)


def test_confirmation_error_is_described_linked_and_focused(
    client: FlaskClient, records: dict
) -> None:
    body = client.post(
        f"/accounts/{records['account_id']}/cash-confirmations",
        data=_confirmation_data(confirmed_balance_amount="not-a-number"),
    ).get_data(as_text=True)
    assert "There is a problem" in body
    assert 'href="#confirmed_balance_amount"' in body
    field = re.search(
        r'<input[^>]*id="confirmed_balance_amount"[^>]*>', body
    ).group(0)
    assert 'aria-invalid="true"' in field
    assert 'aria-describedby="confirmed_balance_amount-error confirmed_balance_amount-hint"' in field
    assert "autofocus" in field


# --- Setup Cash step ---

def test_setup_cash_step_rail_and_mode_copy(
    client: FlaskClient, records: dict
) -> None:
    body = client.get("/setup/cash").get_data(as_text=True)
    assert "<h1>Add current cash</h1>" in body
    assert body.count('aria-current="step"') == 1
    rail = body.split('aria-label="Setup progress"', 1)[1].split("</nav>", 1)[0]
    # Cash is linked in the setup rail once an account exists.
    assert 'href="/setup/cash"' in rail
    # Mode-appropriate entry points.
    assert body.count("Set current cash balance") == 1
    assert "cash is already inside this account" in body
    assert "aggregate statement value" in body
    assert_no_inline_script(body)


# --- Checkpoint-relative trade warning ---

def test_backdated_trade_preview_renders_amber_banner(
    client: FlaskClient, records: dict
) -> None:
    _confirm(client, records)
    body = client.post(
        "/activity/preview",
        data=_trade_data(records, effective_date="2026-06-30", quantity="10",
                         unit_price="12"),
    ).get_data(as_text=True)
    warning = re.search(r'<p class="banner-warning">(.*?)</p>', body)
    assert warning
    assert "on or before the cash confirmation dated 2026-06-30" in warning.group(1)
    assert "not current confirmed-and-carried-forward cash" in warning.group(1)


# --- Observed timing ---

def test_confirmation_flow_observed_timing(
    client: FlaskClient, records: dict
) -> None:
    """The no-JS confirmation flow is exactly three page loads (form,
    post-redirect, account detail) and stays well inside a generous budget."""
    _buy(client, records)
    started = time.perf_counter()
    form = client.get(f"/accounts/{records['account_id']}/cash-confirmations/new")
    assert form.status_code == 200
    saved = client.post(
        f"/accounts/{records['account_id']}/cash-confirmations",
        data=_confirmation_data(),
    )
    assert saved.status_code == 302
    detail = client.get(saved.headers["Location"])
    assert detail.status_code == 200
    elapsed = time.perf_counter() - started

    assert "Cash confirmation saved" in detail.get_data(as_text=True)
    assert elapsed < 2.0


# --- Share/access overlay  ---

def _overlay_records(
    app: Flask, *, share: str = "0.625", access: str = "0.8", value: str | None = "1000"
) -> int:
    with app.app_context():
        portfolio = Portfolio(
            name="Personal portfolio", reporting_currency_code="EUR",
            annual_spending_amount=Decimal("48000"),
            default_as_of_date=date(2026, 8, 4),
        )
        db.session.add(portfolio)
        db.session.flush()
        institution = Institution(portfolio_id=portfolio.id, name="Bank")
        db.session.add(institution)
        db.session.flush()
        account = Account(
            portfolio_id=portfolio.id, institution_id=institution.id,
            name="Joint brokerage", account_type="brokerage",
            default_currency_code="EUR", is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal(share),
            present_access_decimal=Decimal(access),
            relationship_eligible=True, is_active=True,
        )
        db.session.add(account)
        db.session.flush()
        instrument = Instrument(
            portfolio_id=portfolio.id, name="Endowment",
            instrument_type="other", valuation_currency_code="EUR", is_active=True,
        )
        db.session.add(instrument)
        db.session.flush()
        registration = PositionRegistration(
            account_id=account.id, instrument_id=instrument.id,
            tracking_mode="statement_valued", opening_date=date(2026, 8, 1),
        )
        db.session.add(registration)
        db.session.flush()
        if value is not None:
            db.session.add(ValuationObservation(
                position_registration_id=registration.id,
                effective_date=date(2026, 8, 1),
                native_value_amount=Decimal(value), currency_code="EUR",
            ))
        db.session.commit()
        return account.id


def test_detail_value_card_full_breakdown(client: FlaskClient, app: Flask) -> None:
    account_id = _overlay_records(app)
    body = client.get(f"/accounts/{account_id}").get_data(as_text=True)
    card = body.split('id="value-heading"', 1)[1].split("</section>", 1)[0]
    assert "62.5%" in card and "80% of the included share" in card
    # EUR 1,000 × 62.5% × 80% = 625 included / 500 accessible,
    # 375 excluded by share, 125 restricted.
    assert "1,000.00" in card   # gross tracked
    assert "625.00" in card     # included
    assert "500.00" in card     # accessible now
    assert "375.00" in card     # excluded by share
    assert "125.00" in card     # restricted
    assert_no_inline_script(body)


def test_detail_zero_share_is_a_real_zero(client: FlaskClient, app: Flask) -> None:
    account_id = _overlay_records(app, share="0", access="1")
    body = client.get(f"/accounts/{account_id}").get_data(as_text=True)
    card = body.split('id="value-heading"', 1)[1].split("</section>", 1)[0]
    included_row = card.split("Included in portfolio", 1)[1].split("</dd>", 1)[0]
    assert "0.00" in included_row and "money-missing" not in included_row
    assert "1,000.00" in card  # excluded by share


def test_detail_missing_value_stays_missing(client: FlaskClient, app: Flask) -> None:
    account_id = _overlay_records(app, value=None)
    body = client.get(f"/accounts/{account_id}").get_data(as_text=True)
    card = body.split('id="value-heading"', 1)[1].split("</section>", 1)[0]
    assert card.count('<span class="money-missing">—</span>') == 3
    assert "0.00" not in card


def test_index_shows_factors_and_included_value(client: FlaskClient, app: Flask) -> None:
    _overlay_records(app)
    body = client.get("/accounts").get_data(as_text=True)
    assert "Share 62.5%" in body
    assert "access now 80%" in body
    assert "Included value" in body
    assert "625.00" in body
    assert "gross" in body and "accessible now" in body


def _overlay_edit_data(**overrides: str) -> dict[str, str]:
    data = {
        "name": "Joint brokerage",
        "reference": "",
        "cash_tracking_mode": "separate_cash",
        "portfolio_share_percent": "62.5",
        "present_access_percent": "80",
        "earliest_access_date": "2030-01-01",
        "access_note": "Locked until retirement",
        "save_account": "Save account",
    }
    data.update(overrides)
    return data


def test_edit_share_access_round_trip_and_clear(client: FlaskClient, app: Flask) -> None:
    account_id = _overlay_records(app)
    response = client.post(
        f"/accounts/{account_id}/edit", data=_overlay_edit_data()
    )
    assert response.status_code == 302
    detail = client.get(f"/accounts/{account_id}").get_data(as_text=True)
    assert "62.5%" in detail and "80% of the included share" in detail
    assert "locked until 1 Jan 2030" in detail
    assert "Locked until retirement" in detail
    # Explicit blank optional context clears it.
    response = client.post(
        f"/accounts/{account_id}/edit",
        data=_overlay_edit_data(earliest_access_date="", access_note=""),
    )
    assert response.status_code == 302
    detail = client.get(f"/accounts/{account_id}").get_data(as_text=True)
    assert "locked until" not in detail
    assert "Locked until retirement" not in detail


def test_edit_invalid_percentage_linked_focused_and_atomic(
    client: FlaskClient, app: Flask
) -> None:
    account_id = _overlay_records(app)
    response = client.post(
        f"/accounts/{account_id}/edit",
        data=_overlay_edit_data(name="Renamed", portfolio_share_percent="120"),
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'href="#portfolio_share_percent"' in body
    assert "must be between 0 and 100" in body
    field = re.search(r'<input[^>]*id="portfolio_share_percent"[^>]*>', body).group(0)
    assert 'aria-invalid="true"' in field and "autofocus" in field
    # Nothing partially saved: the attempted rename did not stick.
    detail = client.get(f"/accounts/{account_id}").get_data(as_text=True)
    assert "<h1>Joint brokerage</h1>" in detail
