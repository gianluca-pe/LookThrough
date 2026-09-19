"""Integration contract for the server-rendered Buy/Sell workflow."""

import re
from datetime import date
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    Account,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    Posting,
    Transaction,
)


def _records():
    portfolio = Portfolio(
        name="Personal portfolio",
        reporting_currency_code="EUR",
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
        portfolio_id=portfolio.id,
        name="Global Fund",
        ticker_or_isin="FUND123",
        instrument_type="fund",
        valuation_currency_code="EUR",
        is_active=True,
    )
    db.session.add_all([account, instrument])
    db.session.commit()
    return portfolio, account, instrument


def _trade_data(account_id: int, instrument_id: int, **overrides):
    data = {
        "activity_type": "buy",
        "effective_date": "2026-08-04",
        "account_id": account_id,
        "instrument_id": instrument_id,
        "quantity": "1000",
        "unit_price": "10.25",
        "fee_amount": "25",
    }
    data.update(overrides)
    return data


def test_add_activity_redirects_to_setup_without_portfolio(client: FlaskClient) -> None:
    response = client.get("/activity/new")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/setup")


def test_activity_chooser_and_compact_trade_form(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _records()
    app.config["CURRENT_DATE_PROVIDER"] = lambda: date(2026, 8, 4)

    chooser = client.get("/activity/new").get_data(as_text=True)
    assert "<h1>Add activity</h1>" in chooser
    assert 'href="/activity/new?type=buy"' in chooser
    assert 'href="/activity/new?type=sell"' in chooser
    assert "Transfer / FX" in chooser and 'aria-disabled="true"' in chooser

    body = client.get("/activity/new?type=buy").get_data(as_text=True)
    for field_id in (
        "effective_date",
        "account_id",
        "instrument_id",
        "quantity",
        "unit_price",
        "fee_amount",
    ):
        assert f'id="{field_id}"' in body
        assert f'for="{field_id}"' in body
    assert 'value="2026-08-04"' in body
    assert "Settlement currency" not in body
    assert 'formaction="/activity/preview"' in body


def test_preview_renders_exact_effects_without_writing(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, account, instrument = _records()
        account_id, instrument_id = account.id, instrument.id

    response = client.post(
        "/activity/preview", data=_trade_data(account_id, instrument_id)
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Buys" in body
    assert "1,000 units" in body
    assert "10,250.00" in body
    assert "25.00" in body
    assert "-10,275.00" in body
    assert "Quantity after:" in body
    with app.app_context():
        assert db.session.scalar(select(func.count(Transaction.id))) == 0
        assert db.session.scalar(select(func.count(Posting.id))) == 0


def test_post_redirects_to_success_and_persists_all_effects(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, account, instrument = _records()
        account_id, instrument_id = account.id, instrument.id

    response = client.post(
        "/activity/new", data=_trade_data(account_id, instrument_id)
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/activity/1/success")

    success = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Buy recorded" in success
    assert "Bought" in success
    assert "1,000 units" in success
    assert "-10,275.00" in success
    assert "Add another" in success
    assert "View Holdings" in success

    with app.app_context():
        assert db.session.scalar(select(func.count(Transaction.id))) == 1
        assert db.session.scalar(select(func.count(Posting.id))) == 4
        assert db.session.scalar(select(func.count(PositionRegistration.id))) == 1


def test_route_oversell_error_is_linked_to_quantity_and_creates_nothing_new(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, account, instrument = _records()
        account_id, instrument_id = account.id, instrument.id
    client.post("/activity/new", data=_trade_data(account_id, instrument_id))

    response = client.post(
        "/activity/new",
        data=_trade_data(
            account_id,
            instrument_id,
            activity_type="sell",
            quantity="1001",
            unit_price="11.10",
            fee_amount="20",
        ),
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Only 1000" in body
    assert 'href="#quantity"' in body
    quantity_tag = re.search(r'<input[^>]*id="quantity"[^>]*>', body).group(0)
    assert "autofocus" in quantity_tag
    with app.app_context():
        assert db.session.scalar(select(func.count(Transaction.id))) == 1


def test_sell_route_updates_holdings_quantity_through_existing_replay_path(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, account, instrument = _records()
        account_id, instrument_id = account.id, instrument.id
    client.post("/activity/new", data=_trade_data(account_id, instrument_id))

    sale = client.post(
        "/activity/new",
        data=_trade_data(
            account_id,
            instrument_id,
            activity_type="sell",
            quantity="300",
            unit_price="11.10",
            fee_amount="20",
        ),
    )
    assert sale.status_code == 302
    success = client.get(sale.headers["Location"]).get_data(as_text=True)
    assert "Sell recorded" in success
    assert "3,310.00" in success

    with app.app_context():
        postings = list(
            db.session.scalars(
                select(Posting)
                .where(Posting.posting_kind == "instrument")
                .order_by(Posting.id)
            )
        )
        assert [row.quantity_delta for row in postings] == [
            Decimal("1000.000000000000"),
            Decimal("-300.000000000000"),
        ]


def test_sell_entire_holding_route_needs_no_typed_quantity(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, account, instrument = _records()
        account_id, instrument_id = account.id, instrument.id
    client.post("/activity/new", data=_trade_data(account_id, instrument_id))
    sell_all = _trade_data(
        account_id,
        instrument_id,
        activity_type="sell",
        quantity_mode="entire_holding",
        quantity="",
        unit_price="11.10",
        fee_amount="20",
    )

    preview = client.post("/activity/preview", data=sell_all)
    preview_body = preview.get_data(as_text=True)

    assert preview.status_code == 200
    assert "Sells" in preview_body
    assert "1,000 units" in preview_body
    assert "Quantity after: <strong>0 units</strong>" in preview_body

    response = client.post("/activity/new", data=sell_all)
    assert response.status_code == 302
    success = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Sold" in success and "1,000 units" in success

    with app.app_context():
        postings = list(
            db.session.scalars(
                select(Posting)
                .where(Posting.posting_kind == "instrument")
                .order_by(Posting.id)
            )
        )
        assert [row.quantity_delta for row in postings] == [
            Decimal("1000.000000000000"),
            Decimal("-1000.000000000000"),
        ]


def test_global_add_activity_action_is_now_a_real_link(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _records()
    body = client.get("/activity").get_data(as_text=True)
    assert '<a class="btn btn-primary" href="/activity/new">+ Add activity</a>' in body
