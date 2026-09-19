"""Inline instrument creation and sale-to-replacement workflows."""

import re
from datetime import date
from decimal import Decimal

import pytest
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
    Transaction,
)
from app.services.activity import (
    ActivityValidationError,
    InlineInstrumentCommand,
    TradeCommand,
    create_trade_instrument,
    get_replacement_context,
    post_trade,
    preview_trade,
)


TRADE_DATE = date(2026, 8, 4)


def _records(*, multicurrency: bool = False):
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
        is_multicurrency=multicurrency,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        relationship_eligible=True,
        is_active=True,
    )
    source = Instrument(
        portfolio_id=portfolio.id,
        name="Global Fund",
        ticker_or_isin="FUND123",
        instrument_type="fund",
        valuation_currency_code="EUR",
        is_active=True,
    )
    db.session.add_all([account, source])
    db.session.commit()
    return portfolio, account, source


def _command(
    portfolio: Portfolio,
    account: Account,
    instrument: Instrument,
    *,
    activity_type: str,
    quantity: str,
    price: str,
    fee: str = "0",
    replacement_sale_id: int | None = None,
) -> TradeCommand:
    return TradeCommand(
        portfolio_id=portfolio.id,
        activity_type=activity_type,
        effective_date=TRADE_DATE,
        account_id=account.id,
        instrument_id=instrument.id,
        quantity=Decimal(quantity),
        unit_price=Decimal(price),
        fee_amount=Decimal(fee),
        replacement_sale_id=replacement_sale_id,
    )


