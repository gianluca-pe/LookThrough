"""Retirement projection presentation: chart/table agreement, capital/access, shortfalls, legacy goal and no-JavaScript recovery."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient

from app.extensions import db
from app.models import Account
from test_m5_retirement import (
    _assumptions,
    _cash,
    _portfolio_account,
    _position,
)
from test_overview_ui import assert_no_inline_script


CHART_SCRIPTS = (
    '<script src="/static/js/vendor/chart.umd.min.js" defer>',
    '<script src="/static/js/charts.js" defer>',
)


def _row(body: str, label: str) -> str:
    match = re.search(
        rf'<th scope="row">{re.escape(label)}.*?</tr>', body, re.DOTALL
    )
    assert match, f"row {label!r} not found"
    return match.group(0)


def test_hierarchy_outcome_before_annual_path_with_chart_and_table(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account(spending="30000", inflation="0.03")
        _cash(account, "1000000")
        _assumptions(portfolio, current=50, withdrawal=55, final=60)
        db.session.commit()

    body = client.get("/planning/retirement").get_data(as_text=True)
    assert body.index("Base-case outcome") < body.index("Annual path")
    assert "Projected capital at start" in body
    assert "included value less Projects" in body
    assert "First drawdown year" in body
    assert "Surplus / drawdown" in body
    assert 'data-chart="retirement"' in body
    assert 'aria-hidden="true"' in body
    labels = re.search(r'data-labels="([^"]+)"', body).group(1)
    for age in ("50", "55", "59"):
        assert f"&#34;{age}&#34;" in labels
    assert_no_inline_script(body, extra_scripts=CHART_SCRIPTS)
    # The table remains the full equivalent: every projected age is a row.
    for age in range(50, 60):
        _row(body, f"{age} ")
    assert "Inspect withdrawal sources" in body
    assert "Inspect allocation drift" in body
    assert "funded from Cash" in body


def test_no_assumptions_invites_entry_without_chart(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _cash(account, "1000")
        db.session.commit()

    body = client.get("/planning/retirement").get_data(as_text=True)
    assert "Enter and save the ages" in body
    assert "Annual path" not in body
    assert 'data-chart="retirement"' not in body
    assert_no_inline_script(body)


def test_first_shortfall_depletion_and_legacy_below_copy(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _cash(account, "150")
        _assumptions(
            portfolio,
            current=50,
            withdrawal=51,
            final=54,
            legacy=Decimal("25"),
        )
        db.session.commit()

    body = client.get("/planning/retirement").get_data(as_text=True)
    depletion_row = _row(body, "52 ")
    assert "Depleted" in depletion_row
    assert "First" in depletion_row
    assert "Below by" in body
    assert "25.00" in body
    assert "signed gap" not in body


def test_legacy_met_reads_above_target_by(app: Flask, client: FlaskClient) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _cash(account, "1000000")
        _assumptions(
            portfolio,
            current=50,
            withdrawal=55,
            final=60,
            legacy=Decimal("500000"),
        )
        db.session.commit()

    body = client.get("/planning/retirement").get_data(as_text=True)
    assert "Above by" in body
    assert "Target" in body


def test_incomplete_reasons_are_actionable(app: Flask, client: FlaskClient) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account(inflation=None)
        _cash(account, "0")
        _position(
            portfolio,
            account,
            name="Unclassified",
            amount="1000",
            bucket="growth",
            roles={},
        )
        _assumptions(portfolio)
        db.session.commit()

    body = client.get("/planning/retirement").get_data(as_text=True)
    assert "Incomplete" in body
    assert "withheld" in body
    assert 'href="/planning/funding"' in body
    assert 'href="/holdings"' in body
    assert "Annual path" not in body


def test_missing_spending_fx_targets_fx_section(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        portfolio.annual_spending_currency_code = "EUR"
        _cash(account, "1000")
        _assumptions(portfolio)
        db.session.commit()

    body = client.get("/planning/retirement").get_data(as_text=True)
    assert 'href="/values#routine-fx-heading"' in body


def test_drawdown_crossover_is_named_neutrally(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _cash(account, "1000")
        _assumptions(portfolio, current=50, withdrawal=51, final=53)
        db.session.commit()

    body = client.get("/planning/retirement").get_data(as_text=True)
    assert "First drawdown year" in body
    assert "2027 · age 51" in body
    assert '<span class="ccy">USD</span> -100.00' in body
    assert "not a spending shortfall" in body


def test_dated_restricted_capital_is_named_not_absent(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _cash(account, "1000")
        restricted = Account(
            portfolio_id=portfolio.id,
            institution_id=account.institution_id,
            name="Locked",
            account_type="retirement",
            default_currency_code="USD",
            is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("0"),
            earliest_access_date=date(2030, 1, 1),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add(restricted)
        db.session.flush()
        _position(
            portfolio,
            restricted,
            name="House project",
            amount="1000",
            bucket="projects",
            roles={"alternatives": "1"},
        )
        _cash(restricted, "500")
        _assumptions(portfolio, current=50, withdrawal=55, final=60)
        db.session.commit()

    body = client.get("/planning/retirement").get_data(as_text=True)
    assert "Projected capital at start" in body
    assert "1,500.00" in body
    assert (
        'Projects <span class="money"><span class="ccy">USD</span> '
        "1,000.00</span> are reserved outside this path"
    ) in body
    assert (
        'Currently restricted <span class="money"><span class="ccy">USD</span> '
        "500.00</span> remains in projected non-Project capital"
    ) in body
    assert "Restricted capital with future access" in body
    assert "spendable from age 54" in body
    assert "Locked · USD" in body


def test_negative_cash_is_disclosed_as_opening_obligation(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _cash(account, "-50")
        _position(
            portfolio,
            account,
            name="Growth",
            amount="100",
            bucket="growth",
            roles={"equity": "1"},
        )
        _assumptions(portfolio, current=50, withdrawal=51, final=52)
        db.session.commit()

    body = client.get("/planning/retirement").get_data(as_text=True)
    assert "Negative cash" in body
    assert "opening obligation" in body
