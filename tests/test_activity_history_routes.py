"""Server-rendered history, detail, and reversal route contracts."""

from datetime import date
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient

from app.extensions import db
from app.models import Account, Institution, Instrument, Portfolio, Transaction
from app.services.activity import TradeCommand, post_trade


def _seed():
    portfolio = Portfolio(
        name="Portfolio",
        reporting_currency_code="EUR",
        annual_spending_amount=Decimal("48000"),
    )
    db.session.add(portfolio)
    db.session.flush()
    institution = Institution(portfolio_id=portfolio.id, name="Broker")
    db.session.add(institution)
    db.session.flush()
    account = Account(
        portfolio_id=portfolio.id,
        institution_id=institution.id,
        name="Brokerage",
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
        instrument_type="fund",
        valuation_currency_code="EUR",
        is_active=True,
    )
    db.session.add_all([account, instrument])
    db.session.commit()
    trade = post_trade(
        TradeCommand(
            portfolio_id=portfolio.id,
            activity_type="buy",
            effective_date=date(2026, 8, 4),
            account_id=account.id,
            instrument_id=instrument.id,
            quantity=Decimal("100"),
            unit_price=Decimal("10"),
            fee_amount=Decimal("5"),
        )
    )
    return portfolio, account, instrument, trade


def test_history_routes_redirect_to_setup_without_portfolio(
    client: FlaskClient,
) -> None:
    assert client.get("/activity").status_code == 302
    assert client.get("/activity/1").status_code == 302
    assert client.get("/activity/1/reverse").status_code == 302


def test_history_and_detail_render_plain_language_views(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, _, _, trade = _seed()

    history = client.get("/activity").get_data(as_text=True)
    detail = client.get(f"/activity/{trade.transaction_id}").get_data(as_text=True)

    assert "<h1>Activity history</h1>" in history
    assert "Global Fund" in history
    assert "EUR</span> -1,005.00" in history
    assert f'/activity/{trade.transaction_id}' in history
    assert "<h1>Buy</h1>" in detail
    assert "Quantity effect" in detail
    assert "Reverse activity" in detail
    assert "posting" not in detail.lower()


def test_history_filters_are_applied_and_invalid_dates_are_explained(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, account, instrument, _ = _seed()
        account_id = account.id
        instrument_id = instrument.id

    filtered = client.get(
        "/activity?date_from=2026-08-05"
        f"&account_id={account_id}&instrument_id={instrument_id}&type=buy"
        "&currency=eur&status=posted"
    ).get_data(as_text=True)
    invalid = client.get(
        "/activity?date_from=not-a-date&date_to=2026-01-01"
    ).get_data(as_text=True)

    assert "No activities match these filters." in filtered
    assert "There is a problem" in invalid
    assert 'href="#date_from"' in invalid
    assert "YYYY-MM-DD" in invalid


def test_inverted_date_filter_preserves_both_valid_entries_with_error(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _seed()

    body = client.get(
        "/activity?date_from=2026-08-05&date_to=2026-08-01&type=buy"
    ).get_data(as_text=True)

    assert "End date must be on or after the start date." in body
    assert 'id="date_from" name="date_from" type="date" value="2026-08-05"' in body
    assert 'id="date_to" name="date_to" type="date" value="2026-08-01"' in body
    assert '<option value="buy" selected>' in body


def test_reversal_confirmation_requires_reason_then_redirects_to_detail(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, _, _, trade = _seed()
        transaction_id = trade.transaction_id

    confirmation = client.get(
        f"/activity/{transaction_id}/reverse"
    ).get_data(as_text=True)
    invalid = client.post(f"/activity/{transaction_id}/reverse", data={})
    posted = client.post(
        f"/activity/{transaction_id}/reverse",
        data={"reason": "Duplicate activity"},
        follow_redirects=True,
    )

    assert "<h1>Reverse activity</h1>" in confirmation
    assert "original date" in confirmation
    assert 'label for="reason"' in confirmation
    assert invalid.status_code == 400
    assert "Reason: This field is required." in invalid.get_data(as_text=True)
    assert posted.status_code == 200
    body = posted.get_data(as_text=True)
    assert "Buy reversed." in body
    assert "Reversed" in body
    assert "Reverse activity" not in body
    with app.app_context():
        assert db.session.get(Transaction, transaction_id).status == "reversed"


def test_double_submit_is_safe_and_cross_portfolio_activity_is_hidden(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, _, _, trade = _seed()
        transaction_id = trade.transaction_id
        other = Portfolio(
            name="Other",
            reporting_currency_code="USD",
            annual_spending_amount=Decimal("1"),
        )
        db.session.add(other)
        db.session.flush()
        hidden = Transaction(
            portfolio_id=other.id,
            transaction_type="buy",
            effective_date=date(2026, 8, 4),
            status="posted",
        )
        db.session.add(hidden)
        db.session.commit()
        hidden_id = hidden.id

    first = client.post(
        f"/activity/{transaction_id}/reverse", data={"reason": "Duplicate"}
    )
    second = client.post(
        f"/activity/{transaction_id}/reverse",
        data={"reason": "Duplicate again"},
        follow_redirects=True,
    )

    assert first.status_code == 302
    assert "already been reversed" in second.get_data(as_text=True)
    assert client.get(f"/activity/{hidden_id}").status_code == 404
    assert client.get(f"/activity/{hidden_id}/reverse").status_code == 404
