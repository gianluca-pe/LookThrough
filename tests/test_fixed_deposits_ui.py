"""Fixed-deposit UI tests: terms form presentation,
maturity-state copy, Holdings status cell, term-liquidity disclosure, and
FD attention links. Dates, amounts, statuses, and attention decisions come
from the backend view models; these tests assert only their presentation.
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
from app.services.fixed_deposits import FixedDepositCommand, save_fixed_deposit_terms

from test_overview_ui import assert_no_inline_script


AS_OF = date(2026, 8, 2)


def _fd_position(app: Flask) -> tuple[int, int]:
    """A statement-valued USD fixed deposit opened 2 Jun 2026 at 85,000."""

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
        account = Account(
            portfolio_id=portfolio.id, institution_id=institution.id,
            name="Fixed Deposits", account_type="deposit",
            default_currency_code="USD", is_multicurrency=False,
            cash_tracking_mode="included_in_aggregate",
            portfolio_share_decimal=Decimal("1"), present_access_decimal=Decimal("1"),
            relationship_eligible=True, is_active=True,
        )
        db.session.add(account)
        db.session.flush()
        instrument = Instrument(
            portfolio_id=portfolio.id, name="FD_USD_204",
            instrument_type="fixed_deposit", valuation_currency_code="USD",
            is_active=True,
        )
        db.session.add(instrument)
        db.session.flush()
        registration = PositionRegistration(
            account_id=account.id, instrument_id=instrument.id,
            tracking_mode="statement_valued", opening_date=date(2026, 6, 2),
        )
        db.session.add(registration)
        db.session.flush()
        db.session.add(ValuationObservation(
            position_registration_id=registration.id,
            effective_date=date(2026, 6, 2),
            native_value_amount=Decimal("85000"), currency_code="USD",
        ))
        db.session.commit()
        return portfolio.id, registration.id


def _terms(
    app: Flask,
    registration_id: int,
    *,
    maturity: date = date(2026, 8, 20),
    action: str = "undecided",
) -> None:
    with app.app_context():
        save_fixed_deposit_terms(
            1,
            FixedDepositCommand(
                registration_id=registration_id,
                currency_code="USD",
                start_date=date(2026, 6, 2),
                maturity_date=maturity,
                annual_rate_decimal=None,
                maturity_action=action,
                notes=None,
            ),
        )
        db.session.commit()


def _post_terms(client: FlaskClient, registration_id: int, **overrides):
    data = {
        "currency_code": "USD",
        "start_date": "2026-06-02",
        "maturity_date": "2026-08-20",
        "annual_rate_percent": "",
        "maturity_action": "undecided",
        "notes": "",
        **overrides,
    }
    return client.post(
        f"/positions/{registration_id}/fixed-deposit?as_of=2026-08-02", data=data
    )


# --- Terms form presentation ---


def test_terms_form_error_summary_and_first_error_focus(
    client: FlaskClient, app: Flask
) -> None:
    _, registration_id = _fd_position(app)
    response = _post_terms(client, registration_id, maturity_date="2026-06-01")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    summary = re.search(
        r'<div class="error-summary" role="alert">.*?</div>', body, re.S
    ).group(0)
    assert 'href="#maturity_date"' in summary
    assert "Maturity date must be after the start date" in summary
    field = re.search(r'<input[^>]*id="maturity_date"[^>]*>', body).group(0)
    assert 'aria-invalid="true"' in field
    assert 'aria-describedby="maturity_date-error maturity_date-hint"' in field
    assert "autofocus" in field
    assert_no_inline_script(body)


def test_terms_form_currency_mismatch_error_targets_currency_field(
    client: FlaskClient, app: Flask
) -> None:
    _, registration_id = _fd_position(app)
    body = _post_terms(client, registration_id, currency_code="EUR").get_data(as_text=True)
    assert "valuation currency" in body
    field = re.search(r'<input[^>]*id="currency_code"[^>]*>', body).group(0)
    assert 'aria-invalid="true"' in field and "autofocus" in field


def test_terms_form_maturity_state_lines(client: FlaskClient, app: Flask) -> None:
    _, registration_id = _fd_position(app)
    _terms(app, registration_id, maturity=date(2026, 8, 20))
    url = f"/positions/{registration_id}/fixed-deposit"

    active = client.get(f"{url}?as_of=2026-07-01").get_data(as_text=True)
    assert "Matures 20 Aug 2026 — within the agreed term." in active
    assert "badge-warning" not in active.split("<form", 1)[0]

    approaching = client.get(f"{url}?as_of=2026-08-02").get_data(as_text=True)
    assert "Approaching maturity" in approaching
    assert "(18 days)" in approaching

    due = client.get(f"{url}?as_of=2026-08-20").get_data(as_text=True)
    assert "Due today" in due
    assert "no cash or rollover is posted automatically" in due

    overdue = client.get(f"{url}?as_of=2026-08-23").get_data(as_text=True)
    assert "Maturity overdue" in overdue
    assert "(3 days ago)" in overdue


# --- Holdings status cell ---

def _holdings_fd_row(client: FlaskClient, as_of: str = "2026-08-02") -> str:
    body = client.get(f"/holdings?as_of={as_of}").get_data(as_text=True)
    return body.split('<th scope="row">FD_USD_204</th>', 1)[1].split("</tr>", 1)[0]


def test_holdings_fd_approaching_badge_date_and_edit_link(
    client: FlaskClient, app: Flask
) -> None:
    _, registration_id = _fd_position(app)
    _terms(app, registration_id)
    row = _holdings_fd_row(client)
    assert "Matures soon" in row
    assert "matures 20 Aug 2026" in row
    assert 'href="/positions/1/fixed-deposit?as_of=2026-08-02"' in row
    assert 'aria-label="Edit fixed deposit terms for FD_USD_204">Edit terms →</a>' in row
    # Term-locked value is disclosed under the reporting value.
    assert "term liquidity" in row and "85,000.00" in row
    assert "accessible now" in row and "0.00" in row
    # Statement age never makes a fixed deposit look stale.
    assert "Stale" not in row
    # Stacked status content, not ad-hoc break spacing.
    assert '<div class="cell-stack">' in row


def test_holdings_missing_terms_offers_add_action(
    client: FlaskClient, app: Flask
) -> None:
    _fd_position(app)
    row = _holdings_fd_row(client)
    assert "FD terms missing" in row
    assert ('aria-label="Add fixed deposit terms for FD_USD_204">Add terms →</a>') in row


def test_holdings_fd_due_and_overdue_badges(
    client: FlaskClient, app: Flask
) -> None:
    _, registration_id = _fd_position(app)
    _terms(app, registration_id)
    assert "Due today" in _holdings_fd_row(client, as_of="2026-08-20")
    assert "Maturity overdue" in _holdings_fd_row(client, as_of="2026-09-01")


# --- Overview, Review, and Accounts disclosure ---

def _subtotals(body: str) -> str:
    return body.split('class="data subtotals"', 1)[1].split("</table>", 1)[0]


def test_overview_term_liquidity_row_and_fd_attention(
    client: FlaskClient, app: Flask
) -> None:
    _, registration_id = _fd_position(app)
    _terms(app, registration_id)
    body = client.get("/overview?as_of=2026-08-02").get_data(as_text=True)
    subtotals = _subtotals(body)
    term_row = subtotals.split("Term liquidity", 1)[1].split("</tr>", 1)[0]
    assert "85,000.00" in term_row
    assert "not reached maturity" in term_row
    accessible_row = subtotals.split("Accessible now", 1)[1].split("</tr>", 1)[0]
    assert "0.00" in accessible_row
    assert "stays locked until maturity" in accessible_row
    attention = body.split('class="attention-list"', 1)[1].split("</ul>", 1)[0]
    assert "<strong>Fixed deposit</strong> — FD_USD_204 · Fixed Deposits" in attention
    assert ('href="/positions/1/fixed-deposit?as_of=2026-08-02">Review terms →</a>'
            in attention)


def test_overview_matured_fd_leaves_term_liquidity(
    client: FlaskClient, app: Flask
) -> None:
    _, registration_id = _fd_position(app)
    _terms(app, registration_id, action="return_to_cash")
    body = client.get("/overview?as_of=2026-08-20").get_data(as_text=True)
    subtotals = _subtotals(body)
    assert "Term liquidity" not in subtotals
    # With full share/access and nothing term-locked, no bridge rows appear.
    assert "Accessible now" not in subtotals



def test_account_detail_discloses_term_liquidity_separately(
    client: FlaskClient, app: Flask
) -> None:
    _, registration_id = _fd_position(app)
    _terms(app, registration_id)
    body = client.get("/accounts/1?as_of=2026-08-02").get_data(as_text=True)
    value = body.split('id="value-heading"', 1)[1].split("</section>", 1)[0]
    assert "<dt>Accessible now</dt>" in value
    term = value.split("<dt>Term liquidity</dt>", 1)[1].split("</dd>", 1)[0]
    assert "85,000.00" in term
    assert "fixed deposits not yet at maturity" in term
    assert "Excluded by share" not in value
    assert "Restricted by access setting" not in value
