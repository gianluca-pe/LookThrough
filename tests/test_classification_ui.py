"""Classification editor, accessible simple/split inputs, Holdings columns and allocation denominators."""

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
    Posting,
    Price,
    Transaction,
)
from app.services.cash import CashConfirmationCommand, confirm_cash
from app.services.classification import ClassificationCommand, save_classification

from test_overview_ui import assert_no_inline_script


AS_OF = date(2026, 8, 9)


def _portfolio_account(app: Flask):
    with app.app_context():
        portfolio = Portfolio(
            name="Personal portfolio", reporting_currency_code="EUR",
            annual_spending_amount=Decimal("48000"),
            default_as_of_date=AS_OF,
        )
        db.session.add(portfolio)
        db.session.flush()
        institution = Institution(portfolio_id=portfolio.id, name="Bank")
        db.session.add(institution)
        db.session.flush()
        account = Account(
            portfolio_id=portfolio.id, institution_id=institution.id,
            name="Brokerage", account_type="brokerage",
            default_currency_code="EUR", is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"), present_access_decimal=Decimal("1"),
            relationship_eligible=True, is_active=True,
        )
        db.session.add(account)
        db.session.commit()
        return portfolio.id, account.id


def _priced_position(portfolio_id: int, account_id: int, *, name: str, amount: str) -> int:
    with db.session.begin():
        instrument = Instrument(
            portfolio_id=portfolio_id, name=name, instrument_type="fund",
            valuation_currency_code="EUR", is_active=True,
        )
        db.session.add(instrument)
        db.session.flush()
        registration = PositionRegistration(
            account_id=account_id, instrument_id=instrument.id,
            tracking_mode="transaction_tracked", opening_date=date(2026, 8, 1),
        )
        transaction = Transaction(
            portfolio_id=portfolio_id, transaction_type="opening_balance",
            effective_date=date(2026, 8, 1), status="posted",
        )
        db.session.add_all([registration, transaction])
        db.session.flush()
        db.session.add_all([
            Posting(
                transaction_id=transaction.id, account_id=account_id,
                posting_kind="instrument", instrument_id=instrument.id,
                currency_code="EUR", quantity_delta=Decimal("1"),
            ),
            Price(
                instrument_id=instrument.id, effective_date=date(2026, 8, 8),
                price_amount=Decimal(amount), currency_code="EUR",
            ),
        ])
        return instrument.id


def _confirm_cash(portfolio_id: int, account_id: int, amount: str) -> None:
    confirm_cash(
        CashConfirmationCommand(
            portfolio_id=portfolio_id, account_id=account_id,
            currency_code="EUR", effective_date=date(2026, 8, 8),
            confirmed_balance_amount=Decimal(amount),
        )
    )


def _classify(instrument_id: int, weights: dict, *, on=date(2026, 8, 8), bucket=None) -> None:
    portfolio_id = db.session.get(Instrument, instrument_id).portfolio_id
    save_classification(
        portfolio_id,
        ClassificationCommand(
            instrument_id=instrument_id, effective_date=on,
            role_weights={code: Decimal(w) for code, w in weights.items()},
            fire_bucket_code=bucket,
        ),
    )
    db.session.commit()


def _classification_data(**overrides: str) -> dict[str, str]:
    data = {
        "effective_date": "2026-08-08",
        "classification_mode": "simple",
        "primary_role_code": "equity",
        "fire_bucket_code": "",
        "source_note": "",
        "save_classification": "Save classification",
    }
    data.update(overrides)
    return data


# --- Instruments index and navigation ---

