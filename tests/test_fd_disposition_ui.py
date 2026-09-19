"""Maturity-disposition UI tests: form presentation,
preview-card hierarchy, explicit manual zero cash effect, error focus,
attention/Holdings entry points, and no-JavaScript completeness. Amounts,
statuses, and posting decisions come from the backend; these tests assert
only their presentation.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient

from app.extensions import db
from app.models import (
    Account,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    ValuationObservation,
)
from app.services.fixed_deposits import (
    FixedDepositCommand,
    MaturityDispositionCommand,
    post_maturity_disposition,
    save_fixed_deposit_terms,
)

from test_overview_ui import assert_no_inline_script


MATURITY = date(2026, 8, 20)
AS_OF = date(2026, 8, 28)  # eight days past maturity


def _records(app: Flask) -> tuple[int, int, int]:
    """Overdue statement-valued USD deposit plus a separate USD cash account.

    Returns (registration_id, cash_account_id, portfolio_id).
    """

    with app.app_context():
        portfolio = Portfolio(
            name="Portfolio", reporting_currency_code="USD",
            annual_spending_amount=Decimal("48000"),
            annual_spending_currency_code="USD",
            default_as_of_date=AS_OF,
        )
        db.session.add(portfolio)
        db.session.flush()
        institution = Institution(portfolio_id=portfolio.id, name="Bank")
        db.session.add(institution)
        db.session.flush()
        fd_account = Account(
            portfolio_id=portfolio.id, institution_id=institution.id,
            name="Fixed Deposits", account_type="deposit",
            default_currency_code="USD", is_multicurrency=False,
            cash_tracking_mode="included_in_aggregate",
            portfolio_share_decimal=Decimal("1"), present_access_decimal=Decimal("1"),
            relationship_eligible=True, is_active=True,
        )
        cash_account = Account(
            portfolio_id=portfolio.id, institution_id=institution.id,
            name="USD Savings", account_type="cash",
            default_currency_code="USD", is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"), present_access_decimal=Decimal("1"),
            relationship_eligible=True, is_active=True,
        )
        db.session.add_all([fd_account, cash_account])
        db.session.flush()
        instrument = Instrument(
            portfolio_id=portfolio.id, name="FD_USD_203",
            instrument_type="fixed_deposit", valuation_currency_code="USD",
            is_active=True,
        )
        db.session.add(instrument)
        db.session.flush()
        registration = PositionRegistration(
            account_id=fd_account.id, instrument_id=instrument.id,
            tracking_mode="statement_valued", opening_date=date(2026, 5, 8),
        )
        db.session.add(registration)
        db.session.flush()
        db.session.add(ValuationObservation(
            position_registration_id=registration.id,
            effective_date=date(2026, 5, 8),
            native_value_amount=Decimal("85000"), currency_code="USD",
        ))
        save_fixed_deposit_terms(
            portfolio.id,
            FixedDepositCommand(
                registration_id=registration.id,
                currency_code="USD",
                start_date=date(2026, 5, 8),
                maturity_date=MATURITY,
                annual_rate_decimal=None,
                maturity_action="return_to_cash",
                expected_maturity_proceeds_amount=Decimal("85690.25"),
                notes=None,
            ),
        )
        db.session.commit()
        return registration.id, cash_account.id, portfolio.id


def _post_data(cash_id: int, **overrides) -> dict[str, str]:
    data = {
        "disposition_type": "return_to_cash",
        "effective_date": "2026-08-24",
        "confirmed_principal_amount": "85000",
        "confirmed_interest_amount": "690.25",
        "cash_account_id": str(cash_id),
        "note": "",
        "successor_instrument_name": "",
        "successor_reference": "",
        "successor_maturity_date": "",
        "successor_annual_rate_percent": "",
        "successor_expected_proceeds_amount": "",
        "successor_maturity_action": "undecided",
        "preview_disposition": "Preview",
        **overrides,
    }
    return data


def _preview(client: FlaskClient, registration_id: int, cash_id: int, **overrides) -> str:
    response = client.post(
        f"/positions/{registration_id}/fixed-deposit/disposition?as_of=2026-08-28",
        data=_post_data(cash_id, **overrides),
    )
    assert response.status_code == 200
    return response.get_data(as_text=True)


# --- Entry form presentation ---

def test_disposition_form_context_status_prefills_and_keyboard_path(
    client: FlaskClient, app: Flask
) -> None:
    registration_id, cash_account_id, _ = _records(app)
    page = client.get(
        f"/positions/{registration_id}/fixed-deposit/disposition?as_of=2026-08-28"
    )
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert body.count("<h1") == 1 and "<h1>Record maturity disposition</h1>" in body
    assert "FD_USD_203 · Bank · Fixed Deposits" in body
    # The maturity state is explicit before any amount is asked for.
    assert "Maturity overdue" in body
    assert "matured 20 Aug 2026 — nothing was posted automatically." in body
    # The bank quote stays visually subordinate reference data.
    assert re.search(
        r'<p class="meta">Bank-quoted expected proceeds:.*85,690\.25.*reference data, not the amount that will be posted\.</p>',
        body, re.S,
    )
    # Every field has a programmatic label; required is stated in text.
    for field in ("disposition_type", "effective_date", "confirmed_principal_amount",
                  "confirmed_interest_amount", "cash_account_id", "note",
                  "successor_instrument_name", "successor_maturity_date"):
        assert f'<label for="{field}">' in body
    required = re.findall(r'<label for="(\w+)">[^<]*<span class="req">', body)
    assert required == [
        "disposition_type", "effective_date",
        "confirmed_principal_amount", "confirmed_interest_amount",
    ]
    # Hints state the confirmed-amounts contract in plain language.
    assert "not a calculated amount" in body
    assert "Never calculated from the annual rate" in body
    # GET prefills principal and derives the interest prefill from the bank
    # quote, leaving both editable.
    assert 'value="85000"' in body
    assert 'value="690.25"' in body
    # Settlement date defaults to today (never before maturity).
    assert f'value="{max(date.today(), MATURITY).isoformat()}"' in body
    # Successor fields sit inside native disclosure; closed on first render.
    assert "<details>" in body
    assert "Successor deposit details (rollover only)" in body
    # No-JavaScript path: explicit Preview submit, Cancel keeps the as-of date.
    assert "Preview maturity disposition" in body
    assert 'name="confirm_disposition"' not in body  # confirm appears only after preview
    assert f'href="/positions/{registration_id}/fixed-deposit?as_of=2026-08-28">Cancel</a>' in body
    assert_no_inline_script(body)


def test_disposition_form_rejects_not_yet_due_deposit(
    client: FlaskClient, app: Flask
) -> None:
    registration_id, _, _ = _records(app)
    response = client.get(
        f"/positions/{registration_id}/fixed-deposit/disposition?as_of=2026-08-19"
    )
    assert response.status_code == 404


# --- Preview card ---

def test_preview_return_to_cash_names_outcome_and_destination(
    client: FlaskClient, app: Flask
) -> None:
    registration_id, cash_account_id, _ = _records(app)
    body = _preview(client, registration_id, cash_account_id)
    card = body.split('aria-labelledby="disposition-preview-heading"', 1)[1].split("</section>", 1)[0]
    assert "Check before recording" in card
    assert "<dt>Outcome</dt><dd>Return principal and interest to cash</dd>" in card
    assert "85,000.00" in card and "690.25" in card
    assert "85,690.25" in card and "to USD Savings" in card
    assert "<dt>Confirmed minus bank quote</dt>" in card
    assert "0.00" in card  # confirmed total matches the bank quote exactly
    # Keyboard flow: after preview, focus lands on the preview heading.
    assert '<h2 id="disposition-preview-heading" tabindex="-1" autofocus>' in card
    assert "Record maturity disposition" in body
    assert_no_inline_script(body)


def test_preview_rollover_shows_successor_and_interest_only_cash(
    client: FlaskClient, app: Flask
) -> None:
    registration_id, cash_account_id, _ = _records(app)
    body = _preview(
        client, registration_id, cash_account_id,
        disposition_type="rollover",
        confirmed_interest_amount="714.66",
        successor_instrument_name="FD_USD_207",
        successor_maturity_date="2026-11-20",
    )
    card = body.split('aria-labelledby="disposition-preview-heading"', 1)[1].split("</section>", 1)[0]
    assert "<dt>Outcome</dt><dd>Rollover principal; take interest in cash</dd>" in card
    assert "690.25" not in card  # only the entered confirmed interest appears
    assert "714.66" in card and "to USD Savings" in card
    assert "(confirmed interest only — principal moves to the successor deposit)" in card
    assert "FD_USD_207 · matures 20 Nov 2026" in card
    # The disclosure opens so the entered successor terms stay visible.
    assert "<details open>" in body


def test_preview_manual_makes_zero_cash_effect_explicit(
    client: FlaskClient, app: Flask
) -> None:
    registration_id, cash_account_id, _ = _records(app)
    body = _preview(
        client, registration_id, cash_account_id,
        disposition_type="manual",
        cash_account_id="0",
        note="Principal moved to an external account not tracked here",
    )
    card = body.split('aria-labelledby="disposition-preview-heading"', 1)[1].split("</section>", 1)[0]
    assert "<dt>Outcome</dt><dd>Other / manual disposition</dd>" in card
    # Zero is a stated fact, not an apparent missing value.
    assert "0.00" in card
    assert "no cash destination; the deposit closes with no recorded cash movement" in card
    assert "USD Savings" not in card
    # The required note is shown back for confirmation.
    assert "<dt>Note</dt><dd>Principal moved to an external account not tracked here</dd>" in card


# --- Validation and focus ---

def test_error_summary_and_first_error_focus(client: FlaskClient, app: Flask) -> None:
    registration_id, cash_account_id, _ = _records(app)
    body = _preview(
        client, registration_id, cash_account_id, confirmed_principal_amount=""
    )
    summary = re.search(
        r'<div class="error-summary" role="alert">.*?</div>', body, re.S
    ).group(0)
    assert 'href="#confirmed_principal_amount"' in summary
    field = re.search(
        r'<input[^>]*id="confirmed_principal_amount"[^>]*>', body
    ).group(0)
    assert 'aria-invalid="true"' in field
    assert 'aria-describedby="confirmed_principal_amount-error confirmed_principal_amount-hint"' in field
    assert "autofocus" in field
    assert_no_inline_script(body)


def test_manual_disposition_missing_note_targets_note_field(
    client: FlaskClient, app: Flask
) -> None:
    registration_id, cash_account_id, _ = _records(app)
    body = _preview(
        client, registration_id, cash_account_id,
        disposition_type="manual", cash_account_id="0", note="",
    )
    assert "Explain the confirmed manual disposition." in body
    field = re.search(r'<textarea[^>]*id="note"[^>]*>', body).group(0)
    assert 'aria-invalid="true"' in field and "autofocus" in field


# --- Entry points and retirement ---

def test_overdue_fd_entry_points_target_disposition_with_as_of(
    client: FlaskClient, app: Flask
) -> None:
    registration_id, _, _ = _records(app)
    holdings = client.get("/holdings?as_of=2026-08-28").get_data(as_text=True)
    row = holdings.split('<th scope="row">FD_USD_203</th>', 1)[1].split("</tr>", 1)[0]
    assert "Maturity overdue" in row
    assert (f'href="/positions/{registration_id}/fixed-deposit/disposition?as_of=2026-08-28"'
            in row)
    assert "Record disposition →" in row
    assert "Edit terms →" not in row

    overview = client.get("/overview?as_of=2026-08-28").get_data(as_text=True)
    attention = overview.split('class="attention-list"', 1)[1].split("</ul>", 1)[0]
    assert (f'href="/positions/{registration_id}/fixed-deposit/disposition?as_of=2026-08-28"'
            in attention)

    review = client.get("/setup/review").get_data(as_text=True)
    assert "Record disposition →" in review

    terms = client.get(
        f"/positions/{registration_id}/fixed-deposit?as_of=2026-08-28"
    ).get_data(as_text=True)
    assert (f'href="/positions/{registration_id}/fixed-deposit/disposition?as_of=2026-08-28"'
            in terms)


def test_disposed_deposit_leaves_current_views_but_keeps_history(
    client: FlaskClient, app: Flask
) -> None:
    registration_id, cash_account_id, portfolio_id = _records(app)
    with app.app_context():
        post_maturity_disposition(
            MaturityDispositionCommand(
                portfolio_id=portfolio_id,
                registration_id=registration_id,
                disposition_type="return_to_cash",
                effective_date=date(2026, 8, 24),
                confirmed_principal_amount=Decimal("85000"),
                confirmed_interest_amount=Decimal("690.25"),
                cash_account_id=cash_account_id,
                note="Bank maturity statement",
            )
        )
        db.session.commit()

    # Current Holdings and attention no longer carry the closed deposit.
    holdings = client.get("/holdings?as_of=2026-08-28").get_data(as_text=True)
    assert "FD_USD_203" not in holdings
    overview = client.get("/overview?as_of=2026-08-28").get_data(as_text=True)
    assert "Record disposition →" not in overview

    # The disposition route itself exits honestly instead of erroring.
    response = client.get(
        f"/positions/{registration_id}/fixed-deposit/disposition?as_of=2026-08-28"
    )
    assert response.status_code == 302
    assert "/activity" in response.headers["Location"]

    # Activity history keeps the immutable record with its confirmed amounts.
    history = client.get("/activity").get_data(as_text=True)
    assert "Fixed-deposit maturity" in history
    assert "FD_USD_203" in history


def test_any_successor_field_error_reopens_the_disclosure(
    client: FlaskClient, app: Flask
) -> None:
    """Every successor-field error re-opens the disclosure, not only the
    name and maturity date; the disposition stays return-to-cash so nothing
    else can open it."""
    registration_id, cash_account_id, _ = _records(app)
    for field, value, error_id in (
        ("successor_annual_rate_percent", "-5", "successor_annual_rate_percent-error"),
        ("successor_expected_proceeds_amount", "abc", "successor_expected_proceeds_amount-error"),
    ):
        body = _preview(client, registration_id, cash_account_id, **{field: value})
        assert f'id="{error_id}"' in body
        assert "<details open>" in body
