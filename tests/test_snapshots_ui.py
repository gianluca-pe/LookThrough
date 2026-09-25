"""Snapshot presentation: signed changes, reconciliation, overlays and no-JavaScript save."""

from __future__ import annotations

from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient

from app.extensions import db
from app.models import Portfolio, PortfolioSnapshot, Posting, Transaction
from app.services.snapshots import save_snapshot
from test_snapshots import FIRST, SECOND, _add_second_date, _seed
from test_overview_ui import assert_no_inline_script


def _two_snapshots(app: Flask) -> None:
    ids = _seed(app)
    with app.app_context():
        save_snapshot(db.session.get(Portfolio, ids["portfolio_id"]), FIRST)
    _add_second_date(app, ids)
    with app.app_context():
        save_snapshot(db.session.get(Portfolio, ids["portfolio_id"]), SECOND)


def _latest_detail(client: FlaskClient, app: Flask) -> str:
    with app.app_context():
        latest = (
            db.session.query(PortfolioSnapshot)
            .order_by(PortfolioSnapshot.as_of_date.desc())
            .first()
        )
        snapshot_id = latest.id
    return client.get(f"/snapshots/{snapshot_id}").get_data(as_text=True)


def test_detail_shows_frozen_receipt_and_signed_comparison(
    app: Flask, client: FlaskClient
) -> None:
    _two_snapshots(app)
    body = _latest_detail(client, app)

    assert body.index("Frozen at save") < body.index("Since 1 Jan 2026")
    # Frozen totals and composition mirror the overview cards.
    assert "Gross tracked value" in body
    assert "Included portfolio value" in body
    assert "Accessible now" in body
    assert "Growth (10+ years)" in body
    # Signed deltas: +100 gross change, +50 external flow, +30 correction,
    # +20 unattributed — direction shown by sign, never by colour.
    assert '<span class="ccy">USD</span> +100.00' in body
    assert '<span class="ccy">USD</span> +50.00' in body
    assert '<span class="ccy">USD</span> +30.00' in body
    assert '<span class="ccy">USD</span> +20.00' in body
    # Planning overlays are separate from the reconciliation equation.
    assert body.index("Unattributed value change") < body.index("Planning overlay")
    assert "Included value change" in body
    assert "Accessible value change" in body
    assert "never netted above" in body
    assert "not a return calculation" in body
    assert_no_inline_script(body)


def test_zero_change_has_no_sign_and_no_colour(
    app: Flask, client: FlaskClient
) -> None:
    ids = _seed(app)
    with app.app_context():
        save_snapshot(db.session.get(Portfolio, ids["portfolio_id"]), FIRST)
    with app.app_context():
        save_snapshot(db.session.get(Portfolio, ids["portfolio_id"]), SECOND)
    body = _latest_detail(client, app)
    assert '<span class="ccy">USD</span> 0.00' in body
    assert "+0.00" not in body



def test_partial_comparison_is_local_and_labelled(
    app: Flask, client: FlaskClient
) -> None:
    ids = _seed(app)
    with app.app_context():
        save_snapshot(db.session.get(Portfolio, ids["portfolio_id"]), FIRST)
        deposit = Transaction(
            portfolio_id=ids["portfolio_id"],
            transaction_type="deposit",
            effective_date=SECOND,
            status="posted",
        )
        db.session.add(deposit)
        db.session.flush()
        db.session.add(
            Posting(
                transaction_id=deposit.id,
                account_id=ids["account_id"],
                posting_kind="cash",
                currency_code="EUR",  # no EUR→USD rate: movement FX missing
                cash_amount_delta=Decimal("10"),
            )
        )
        db.session.commit()
    with app.app_context():
        save_snapshot(db.session.get(Portfolio, ids["portfolio_id"]), SECOND)
    body = _latest_detail(client, app)
    assert "Partial" in body
    assert "withheld" in body
    assert "incomplete" in body



def test_save_preview_marks_state_and_requires_confirmation(
    app: Flask, client: FlaskClient
) -> None:
    _seed(app)
    body = client.get("/snapshots/new").get_data(as_text=True)
    assert "What will be frozen" in body
    assert "Included portfolio value" in body
    assert "Accessible now" in body
    assert_no_inline_script(body)

    refused = client.post("/snapshots/new", data={"as_of_date": FIRST.isoformat()})
    assert refused.status_code == 400
    refused_body = refused.get_data(as_text=True)
    assert "Confirm before saving" in refused_body
    assert 'href="#confirm_snapshot"' in refused_body
    assert "autofocus" in refused_body
