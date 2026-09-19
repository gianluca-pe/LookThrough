"""Allocation target forms and actual-versus-target Overview presentation."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient

from app.extensions import db
from app.models import (
    Account,
    CashBalanceCheckpoint,
    Institution,
    Instrument,
    InstrumentClassification,
    Portfolio,
    PositionRegistration,
    ValuationObservation,
)
from app.services.planning import save_allocation_targets


AS_OF = date(2026, 8, 29)

TARGETS = {
    "equity": (Decimal("0.70"), Decimal("0.80")),
    "income": (Decimal("0"), Decimal("0.10")),
    "liquidity": (Decimal("0.10"), Decimal("0.20")),
    "alternatives": (Decimal("0"), Decimal("0.10")),
}


def _portfolio_account() -> tuple[Portfolio, Account]:
    portfolio = Portfolio(
        name="Planning portfolio",
        reporting_currency_code="EUR",
        annual_spending_amount=Decimal("40000"),
        annual_spending_currency_code="EUR",
    )
    db.session.add(portfolio)
    db.session.flush()
    institution = Institution(portfolio_id=portfolio.id, name="Bank")
    db.session.add(institution)
    db.session.flush()
    account = Account(
        portfolio_id=portfolio.id,
        institution_id=institution.id,
        name="Account",
        account_type="brokerage",
        default_currency_code="EUR",
        is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        is_active=True,
    )
    db.session.add(account)
    db.session.flush()
    return portfolio, account


def _classified_statement_position(
    portfolio: Portfolio,
    account: Account,
    *,
    amount: str,
    role: str,
    bucket: str,
) -> Instrument:
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name="Fund",
        instrument_type="fund",
        valuation_currency_code="EUR",
        fire_bucket_code=bucket,
        is_active=True,
    )
    db.session.add(instrument)
    db.session.flush()
    registration = PositionRegistration(
        account_id=account.id,
        instrument_id=instrument.id,
        tracking_mode="statement_valued",
        opening_date=date(2026, 8, 1),
    )
    db.session.add(registration)
    db.session.flush()
    db.session.add(
        ValuationObservation(
            position_registration_id=registration.id,
            effective_date=AS_OF,
            native_value_amount=Decimal(amount),
            currency_code="EUR",
        )
    )
    db.session.add(
        InstrumentClassification(
            instrument_id=instrument.id,
            economic_role_code=role,
            weight_decimal=Decimal("1"),
            effective_date=AS_OF,
        )
    )
    return instrument


def _seed_complete(portfolio: Portfolio, account: Account) -> None:
    """Equity EUR 600 + confirmed cash EUR 400: 60/40 against the targets."""
    _classified_statement_position(
        portfolio, account, amount="600", role="equity", bucket="projects"
    )
    db.session.add(
        CashBalanceCheckpoint(
            account_id=account.id,
            currency_code="EUR",
            effective_date=AS_OF,
            confirmed_balance_amount=Decimal("400"),
        )
    )
    save_allocation_targets(portfolio.id, TARGETS)
    db.session.commit()


def test_overview_shows_exact_target_gaps_when_complete(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _seed_complete(portfolio, account)
    body = client.get("/overview").get_data(as_text=True)
    assert "Target range" in body
    assert "70.0–80.0%" in body
    equity_row = body.split(">Equity<", 1)[1].split("</tr>", 1)[0]
    assert "Below" in equity_row
    assert "100.00" in equity_row
    assert "10.0 pt" in equity_row
    liquidity_row = body.split(">Liquidity<", 1)[1].split("</tr>", 1)[0]
    assert "Above" in liquidity_row
    assert "200.00" in liquidity_row
    # A zero-value role still shows because its within-range result is real.
    income_row = body.split(">Income<", 1)[1].split("</tr>", 1)[0]
    assert "Within range" in income_row
    assert "not advice" in body
    # Chart island mirrors the table, nothing recomputed.
    assert 'data-chart="allocation"' in body
    assert "&#34;Equity&#34;" in body and "&#34;Alternatives&#34;" in body
    assert 'data-actual="[60.0, 0.0, 40.0, 0.0]"' in body
    assert 'data-target-min="[70.0, 0.0, 10.0, 0.0]"' in body
    assert 'data-target-max="[80.0, 10.0, 20.0, 10.0]"' in body
    # The Total row foots to the headline included value (600 + 400 cash)
    # and leaves the per-role target cells empty — ranges do not sum.
    role_card = body.split('id="role-allocation-heading"', 1)[1].split("</section>", 1)[0]
    total_row = role_card.split('<th scope="row">Total</th>', 1)[1].split("</tr>", 1)[0]
    assert total_row.count("1,000.00") == 2
    assert "100.0%" in total_row
    assert total_row.count(">—</td>") == 2


def test_overview_withholds_results_while_classification_incomplete(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        instrument = _classified_statement_position(
            portfolio, account, amount="600", role="equity", bucket="growth"
        )
        db.session.delete(
            db.session.query(InstrumentClassification)
            .filter_by(instrument_id=instrument.id)
            .one()
        )
        save_allocation_targets(portfolio.id, TARGETS)
        db.session.add(
            CashBalanceCheckpoint(
                account_id=account.id,
                currency_code="EUR",
                effective_date=AS_OF,
                confirmed_balance_amount=Decimal("400"),
            )
        )
        db.session.commit()
    body = client.get("/overview").get_data(as_text=True)
    assert "Target range" in body  # entered targets remain visible facts
    assert "Within range" not in body
    assert ">Below<" not in body
    assert "withheld" in body
    assert "no result is invented from partial data" in body
    # Withheld comparison suppresses the chart bands too.
    assert "data-target-min" not in body


def test_overview_links_to_targets_when_none_set(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _classified_statement_position(
            portfolio, account, amount="600", role="equity", bucket="growth"
        )
        db.session.commit()
    body = client.get("/overview").get_data(as_text=True)
    assert "Target range" not in body
    assert "data-target-min" not in body  # no invented bands without targets
    assert 'data-chart="allocation"' in body
    assert "Set target ranges →" in body
    assert 'href="/planning/targets"' in body


def test_overview_bucket_card_explains_projects_and_horizons(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _seed_complete(portfolio, account)
    body = client.get("/overview").get_data(as_text=True)
    assert "funding horizon, not a spending allowance" in body
    assert "excluded from ten-year funding" in body
    # All four buckets are a fixed vocabulary: an empty Now row is stated,
    # not hidden (the empty near-term horizon is the signal).
    bucket_card = body.split('aria-labelledby="bucket-allocation-heading"', 1)[1]
    # Cash is a visible row above the buckets, outside their percentages.
    cash_row = bucket_card.split("<td>Cash</td>", 1)[1].split("</tr>", 1)[0]
    assert "400.00" in cash_row
    assert "no manually assigned horizon" in bucket_card
    assert "Now (0–3 years)" in bucket_card
    now_row = bucket_card.split(">Now (0–3 years)<", 1)[1].split("</tr>", 1)[0]
    assert "0.00" in now_row
    # The Total row foots to the headline included value (600 + 400 = 1,000).
    total_row = bucket_card.split('<th scope="row">Total</th>', 1)[1].split("</tr>", 1)[0]
    assert total_row.count("1,000.00") == 2


def test_targets_form_groups_ranges_under_role_legends(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _portfolio_account()
        db.session.commit()
    body = client.get("/planning/targets").get_data(as_text=True)
    assert "<legend>Equity</legend>" in body
    assert "<legend>Liquidity</legend>" in body
    assert "Minimum (%)" in body
    assert "Maximum (%)" in body
    assert "not recommendations" in body
    assert 'href="/settings"' in body


def test_targets_form_rejects_infeasible_ranges_with_linked_focus(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _portfolio_account()
        db.session.commit()
    data = {
        "equity_minimum_percent": "50",
        "equity_maximum_percent": "70",
        "income_minimum_percent": "60",
        "income_maximum_percent": "70",
        "liquidity_minimum_percent": "10",
        "liquidity_maximum_percent": "30",
        "alternatives_minimum_percent": "0",
        "alternatives_maximum_percent": "10",
        "save_targets": "Save target ranges",
    }
    response = client.post("/planning/targets", data=data)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "must allow a complete 100% allocation" in body
    assert 'href="#equity_minimum_percent"' in body
    tag = re.search(r'<input[^>]*id="equity_minimum_percent"[^>]*>', body)
    assert tag is not None and "autofocus" in tag.group(0)

    swapped = dict(data, equity_minimum_percent="80", equity_maximum_percent="70")
    response = client.post("/planning/targets", data=swapped)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Maximum must be greater than or equal to minimum." in body
    assert 'href="#equity_maximum_percent"' in body
