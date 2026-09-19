"""Relationship-minimum UI tests: Accounts table states,
account-detail card, the terms form's labels/error focus, and Overview
attention links. Values, buffers, statuses, and attention decisions come
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
    FxRate,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    ValuationObservation,
)
from app.services.cash import CashConfirmationCommand, confirm_cash
from app.services.relationships import RelationshipRuleCommand, save_relationship_rule

from test_overview_ui import assert_no_inline_script


AS_OF = date(2026, 8, 28)


def _records(app: Flask) -> tuple[int, int, int, int]:
    """Portfolio with one bank, an eligible and an excluded EUR cash account,
    and a current EUR/GBP rate (a synthetic multi-currency case)."""

    with app.app_context():
        portfolio = Portfolio(
            name="Portfolio", reporting_currency_code="EUR",
            annual_spending_amount=Decimal("48000"),
            annual_spending_currency_code="EUR",
            default_as_of_date=AS_OF, fx_stale_days=7,
        )
        db.session.add(portfolio)
        db.session.flush()
        institution = Institution(portfolio_id=portfolio.id, name="Example Bank")
        other = Institution(portfolio_id=portfolio.id, name="Other Bank")
        db.session.add_all([institution, other])
        db.session.flush()
        eligible = Account(
            portfolio_id=portfolio.id, institution_id=institution.id,
            name="Eligible EUR account", account_type="cash",
            default_currency_code="EUR", is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("0.5"),
            present_access_decimal=Decimal("0.2"),
            relationship_eligible=True, is_active=True,
        )
        excluded = Account(
            portfolio_id=portfolio.id, institution_id=institution.id,
            name="Excluded EUR account", account_type="cash",
            default_currency_code="EUR", is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"), present_access_decimal=Decimal("1"),
            relationship_eligible=False, is_active=True,
        )
        db.session.add_all([eligible, excluded])
        db.session.flush()
        for account, amount in ((eligible, "100000"), (excluded, "50000")):
            confirm_cash(
                CashConfirmationCommand(
                    portfolio_id=portfolio.id, account_id=account.id,
                    currency_code="EUR", effective_date=date(2026, 8, 27),
                    confirmed_balance_amount=Decimal(amount),
                )
            )
        # A small position on the excluded account gets past the Overview
        # no-positions redirect without touching the relationship balance.
        instrument = Instrument(
            portfolio_id=portfolio.id, name="Excluded fund",
            instrument_type="fund", valuation_currency_code="EUR", is_active=True,
        )
        db.session.add(instrument)
        db.session.flush()
        registration = PositionRegistration(
            account_id=excluded.id, instrument_id=instrument.id,
            tracking_mode="statement_valued", opening_date=date(2026, 8, 1),
        )
        db.session.add(registration)
        db.session.flush()
        db.session.add(ValuationObservation(
            position_registration_id=registration.id,
            effective_date=date(2026, 8, 27),
            native_value_amount=Decimal("100"), currency_code="EUR",
        ))
        db.session.add(FxRate(
            effective_date=date(2026, 8, 27), base_currency_code="EUR",
            quote_currency_code="GBP", quote_per_base_amount=Decimal("0.85573"),
        ))
        db.session.commit()
        return portfolio.id, institution.id, eligible.id, excluded.id


def _rule(
    app: Flask,
    institution_id: int,
    eligible_ids: int | tuple[int, ...],
    *,
    threshold: str = "75000",
    buffer: str | None = None,
    active: bool = True,
) -> None:
    ids = (eligible_ids,) if isinstance(eligible_ids, int) else eligible_ids
    with app.app_context():
        save_relationship_rule(
            1,
            RelationshipRuleCommand(
                institution_id=institution_id,
                name="Premier relationship",
                threshold_amount=Decimal(threshold),
                threshold_currency_code="GBP",
                warning_buffer_amount=Decimal(buffer) if buffer else None,
                is_active=active,
                eligible_account_ids=ids,
                notes=None,
            ),
        )
        db.session.commit()


def _gbp_account(app: Flask, institution_id: int) -> int:
    """A second eligible account already in the threshold currency: it keeps a
    known subtotal traceable when another account cannot be converted."""

    with app.app_context():
        account = Account(
            portfolio_id=1, institution_id=institution_id,
            name="GBP account", account_type="cash",
            default_currency_code="GBP", is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"), present_access_decimal=Decimal("1"),
            relationship_eligible=True, is_active=True,
        )
        db.session.add(account)
        db.session.flush()
        confirm_cash(
            CashConfirmationCommand(
                portfolio_id=1, account_id=account.id,
                currency_code="GBP", effective_date=date(2026, 8, 27),
                confirmed_balance_amount=Decimal("5000"),
            )
        )
        db.session.commit()
        return account.id


def _relationship_section(body: str) -> str:
    return body.split('id="relationship-minimums-heading"', 1)[1].split("</section>", 1)[0]


# --- Accounts index ---

def test_accounts_index_relationship_table_compact_states(
    client: FlaskClient, app: Flask
) -> None:
    _, institution_id, eligible_id, _ = _records(app)
    _rule(app, institution_id, eligible_id)
    body = client.get("/accounts?as_of=2026-08-28").get_data(as_text=True)
    section = _relationship_section(body)
    assert "<table" in section  # one compact row per institution, not prose
    for header in ("Eligible value", "Minimum", "Buffer", "Status"):
        assert f"<th scope=\"col\"" in section and header in section
    # Canonical case: EUR 100,000 at 0.85573 → GBP 85,573 vs GBP 75,000.
    # The institution cell is the row header, matching the accounts table.
    assert '<th scope="row">Example Bank</th>' in section
    row = section.split('<th scope="row">Example Bank</th>', 1)[1].split("</tr>", 1)[0]
    assert "85,573.00" in row and "75,000.00" in row and "10,573.00" in row
    assert "badge" not in row  # a met minimum is not a badge state
    assert 'href="/institutions/1/relationship?as_of=2026-08-28"' in row
    assert 'aria-label="Review relationship minimum for Example Bank"' in row
    other = section.split('<th scope="row">Other Bank</th>', 1)[1].split("</tr>", 1)[0]
    assert "No minimum recorded" in other
    assert 'aria-label="Add relationship minimum for Other Bank"' in other
    # Gross relationship value is stated as distinct from share/access.
    assert "portfolio share and present access do not reduce them" in section
    assert_no_inline_script(body)


def test_accounts_index_below_minimum_and_cannot_determine_badges(
    client: FlaskClient, app: Flask
) -> None:
    _, institution_id, eligible_id, _ = _records(app)
    _rule(app, institution_id, eligible_id, threshold="90000")
    body = client.get("/accounts?as_of=2026-08-28").get_data(as_text=True)
    row = _relationship_section(body).split('<th scope="row">Example Bank</th>', 1)[1]
    assert "Below minimum" in row
    assert "-4,427.00" in row  # buffer shown signed, not red

    # One eligible account still converts; the other cannot. The known
    # subtotal stays traceable and the missing side is never a false zero.
    gbp_id = _gbp_account(app, institution_id)
    _rule(app, institution_id, (eligible_id, gbp_id), threshold="90000")
    with app.app_context():
        db.session.query(FxRate).delete()
        db.session.commit()
    body = client.get("/accounts?as_of=2026-08-28").get_data(as_text=True)
    row = _relationship_section(body).split('<th scope="row">Example Bank</th>', 1)[1]
    assert "Cannot determine" in row
    assert "known" in row and "5,000.00" in row  # traceable known subtotal


# --- Account detail ---

def test_account_detail_relationship_card_states(
    client: FlaskClient, app: Flask
) -> None:
    _, institution_id, eligible_id, _ = _records(app)
    body = client.get(f"/accounts/{eligible_id}?as_of=2026-08-28").get_data(as_text=True)
    card = body.split('id="relationship-heading"', 1)[1].split("</section>", 1)[0]
    assert "No commercial relationship minimum is recorded" in card
    assert "Add relationship minimum" in card

    _rule(app, institution_id, eligible_id, threshold="90000")
    body = client.get(f"/accounts/{eligible_id}?as_of=2026-08-28").get_data(as_text=True)
    card = body.split('id="relationship-heading"', 1)[1].split("</section>", 1)[0]
    assert "85,573.00" in card and "90,000.00" in card
    assert "Below minimum" in card
    assert "Review relationship minimum" in card
    assert "full tracked account values" in card

    _rule(app, institution_id, eligible_id, threshold="75000")
    body = client.get(f"/accounts/{eligible_id}?as_of=2026-08-28").get_data(as_text=True)
    card = body.split('id="relationship-heading"', 1)[1].split("</section>", 1)[0]
    assert "badge" not in card  # met state carries no badge


# --- Relationship form ---

def test_relationship_form_labels_hints_and_keyboard_path(
    client: FlaskClient, app: Flask
) -> None:
    _, institution_id, eligible_id, _ = _records(app)
    body = client.get(
        f"/institutions/{institution_id}/relationship?as_of=2026-08-28"
    ).get_data(as_text=True)
    assert body.count("<h1") == 1 and "<h1>Relationship minimum</h1>" in body
    assert "does not confirm the" in body  # no contractual/protection claim
    for field in ("name", "threshold_amount", "threshold_currency_code",
                  "warning_buffer_amount", "notes"):
        assert f'<label for="{field}">' in body
    required = re.findall(r'<label for="(\w+)">[^<]*<span class="req">', body)
    assert required == ["name", "threshold_amount", "threshold_currency_code"]
    # Eligible accounts are a labelled checkbox group, required while active.
    assert 'id="eligible_account_ids"' in body
    assert "(required while active)" in body
    assert f'<label for="eligible-account-{eligible_id}">Eligible EUR account</label>' in body
    # The active toggle uses the shared linked-label checkbox pattern.
    assert '<label for="is_active">Use this relationship minimum</label>' in body
    assert "Save relationship minimum" in body
    assert 'href="/accounts?as_of=2026-08-28">Back to accounts</a>' in body
    assert '<datalist id="currency-codes">' in body
    assert_no_inline_script(body)


def test_relationship_form_error_summary_and_first_error_focus(
    client: FlaskClient, app: Flask
) -> None:
    _, institution_id, _, _ = _records(app)
    response = client.post(
        f"/institutions/{institution_id}/relationship?as_of=2026-08-28",
        data={
            "name": "",
            "threshold_amount": "75000",
            "threshold_currency_code": "GBP",
            "warning_buffer_amount": "",
            "is_active": "y",
            "notes": "",
        },
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    summary = re.search(
        r'<div class="error-summary" role="alert">.*?</div>', body, re.S
    ).group(0)
    assert 'href="#name"' in summary
    assert 'href="#eligible_account_ids"' in summary
    assert "Choose at least one eligible account" in summary
    name_field = re.search(r'<input[^>]*id="name"[^>]*>', body).group(0)
    assert 'aria-invalid="true"' in name_field and "autofocus" in name_field
    assert 'aria-describedby="eligible_account_ids-error"' in body
    assert_no_inline_script(body)


# --- Result states on the form page ---

def test_relationship_result_states_presented_without_advice(
    client: FlaskClient, app: Flask
) -> None:
    _, institution_id, eligible_id, _ = _records(app)
    url = f"/institutions/{institution_id}/relationship?as_of=2026-08-28"

    _rule(app, institution_id, eligible_id)
    body = client.get(url).get_data(as_text=True)
    result = body.split('id="relationship-result-heading"', 1)[1].split("</section>", 1)[0]
    assert "85,573.00" in result and "75,000.00" in result and "10,573.00" in result
    assert "The current eligible value meets the amount you entered." in result
    assert "Minimum met" not in result  # no OK badge
    assert "Eligible EUR account" in result and "latest source 27 Aug 2026" in result
    assert "Excluded EUR account" not in result

    _rule(app, institution_id, eligible_id, buffer="12000")
    body = client.get(url).get_data(as_text=True)
    assert "Inside preferred buffer" in body
    assert "the extra buffer is not" in body

    _rule(app, institution_id, eligible_id, threshold="90000")
    body = client.get(url).get_data(as_text=True)
    assert "Below minimum" in body
    assert "below the amount you entered" in body


def test_relationship_result_cannot_determine_shows_known_subtotal(
    client: FlaskClient, app: Flask
) -> None:
    _, institution_id, eligible_id, _ = _records(app)
    gbp_id = _gbp_account(app, institution_id)
    _rule(app, institution_id, (eligible_id, gbp_id))
    with app.app_context():
        db.session.query(FxRate).delete()
        db.session.commit()
    body = client.get(
        f"/institutions/{institution_id}/relationship?as_of=2026-08-28"
    ).get_data(as_text=True)
    result = body.split('id="relationship-result-heading"', 1)[1].split("</section>", 1)[0]
    assert "Cannot determine" in result
    assert "Known eligible subtotal" in result and "5,000.00" in result
    assert "never counted as zero" in result
    assert "Eligible EUR account" in result and "— incomplete value" in result
    assert "GBP account" in result
    # The exact missing input is named and linked to its targeted Values form
    #: the threshold-currency FX path, not a generic warning.
    assert "separate GBP relationship view" in result
    assert "Missing relationship FX" in result and "EUR → GBP" in result
    assert ('href="/values?base=EUR&amp;quote=GBP#fx-heading">Update →</a>'
            in result)


def test_relationship_result_stale_is_explicit(
    client: FlaskClient, app: Flask
) -> None:
    _, institution_id, eligible_id, _ = _records(app)
    _rule(app, institution_id, eligible_id)
    with app.app_context():
        rate = db.session.query(FxRate).one()
        rate.effective_date = date(2026, 8, 1)  # older than the 7-day FX setting
        db.session.commit()
    body = client.get(
        f"/institutions/{institution_id}/relationship?as_of=2026-08-28"
    ).get_data(as_text=True)
    result = body.split('id="relationship-result-heading"', 1)[1].split("</section>", 1)[0]
    assert "85,573.00" in result  # stale values still calculate
    assert "Stale" in result and "eligible values are stale" in result
    # The stale dependency is named with its source date and targeted link.
    assert "Stale relationship FX" in result
    assert "EUR → GBP" in result and "source 1 Aug 2026" in result
    assert ('href="/values?base=EUR&amp;quote=GBP#fx-heading">Update →</a>'
            in result)


# --- Overview attention ---

def test_overview_relationship_attention_structured_and_linked(
    client: FlaskClient, app: Flask
) -> None:
    _, institution_id, eligible_id, _ = _records(app)
    _rule(app, institution_id, eligible_id, threshold="90000")
    body = client.get("/overview?as_of=2026-08-28").get_data(as_text=True)
    attention = body.split('class="attention-list"', 1)[1].split("</ul>", 1)[0]
    assert "<strong>Relationship minimum</strong> — Example Bank" in attention
    assert "Below minimum" in attention
    assert "85,573.00" in attention and "90,000.00" in attention
    assert ('href="/institutions/1/relationship?as_of=2026-08-28">Review minimum →</a>'
            in attention)


def test_overview_met_minimum_creates_no_attention(
    client: FlaskClient, app: Flask
) -> None:
    _, institution_id, eligible_id, _ = _records(app)
    _rule(app, institution_id, eligible_id)
    body = client.get("/overview?as_of=2026-08-28").get_data(as_text=True)
    assert "Relationship minimum" not in body
