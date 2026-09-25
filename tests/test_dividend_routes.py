"""Integration contract for server-rendered dividend outcomes."""

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
    source = Instrument(
        portfolio_id=portfolio.id,
        name="Income Fund",
        ticker_or_isin="INC1",
        instrument_type="fund",
        valuation_currency_code="EUR",
        is_active=True,
    )
    target = Instrument(
        portfolio_id=portfolio.id,
        name="Bond ETF",
        ticker_or_isin="BOND1",
        instrument_type="etf",
        valuation_currency_code="EUR",
        is_active=True,
    )
    db.session.add_all([account, source, target])
    db.session.flush()
    db.session.add(
        PositionRegistration(
            account_id=account.id,
            instrument_id=source.id,
            tracking_mode="transaction_tracked",
            opening_date=date(2026, 1, 1),
        )
    )
    db.session.commit()
    return portfolio, account, source, target


def _data(account_id: int, source_id: int, **overrides: object):
    data: dict[str, object] = {
        "effective_date": "2026-08-04",
        "account_id": account_id,
        "instrument_id": source_id,
        "currency_code": "EUR",
        "net_amount": "425",
        "outcome": "cash",
        "gross_amount": "500",
        "withholding_amount": "75",
        "reinvestment_instrument_id": "0",
        "reinvestment_quantity": "",
        "reinvestment_unit_price": "",
        "reinvestment_purchase_amount": "",
        "reinvestment_fee_amount": "",
    }
    data.update(overrides)
    return data




def test_cash_dividend_preview_is_exact_and_does_not_write(
    app: Flask,
    client: FlaskClient,
) -> None:
    with app.app_context():
        _, account, source, _ = _records()
        account_id, source_id = account.id, source.id

    response = client.post(
        "/activity/dividend/preview",
        data=_data(account_id, source_id),
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "500.00" in body
    assert "75.00" in body
    assert "425.00" in body
    assert "Net account cash effect" in body
    with app.app_context():
        assert db.session.scalar(select(func.count(Transaction.id))) == 0
        assert db.session.scalar(select(func.count(Posting.id))) == 0


def test_cash_dividend_posts_and_redirects_to_persisted_receipt(
    app: Flask,
    client: FlaskClient,
) -> None:
    with app.app_context():
        _, account, source, _ = _records()
        account_id, source_id = account.id, source.id

    response = client.post(
        "/activity/dividend",
        data=_data(account_id, source_id),
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/activity/dividend/1/success")
    receipt = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Dividend recorded" in receipt
    assert "Income Fund" in receipt
    assert "500.00" in receipt
    assert "75.00" in receipt
    assert "425.00" in receipt
    assert "Linked Buy" not in receipt

    with app.app_context():
        assert db.session.scalar(select(func.count(Transaction.id))) == 1
        assert db.session.scalar(select(func.count(Posting.id))) == 3


def test_dividend_reconciliation_error_links_and_focuses_net_amount(
    app: Flask,
    client: FlaskClient,
) -> None:
    with app.app_context():
        _, account, source, _ = _records()
        account_id, source_id = account.id, source.id

    response = client.post(
        "/activity/dividend",
        data=_data(account_id, source_id, withholding_amount="74"),
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Gross distribution minus entered withholding" in body
    assert 'href="#net_amount"' in body
    net_tag = re.search(r'<input[^>]*id="net_amount"[^>]*>', body).group(0)
    assert "autofocus" in net_tag
    assert 'aria-invalid="true"' in net_tag
    with app.app_context():
        assert db.session.scalar(select(func.count(Transaction.id))) == 0


def test_reinvest_same_route_posts_linked_events_and_zero_cash(
    app: Flask,
    client: FlaskClient,
) -> None:
    with app.app_context():
        _, account, source, _ = _records()
        account_id, source_id = account.id, source.id

    response = client.post(
        "/activity/dividend",
        data=_data(
            account_id,
            source_id,
            outcome="reinvest_same",
            reinvestment_quantity="40",
            reinvestment_unit_price="10.60",
            reinvestment_fee_amount="1",
        ),
    )
    assert response.status_code == 302
    receipt = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Linked Buy" in receipt
    assert "40 units" in receipt
    assert "424.00" in receipt
    assert "1.00" in receipt
    assert "Net account cash effect" in receipt and "0.00" in receipt

    with app.app_context():
        transactions = list(db.session.scalars(select(Transaction).order_by(Transaction.id)))
        assert [row.transaction_type for row in transactions] == ["dividend", "buy"]
        assert transactions[0].activity_group_id == transactions[1].activity_group_id
        assert transactions[0].activity_group_id is not None


def test_reinvest_other_route_uses_selected_instrument_and_purchase_amount(
    app: Flask,
    client: FlaskClient,
) -> None:
    with app.app_context():
        _, account, source, target = _records()
        account_id, source_id, target_id = account.id, source.id, target.id

    response = client.post(
        "/activity/dividend",
        data=_data(
            account_id,
            source_id,
            outcome="reinvest_other",
            reinvestment_instrument_id=str(target_id),
            reinvestment_quantity="3",
            reinvestment_purchase_amount="100",
            gross_amount="",
            withholding_amount="",
        ),
    )
    receipt = client.get(response.headers["Location"]).get_data(as_text=True)
    assert response.status_code == 302
    assert "Bond ETF" in receipt
    assert "100.00" in receipt
    assert "325.00" in receipt


def test_missing_reinvestment_values_focus_first_material_error(
    app: Flask,
    client: FlaskClient,
) -> None:
    with app.app_context():
        _, account, source, _ = _records()
        account_id, source_id = account.id, source.id

    response = client.post(
        "/activity/dividend",
        data=_data(account_id, source_id, outcome="reinvest_same"),
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Additional units are required" in body
    assert 'href="#reinvestment_quantity"' in body
    quantity_tag = re.search(
        r'<input[^>]*id="reinvestment_quantity"[^>]*>', body
    ).group(0)
    assert "autofocus" in quantity_tag
    with app.app_context():
        assert db.session.scalar(select(func.count(Transaction.id))) == 0
