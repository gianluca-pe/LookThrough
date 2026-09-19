"""Integration contract for account cash and confirmation routes."""

import re
from datetime import date
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    Account,
    CashBalanceCheckpoint,
    Institution,
    Instrument,
    Portfolio,
    Posting,
    Transaction,
)


def _records() -> tuple[Portfolio, Account, Account, Instrument]:
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
    separate = Account(
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
    included = Account(
        portfolio_id=portfolio.id,
        institution_id=institution.id,
        name="Pension wrapper",
        account_type="retirement",
        default_currency_code="EUR",
        is_multicurrency=False,
        cash_tracking_mode="included_in_aggregate",
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("0"),
        relationship_eligible=False,
        is_active=True,
    )
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name="Global Fund",
        instrument_type="fund",
        valuation_currency_code="EUR",
        is_active=True,
    )
    db.session.add_all([separate, included, instrument])
    db.session.commit()
    return portfolio, separate, included, instrument


def _cash_effect(
    portfolio: Portfolio,
    account: Account,
    *,
    on: date,
    amount: str,
) -> None:
    transaction = Transaction(
        portfolio_id=portfolio.id,
        transaction_type="cash_adjustment",
        effective_date=on,
        status="posted",
    )
    db.session.add(transaction)
    db.session.flush()
    db.session.add(
        Posting(
            transaction_id=transaction.id,
            account_id=account.id,
            posting_kind="cash",
            currency_code="EUR",
            cash_amount_delta=Decimal(amount),
        )
    )
    db.session.commit()


def _confirmation_data(**overrides: str) -> dict[str, str]:
    data = {
        "currency_code": "EUR",
        "effective_date": "2026-06-30",
        "confirmed_balance_amount": "17950",
        "source_note": "June broker statement",
    }
    data.update(overrides)
    return data


def test_accounts_redirects_to_setup_without_portfolio(client: FlaskClient) -> None:
    response = client.get("/accounts")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/setup")


def test_confirmation_flow_reconciles_without_creating_activity(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, separate, _, _ = _records()
        _cash_effect(portfolio, separate, on=date(2026, 6, 1), amount="18400")
        account_id = separate.id
        transaction_count = db.session.scalar(select(func.count(Transaction.id)))
        posting_count = db.session.scalar(select(func.count(Posting.id)))

    app.config["CURRENT_DATE_PROVIDER"] = lambda: date(2026, 6, 30)
    form = client.get(
        f"/accounts/{account_id}/cash-confirmations/new"
    ).get_data(as_text=True)
    assert "Set current cash balance" in form
    assert "18,400.00" in form
    assert 'value="2026-06-30"' in form
    assert "Treated as end-of-day" in form
    assert 'name="category"' not in form
    assert 'name="explanation"' not in form

    response = client.post(
        f"/accounts/{account_id}/cash-confirmations",
        data=_confirmation_data(),
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/accounts/{account_id}?currency=EUR")

    detail = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Cash confirmation saved" in detail
    assert "17,950.00" in detail
    assert "18,400.00" in detail
    assert "-450.00" in detail
    assert "not a deposit, withdrawal, income, expense, gain, or loss" in detail

    with app.app_context():
        checkpoint = db.session.scalar(select(CashBalanceCheckpoint))
        assert checkpoint is not None
        assert checkpoint.confirmed_balance_amount == Decimal("17950")
        assert checkpoint.correction_amount == Decimal("-450")
        assert db.session.scalar(select(func.count(Transaction.id))) == transaction_count
        assert db.session.scalar(select(func.count(Posting.id))) == posting_count


def test_setup_cash_continuation_returns_to_account_setup(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, separate, _, _ = _records()
        account_id = separate.id

    form = client.get(
        f"/accounts/{account_id}/cash-confirmations/new",
        query_string={"currency": "EUR", "return_to": "setup_accounts"},
    ).get_data(as_text=True)
    assert 'name="return_to" type="hidden" value="setup_accounts"' in form

    response = client.post(
        f"/accounts/{account_id}/cash-confirmations",
        data=_confirmation_data(return_to="setup_accounts"),
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/setup?step=accounts")
    with app.app_context():
        assert db.session.scalar(select(func.count(CashBalanceCheckpoint.id))) == 1


def test_invalid_confirmation_links_and_focuses_the_first_error(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, separate, _, _ = _records()
        account_id = separate.id

    response = client.post(
        f"/accounts/{account_id}/cash-confirmations",
        data=_confirmation_data(confirmed_balance_amount="not-a-number"),
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'href="#confirmed_balance_amount"' in body
    field = re.search(
        r'<input[^>]*id="confirmed_balance_amount"[^>]*>', body
    ).group(0)
    assert 'aria-invalid="true"' in field
    assert "autofocus" in field
    with app.app_context():
        assert db.session.scalar(select(func.count(CashBalanceCheckpoint.id))) == 0


def test_included_aggregate_account_blocks_separate_confirmation(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, _, included, _ = _records()
        account_id = included.id

    get_response = client.get(f"/accounts/{account_id}/cash-confirmations/new")
    assert get_response.status_code == 302
    blocked = client.get(get_response.headers["Location"]).get_data(as_text=True)
    assert "would count it twice" in blocked
    assert "No separate cash balance is added" in blocked

    post_response = client.post(
        f"/accounts/{account_id}/cash-confirmations",
        data=_confirmation_data(),
    )
    body = post_response.get_data(as_text=True)
    assert post_response.status_code == 200
    assert "would count the same value twice" in body
    with app.app_context():
        assert db.session.scalar(select(func.count(CashBalanceCheckpoint.id))) == 0


def test_cash_setup_and_trade_preview_disclose_confirmation_cutoff(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, separate, _, instrument = _records()
        account_id, instrument_id = separate.id, instrument.id

    setup = client.get("/setup/cash").get_data(as_text=True)
    assert setup.count('aria-current="step"') == 1
    assert "Add current cash" in setup
    assert "cash is already inside this account" in setup
    assert "aggregate statement value" in setup

    client.post(
        f"/accounts/{account_id}/cash-confirmations",
        data=_confirmation_data(),
    )
    preview = client.post(
        "/activity/preview",
        data={
            "activity_type": "buy",
            "effective_date": "2026-06-30",
            "account_id": account_id,
            "instrument_id": instrument_id,
            "quantity": "10",
            "unit_price": "12",
            "fee_amount": "0",
        },
    ).get_data(as_text=True)
    assert "on or before the cash confirmation dated 2026-06-30" in preview
    assert "not current confirmed-and-carried-forward cash" in preview

    positions = client.get("/setup/positions").get_data(as_text=True)
    rail = positions.split('aria-label="Setup progress"', 1)[1].split(
        "</nav>", 1
    )[0]
    assert re.search(
        r'<li class="step-complete">.*?'
        r'<span class="step-label">Cash</span>.*?\(completed\).*?</li>',
        rail,
        re.DOTALL,
    )