def test_index_lists_instruments_and_nav_is_linked(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        _priced_position(portfolio_id, account_id, name="Global Fund", amount="500")
    body = client.get("/instruments").get_data(as_text=True)
    assert "<h1>Instruments</h1>" in body
    assert '<th scope="row">Global Fund' in body
    assert '<span class="badge badge-missing">Unclassified</span>' in body
    assert 'href="/instruments/1/classification"' in body
    sidebar = body.split('<aside class="sidebar">', 1)[1].split("</aside>", 1)[0]
    assert '<a href="/instruments" aria-current="page">Instruments</a>' in sidebar
    assert "<th scope=\"col\">Hedging</th>" not in body
    assert_no_inline_script(body)


# --- Classification editor ---

def test_editor_simple_role_round_trips_as_exact_100(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        _priced_position(portfolio_id, account_id, name="Global Fund", amount="500")
    response = client.post("/instruments/1/classification", data=_classification_data())
    assert response.status_code == 302
    index = client.get("/instruments").get_data(as_text=True)
    row = index.split('<th scope="row">Global Fund', 1)[1].split("</tr>", 1)[0]
    assert "Equity" in row and "Equity 100%" not in row  # one role is exactly 100%
    assert "Effective 8 Aug 2026" in row
    editor = client.get("/instruments/1/classification").get_data(as_text=True)
    assert "Current classification" in editor and "Equity" in editor
    assert "Effective 8 Aug 2026" in editor


def test_editor_only_asks_for_used_inputs_and_preserves_legacy_metadata(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        instrument_id = _priced_position(
            portfolio_id, account_id, name="Legacy Details Fund", amount="500"
        )
        instrument = db.session.get(Instrument, instrument_id)
        instrument.fund_base_currency_code = "USD"
        instrument.hedging_status = "hedged"
        instrument.capital_certainty_code = "medium"
        instrument.equity_sensitivity_code = "sometimes"
        instrument.liquidity_profile_code = "days"
        instrument.duration_band_code = "intermediate"
        instrument.credit_band_code = "mixed"
        instrument.currency_treatment_code = "hedged_to_share_class"
        db.session.commit()

    editor = client.get(
        f"/instruments/{instrument_id}/classification"
    ).get_data(as_text=True)
    assert "The allocation role is used in Asset allocation" in editor
    assert "The FIRE bucket is used in Ten-year funding" in editor
    assert "Source or reasoning" in editor
    assert "Factsheet dated 31 Jul 2026" in editor
    assert "Fund base currency" not in editor
    for removed_label in (
        "Fund base currency",
        "Hedging status",
        "Capital certainty",
        "Equity sensitivity",
        "Duration band",
        "Credit band",
        "Currency treatment",
    ):
        assert removed_label not in editor

    response = client.post(
        f"/instruments/{instrument_id}/classification",
        data=_classification_data(
            fire_bucket_code="growth",
            source_note="Owner review",
            # A stale or tampered client cannot edit a removed input.
            hedging_status="unhedged",
        ),
    )
    assert response.status_code == 302

    with app.app_context():
        saved = db.session.get(Instrument, instrument_id)
        assert saved.fund_base_currency_code == "USD"
        assert saved.hedging_status == "hedged"
        assert saved.capital_certainty_code == "medium"
        assert saved.equity_sensitivity_code == "sometimes"
        assert saved.liquidity_profile_code == "days"
        assert saved.duration_band_code == "intermediate"
        assert saved.credit_band_code == "mixed"
        assert saved.currency_treatment_code == "hedged_to_share_class"


def test_editor_advanced_split_saves(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        _priced_position(portfolio_id, account_id, name="Balanced Fund", amount="500")
    response = client.post(
        "/instruments/1/classification",
        data=_classification_data(
            classification_mode="advanced", primary_role_code="",
            role_equity_percent="60", role_liquidity_percent="40",
        ),
    )
    assert response.status_code == 302
    index = client.get("/instruments").get_data(as_text=True)
    row = index.split('<th scope="row">Balanced Fund', 1)[1].split("</tr>", 1)[0]
    assert "Equity 60%" in row and "Liquidity 40%" in row


def test_editor_role_paths_and_definitions_are_complete_without_javascript(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        _priced_position(portfolio_id, account_id, name="Balanced Fund", amount="500")

    body = client.get("/instruments/1/classification").get_data(as_text=True)
    assert 'data-primary-role-section' in body
    assert '<fieldset class="field classification-role-split" data-advanced-roles>' in body
    assert '<details class="disclosure"' not in body
    assert "Allocation role definitions" in body
    assert "Cash, money-market funds, T-bills, and short fixed deposits." in body
    assert "Percentages must total 100%." in body
    assert "You may leave this unset." in body
    assert_no_inline_script(body)


def test_editor_advanced_total_90_rejected_linked_focused_atomic(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        _priced_position(portfolio_id, account_id, name="Balanced Fund", amount="500")
    response = client.post(
        "/instruments/1/classification",
        data=_classification_data(
            classification_mode="advanced", primary_role_code="",
            role_equity_percent="60", role_liquidity_percent="30",
        ),
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'href="#classification_mode"' in body
    assert "must total 100%" in body
    field = re.search(r'<select[^>]*id="classification_mode"[^>]*>', body).group(0)
    assert 'aria-invalid="true"' in field and "autofocus" in field
    assert '<fieldset class="field classification-role-split" data-advanced-roles>' in body
    # Not persisted: the index still shows the instrument as unclassified.
    index = client.get("/instruments").get_data(as_text=True)
    assert '<span class="badge badge-missing">Unclassified</span>' in index


def test_same_date_requires_explicit_replace(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        instrument_id = _priced_position(
            portfolio_id, account_id, name="Global Fund", amount="500"
        )
        _classify(instrument_id, {"equity": "1"})
    response = client.post(
        "/instruments/1/classification",
        data=_classification_data(primary_role_code="liquidity"),
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "already exists for this date" in body
    assert 'href="#replace_existing"' in body
    checkbox = re.search(r'<input[^>]*id="replace_existing"[^>]*>', body).group(0)
    assert 'aria-invalid="true"' in checkbox and "autofocus" in checkbox
    response = client.post(
        "/instruments/1/classification",
        data=_classification_data(primary_role_code="liquidity", replace_existing="y"),
    )
    assert response.status_code == 302
    index = client.get("/instruments").get_data(as_text=True)
    row = index.split('<th scope="row">Global Fund', 1)[1].split("</tr>", 1)[0]
    assert "Liquidity" in row and "Equity" not in row


def test_future_classification_hidden_from_earlier_as_of(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        instrument_id = _priced_position(
            portfolio_id, account_id, name="Global Fund", amount="500"
        )
        _classify(instrument_id, {"equity": "1"}, on=date(2026, 8, 10))
    early = client.get("/instruments?as_of=2026-08-09").get_data(as_text=True)
    assert '<span class="badge badge-missing">Unclassified</span>' in early
    later = client.get("/instruments?as_of=2026-08-10").get_data(as_text=True)
    assert "Equity" in later
    # And the earlier as-of Overview keeps the value outside role percentages.
    overview = client.get("/overview?as_of=2026-08-09").get_data(as_text=True)
    assert "Unclassified" in overview
    assert "Asset allocation" in overview
    assert "% of classified" not in overview  # nothing classified yet: no table


# --- Overview allocation cards ---

def _allocation_fixture(app: Flask) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        classified = _priced_position(
            portfolio_id, account_id, name="Classified Fund", amount="500"
        )
        _priced_position(portfolio_id, account_id, name="Other Fund", amount="250")
        _classify(
            classified, {"equity": "0.6", "liquidity": "0.4"}, bucket="now"
        )
        _confirm_cash(portfolio_id, account_id, "100")


def test_overview_role_allocation_exact_denominator_and_unclassified(
    client: FlaskClient, app: Flask
) -> None:
    _allocation_fixture(app)
    body = client.get("/overview?as_of=2026-08-09").get_data(as_text=True)
    card = body.split('id="role-allocation-heading"', 1)[1].split("</section>", 1)[0]
    # 500 × 60/40 = 300/200; cash 100 lands in Liquidity; denominator 600.
    equity = card.split("<td>Equity</td>", 1)[1].split("</tr>", 1)[0]
    assert "300.00" in equity and "50.0%" in equity
    liquidity = card.split("<td>Liquidity</td>", 1)[1].split("</tr>", 1)[0]
    assert "300.00" in liquidity and "50.0%" in liquidity
    assert "classified included value" in card and "600.00" in card
    # The 250 unclassified investment stays visible outside the denominator.
    assert "Unclassified" in card and "250.00" in card
    assert 'href="/instruments"' in card
    # The Total row foots to the headline included value (600 classified
    # + 250 unclassified) and the percentages column closes at 100.0%.
    total = card.split('<th scope="row">Total</th>', 1)[1].split("</tr>", 1)[0]
    assert total.count("850.00") == 2
    assert "100.0%" in total


def test_overview_bucket_card_excludes_unbucketed_and_discloses_cash(
    client: FlaskClient, app: Flask
) -> None:
    _allocation_fixture(app)
    body = client.get("/overview?as_of=2026-08-09").get_data(as_text=True)
    card = body.split('id="bucket-allocation-heading"', 1)[1].split("</section>", 1)[0]
    now = card.split("<td>Now (0–3 years)</td>", 1)[1].split("</tr>", 1)[0]
    assert "500.00" in now and "100.0%" in now
    assert "No bucket set:" in card and "250.00" in card
    # Cash is a first-class row: no assigned horizon, outside the bucket percentages.
    cash = card.split("<td>Cash</td>", 1)[1].split("</tr>", 1)[0]
    assert "100.00" in cash
    assert "no manually assigned horizon" in card
    # The Total row foots: rows 600.00 + the unbucketed 250.00 disclosed
    # below equal the headline included 850.00, and accessible matches too.
    total = card.split('<th scope="row">Total</th>', 1)[1].split("</tr>", 1)[0]
    assert total.count("850.00") == 2
    # The bucketed percentages are of the bucket rows only, so they foot
    # to exactly 100.0% even though the displayed rows may not.
    assert "100.0%" in total


def test_overview_zero_denominator_never_renders_zero_allocation(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        _priced_position(portfolio_id, account_id, name="Other Fund", amount="250")
    body = client.get("/overview?as_of=2026-08-09").get_data(as_text=True)
    card = body.split('id="role-allocation-heading"', 1)[1].split("</section>", 1)[0]
    assert "<table" not in card and "0.0%" not in card
    assert "Unclassified" in card and "250.00" in card


def test_overview_missing_value_produces_no_allocation_card(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        instrument = Instrument(
            portfolio_id=portfolio_id, name="Unpriced", instrument_type="fund",
            valuation_currency_code="EUR", is_active=True,
        )
        db.session.add(instrument)
        db.session.flush()
        db.session.add(PositionRegistration(
            account_id=account_id, instrument_id=instrument.id,
            tracking_mode="statement_valued", opening_date=date(2026, 8, 1),
        ))
        db.session.commit()
    body = client.get("/overview?as_of=2026-08-09").get_data(as_text=True)
    assert 'id="role-allocation-heading"' not in body
    assert 'id="bucket-allocation-heading"' not in body


# --- Holdings columns ---

def test_holdings_role_bucket_columns_and_classify_links(
    client: FlaskClient, app: Flask
) -> None:
    _allocation_fixture(app)
    body = client.get("/holdings?as_of=2026-08-09").get_data(as_text=True)
    classified = body.split('<th scope="row">Classified Fund</th>', 1)[1].split("</tr>", 1)[0]
    assert "Equity 60%" in classified and "Liquidity 40%" in classified
    assert "Now (0–3 years)" in classified
    other = body.split('<th scope="row">Other Fund</th>', 1)[1].split("</tr>", 1)[0]
    assert "Unclassified" in other
    assert 'aria-label="Classify Other Fund"' in other
    assert 'href="/instruments/2/classification"' in other
    assert_no_inline_script(body)