def _trade_data(
    account_id: int,
    instrument_id: int,
    **overrides: object,
) -> dict[str, object]:
    data: dict[str, object] = {
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


def test_inline_instrument_service_saves_identity_only(app: Flask) -> None:
    with app.app_context():
        portfolio, account, _ = _records()

        created = create_trade_instrument(
            InlineInstrumentCommand(
                portfolio_id=portfolio.id,
                account_id=account.id,
                name="  Short Bond ETF  ",
                ticker_or_isin="  BOND1  ",
                valuation_currency_code="eur",
                instrument_type="etf",
            )
        )

        instrument = db.session.get(Instrument, created.instrument_id)
        assert instrument.name == "Short Bond ETF"
        assert instrument.ticker_or_isin == "BOND1"
        assert instrument.valuation_currency_code == "EUR"
        assert instrument.instrument_type == "etf"
        assert instrument.is_active is True
        assert db.session.scalar(select(func.count(Transaction.id))) == 0
        assert db.session.scalar(select(func.count(PositionRegistration.id))) == 0


@pytest.mark.parametrize("instrument_type", ["cash", "fixed_deposit"])
def test_inline_buy_rejects_non_quantity_instrument_types_without_partial_save(
    app: Flask, instrument_type: str
) -> None:
    with app.app_context():
        portfolio, account, _ = _records()
        before = db.session.scalar(select(func.count(Instrument.id)))

        with pytest.raises(ActivityValidationError) as raised:
            create_trade_instrument(
                InlineInstrumentCommand(
                    portfolio_id=portfolio.id,
                    account_id=account.id,
                    name="Not a trade instrument",
                    ticker_or_isin=None,
                    valuation_currency_code="EUR",
                    instrument_type=instrument_type,
                )
            )

        assert raised.value.field == "new_instrument_type"
        assert "dedicated workflows" in raised.value.message
        assert db.session.scalar(select(func.count(Instrument.id))) == before


def test_inline_instrument_rejects_unusable_currency_without_partial_save(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, _ = _records()
        before = db.session.scalar(select(func.count(Instrument.id)))

        with pytest.raises(ActivityValidationError) as raised:
            create_trade_instrument(
                InlineInstrumentCommand(
                    portfolio_id=portfolio.id,
                    account_id=account.id,
                    name="US Fund",
                    ticker_or_isin=None,
                    valuation_currency_code="USD",
                    instrument_type="fund",
                )
            )

        assert raised.value.field == "new_valuation_currency_code"
        assert "single-currency account" in raised.value.message
        assert db.session.scalar(select(func.count(Instrument.id))) == before


def test_inline_instrument_commit_failure_rolls_back(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        portfolio, account, _ = _records()
        before = db.session.scalar(select(func.count(Instrument.id)))

        def fail_commit() -> None:
            db.session.flush()
            raise RuntimeError("injected instrument failure")

        monkeypatch.setattr(db.session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected instrument failure"):
            create_trade_instrument(
                InlineInstrumentCommand(
                    portfolio_id=portfolio.id,
                    account_id=account.id,
                    name="Short Bond ETF",
                    ticker_or_isin=None,
                    valuation_currency_code="EUR",
                    instrument_type="etf",
                )
            )

        assert db.session.scalar(select(func.count(Instrument.id))) == before


def test_replacement_context_is_exact_and_purchase_stays_separate(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, source = _records(multicurrency=True)
        post_trade(
            _command(
                portfolio,
                account,
                source,
                activity_type="buy",
                quantity="1000",
                price="10.25",
                fee="25",
            )
        )
        sale = post_trade(
            _command(
                portfolio,
                account,
                source,
                activity_type="sell",
                quantity="300",
                price="11.10",
                fee="20",
            )
        )
        replacement = Instrument(
            portfolio_id=portfolio.id,
            name="Short Bond ETF",
            instrument_type="etf",
            valuation_currency_code="EUR",
            is_active=True,
        )
        db.session.add(replacement)
        db.session.commit()

        context = get_replacement_context(
            sale.transaction_id, portfolio_id=portfolio.id
        )
        purchase = post_trade(
            _command(
                portfolio,
                account,
                replacement,
                activity_type="buy",
                quantity="100",
                price="30",
                fee="5",
                replacement_sale_id=sale.transaction_id,
            )
        )

        assert context.account_id == account.id
        assert context.effective_date == TRADE_DATE
        assert context.settlement_currency == "EUR"
        assert context.available_proceeds == Decimal("3310.000000000000")
        assert purchase.transaction_id != sale.transaction_id
        sale_row = db.session.get(Transaction, sale.transaction_id)
        purchase_row = db.session.get(Transaction, purchase.transaction_id)
        assert sale_row.activity_group_id is None
        assert purchase_row.activity_group_id is None
        assert sale_row.transaction_type == "sell"
        assert purchase_row.transaction_type == "buy"


def test_replacement_blocks_another_account_or_currency(app: Flask) -> None:
    with app.app_context():
        portfolio, account, source = _records(multicurrency=True)
        post_trade(
            _command(
                portfolio,
                account,
                source,
                activity_type="buy",
                quantity="100",
                price="10",
            )
        )
        sale = post_trade(
            _command(
                portfolio,
                account,
                source,
                activity_type="sell",
                quantity="10",
                price="11",
            )
        )
        other_account = Account(
            portfolio_id=portfolio.id,
            institution_id=account.institution_id,
            name="Other account",
            account_type="brokerage",
            default_currency_code="EUR",
            is_multicurrency=True,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        usd_instrument = Instrument(
            portfolio_id=portfolio.id,
            name="US Fund",
            instrument_type="fund",
            valuation_currency_code="USD",
            is_active=True,
        )
        db.session.add_all([other_account, usd_instrument])
        db.session.commit()

        with pytest.raises(ActivityValidationError) as wrong_account:
            preview_trade(
                _command(
                    portfolio,
                    other_account,
                    source,
                    activity_type="buy",
                    quantity="1",
                    price="10",
                    replacement_sale_id=sale.transaction_id,
                )
            )
        with pytest.raises(ActivityValidationError) as wrong_currency:
            preview_trade(
                _command(
                    portfolio,
                    account,
                    usd_instrument,
                    activity_type="buy",
                    quantity="1",
                    price="10",
                    replacement_sale_id=sale.transaction_id,
                )
            )

        assert wrong_account.value.field == "account_id"
        assert wrong_currency.value.field == "instrument_id"
        assert "sale's settlement currency" in wrong_currency.value.message

        before_sale = TradeCommand(
            portfolio_id=portfolio.id,
            activity_type="buy",
            effective_date=date(2026, 8, 3),
            account_id=account.id,
            instrument_id=source.id,
            quantity=Decimal("1"),
            unit_price=Decimal("10"),
            replacement_sale_id=sale.transaction_id,
        )
        with pytest.raises(ActivityValidationError) as wrong_date:
            preview_trade(before_sale)
        assert wrong_date.value.field == "effective_date"
        assert "on or after" in wrong_date.value.message


def test_nonpositive_net_sale_has_no_replacement_context(app: Flask) -> None:
    with app.app_context():
        portfolio, account, source = _records()
        post_trade(
            _command(
                portfolio,
                account,
                source,
                activity_type="buy",
                quantity="10",
                price="10",
            )
        )
        sale = post_trade(
            _command(
                portfolio,
                account,
                source,
                activity_type="sell",
                quantity="1",
                price="1",
                fee="2",
            )
        )

        assert sale.preview.cash_effect == Decimal("-1")
        assert (
            get_replacement_context(sale.transaction_id, portfolio_id=portfolio.id)
            is None
        )


def test_buy_exposes_inline_identity_fields_and_sell_does_not(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _records()

    buy = client.get("/activity/new?type=buy").get_data(as_text=True)
    sell = client.get("/activity/new?type=sell").get_data(as_text=True)

    assert ">Add a new instrument</option>" in buy
    assert '<summary>Add a new instrument</summary>' in buy
    for field_id in (
        "new_instrument_name",
        "new_ticker_or_isin",
        "new_valuation_currency_code",
        "new_instrument_type",
    ):
        assert f'id="{field_id}"' in buy
        assert f'id="{field_id}"' not in sell
    assert 'formaction="/activity/instruments"' in buy


def test_inline_route_returns_to_populated_buy_and_invalid_input_focuses(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, account, source = _records()
        account_id, source_id = account.id, source.id

    invalid = client.post(
        "/activity/instruments",
        data=_trade_data(
            account_id,
            0,
            new_instrument_name="",
            new_ticker_or_isin="",
            new_valuation_currency_code="EUR",
            new_instrument_type="etf",
            create_instrument="Save and use instrument",
        ),
    )
    invalid_body = invalid.get_data(as_text=True)
    assert invalid.status_code == 200
    assert 'href="#new_instrument_name"' in invalid_body
    name_tag = re.search(
        r'<input[^>]*id="new_instrument_name"[^>]*>', invalid_body
    ).group(0)
    assert "autofocus" in name_tag

    response = client.post(
        "/activity/instruments",
        data=_trade_data(
            account_id,
            0,
            quantity="125",
            unit_price="20.5",
            fee_amount="5",
            new_instrument_name="Short Bond ETF",
            new_ticker_or_isin="BOND1",
            new_valuation_currency_code="eur",
            new_instrument_type="etf",
            create_instrument="Save and use instrument",
        ),
    )
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/activity/new?type=buy")
    returned = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Short Bond ETF (BOND1)" in returned
    selected = re.search(
        r'<option selected value="(\d+)">Short Bond ETF \(BOND1\)</option>',
        returned,
    )
    assert selected
    assert 'value="125"' in returned
    assert 'value="20.5"' in returned
    assert "Finish recording the Buy" in returned

    with app.app_context():
        assert db.session.scalar(select(func.count(Instrument.id))) == 2
        assert db.session.scalar(select(func.count(Transaction.id))) == 0
        assert db.session.get(Instrument, source_id) is not None


def test_buy_cannot_record_the_unsaved_new_instrument_choice(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, account, _ = _records()
        account_id = account.id

    response = client.post(
        "/activity/new",
        data=_trade_data(account_id, 0),
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Save the new instrument before recording this Buy" in body
    assert 'href="#instrument_id"' in body
    with app.app_context():
        assert db.session.scalar(select(func.count(Transaction.id))) == 0


def test_sell_to_inline_replacement_buy_is_a_complete_no_js_flow(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, account, source = _records()
        account_id, source_id = account.id, source.id
        post_trade(
            _command(
                portfolio,
                account,
                source,
                activity_type="buy",
                quantity="1000",
                price="10.25",
                fee="25",
            )
        )

    sale_response = client.post(
        "/activity/new",
        data=_trade_data(
            account_id,
            source_id,
            activity_type="sell",
            quantity="300",
            unit_price="11.10",
            fee_amount="20",
        ),
    )
    sale_id = int(sale_response.headers["Location"].split("/")[2])
    success = client.get(sale_response.headers["Location"]).get_data(as_text=True)
    replacement_url = f"/activity/{sale_id}/replacement"
    assert f'href="{replacement_url}"' in success
    assert "Use sale proceeds to buy another instrument" in success

    replacement_form = client.get(replacement_url).get_data(as_text=True)
    assert "Continue from sale" in replacement_form
    assert "3,310.00" in replacement_form
    assert f'value="{sale_id}"' in replacement_form
    assert 'value="2026-08-04"' in replacement_form
    assert re.search(
        rf'<option selected value="{account_id}">Broker A — Brokerage EUR</option>',
        replacement_form,
    )

    create_response = client.post(
        "/activity/instruments",
        data=_trade_data(
            account_id,
            0,
            quantity="100",
            unit_price="30",
            fee_amount="5",
            replacement_sale_id=str(sale_id),
            new_instrument_name="Short Bond ETF",
            new_ticker_or_isin="BOND1",
            new_valuation_currency_code="EUR",
            new_instrument_type="etf",
            create_instrument="Save and use instrument",
        ),
    )
    assert create_response.status_code == 302
    assert create_response.headers["Location"].startswith(replacement_url)
    populated = client.get(create_response.headers["Location"]).get_data(as_text=True)
    instrument_id = int(
        re.search(
            r'<option selected value="(\d+)">Short Bond ETF \(BOND1\)</option>',
            populated,
        ).group(1)
    )
    assert "3,310.00" in populated
    assert 'value="100"' in populated
    assert 'value="30"' in populated

    buy_response = client.post(
        "/activity/new",
        data=_trade_data(
            account_id,
            instrument_id,
            quantity="100",
            unit_price="30",
            fee_amount="5",
            replacement_sale_id=str(sale_id),
        ),
    )
    assert buy_response.status_code == 302

    with app.app_context():
        sale = db.session.get(Transaction, sale_id)
        replacement_buy = db.session.scalar(
            select(Transaction)
            .where(Transaction.id != sale_id, Transaction.transaction_type == "buy")
            .order_by(Transaction.id.desc())
        )
        assert sale.activity_group_id is None
        assert replacement_buy.activity_group_id is None
        assert replacement_buy.id != sale.id
        assert db.session.scalar(select(func.count(Transaction.id))) == 3


def test_replacement_route_rejects_a_buy_or_another_portfolios_sale(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        first_portfolio, account, instrument = _records()
        buy = post_trade(
            _command(
                first_portfolio,
                account,
                instrument,
                activity_type="buy",
                quantity="10",
                price="10",
            )
        )
        other_portfolio, other_account, other_instrument = _records()
        post_trade(
            _command(
                other_portfolio,
                other_account,
                other_instrument,
                activity_type="buy",
                quantity="10",
                price="10",
            )
        )
        other_sale = post_trade(
            _command(
                other_portfolio,
                other_account,
                other_instrument,
                activity_type="sell",
                quantity="1",
                price="11",
            )
        )
        transaction_count = db.session.scalar(select(func.count(Transaction.id)))
        account_id, instrument_id = account.id, instrument.id

    assert client.get(f"/activity/{buy.transaction_id}/replacement").status_code == 404
    assert (
        client.get(f"/activity/{other_sale.transaction_id}/replacement").status_code
        == 404
    )
    tampered_post = client.post(
        "/activity/new",
        data=_trade_data(
            account_id,
            instrument_id,
            quantity="1",
            unit_price="10",
            fee_amount="0",
            replacement_sale_id=str(other_sale.transaction_id),
        ),
    )
    assert tampered_post.status_code == 404
    with app.app_context():
        assert db.session.scalar(select(func.count(Transaction.id))) == transaction_count
