"""Exact Buy/Sell service calculations, invariants, and atomic persistence."""

from datetime import date
from decimal import Decimal

import pytest
from flask import Flask
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
    ValuationObservation,
)
from app.services.activity import (
    ActivityValidationError,
    TradeCommand,
    get_trade_receipt,
    post_trade,
    preview_trade,
)
from app.services.activity_history import ReversalCommand, reverse_activity
from app.services.positions import quantity_as_of


TRADE_DATE = date(2026, 8, 4)


def _base_records(
    *,
    account_currency: str = "EUR",
    is_multicurrency: bool = False,
    cash_tracking_mode: str = "separate_cash",
    instrument_currency: str = "EUR",
):
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
        default_currency_code=account_currency,
        is_multicurrency=is_multicurrency,
        cash_tracking_mode=cash_tracking_mode,
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        relationship_eligible=True,
        is_active=True,
    )
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name="Global Fund",
        instrument_type="fund",
        valuation_currency_code=instrument_currency,
        is_active=True,
    )
    db.session.add_all([account, instrument])
    db.session.commit()
    return portfolio, account, instrument


def _command(
    portfolio,
    account,
    instrument,
    *,
    activity_type: str,
    quantity: str,
    price: str,
    fee: str = "0",
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
    )


def _posting_by_kind(transaction_id: int) -> dict[str, Posting]:
    rows = db.session.scalars(
        select(Posting)
        .where(Posting.transaction_id == transaction_id)
        .order_by(Posting.id)
    )
    return {row.posting_kind: row for row in rows}


def test_buy_preview_is_exact_and_creates_no_records(app: Flask) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records()
        command = _command(
            portfolio,
            account,
            instrument,
            activity_type="buy",
            quantity="1000",
            price="10.25",
            fee="25",
        )

        preview = preview_trade(command)

        assert preview.gross_amount == Decimal("10250.00")
        assert preview.cash_effect == Decimal("-10275.00")
        assert preview.quantity_before == Decimal("0")
        assert preview.quantity_after == Decimal("1000")
        assert preview.creates_registration is True
        assert preview.settlement_currency == "EUR"
        assert db.session.scalar(select(func.count(Transaction.id))) == 0
        assert db.session.scalar(select(func.count(Posting.id))) == 0
        assert db.session.scalar(select(func.count(PositionRegistration.id))) == 0


def test_buy_posting_template_balances_and_opens_the_position(app: Flask) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records()
        posted = post_trade(
            _command(
                portfolio,
                account,
                instrument,
                activity_type="buy",
                quantity="1000",
                price="10.25",
                fee="25",
            )
        )

        transaction = db.session.get(Transaction, posted.transaction_id)
        registration = db.session.get(PositionRegistration, posted.registration_id)
        rows = _posting_by_kind(transaction.id)

        assert transaction.transaction_type == "buy"
        assert transaction.status == "posted"
        assert registration.tracking_mode == "transaction_tracked"
        assert registration.opening_date == TRADE_DATE
        assert rows["instrument"].quantity_delta == Decimal("1000.000000000000")
        assert rows["instrument"].unit_price == Decimal("10.250000000000")
        assert rows["cash"].cash_amount_delta == Decimal("-10275.000000000000")
        assert rows["expense"].cash_amount_delta == Decimal("25.000000000000")
        assert rows["clearing"].cash_amount_delta == Decimal("10250.000000000000")
        assert sum(
            row.cash_amount_delta or Decimal("0") for row in rows.values()
        ) == Decimal("0")
        assert quantity_as_of(registration, TRADE_DATE) == Decimal("1000.000000000000")

        receipt = get_trade_receipt(transaction.id, portfolio_id=portfolio.id)
        assert receipt is not None
        assert receipt.cash_effect == Decimal("-10275.000000000000")
        assert receipt.fee_amount == Decimal("25.000000000000")


def test_later_sale_matches_golden_cash_and_quantity_scenario(app: Flask) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records()
        buy = post_trade(
            _command(
                portfolio,
                account,
                instrument,
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
                instrument,
                activity_type="sell",
                quantity="300",
                price="11.10",
                fee="20",
            )
        )

        assert sale.preview.gross_amount == Decimal("3330.00")
        assert sale.preview.cash_effect == Decimal("3310.00")
        assert sale.preview.quantity_before == Decimal("1000.000000000000")
        assert sale.preview.quantity_after == Decimal("700.000000000000")
        rows = _posting_by_kind(sale.transaction_id)
        assert rows["instrument"].quantity_delta == Decimal("-300.000000000000")
        assert rows["cash"].cash_amount_delta == Decimal("3310.000000000000")
        assert rows["expense"].cash_amount_delta == Decimal("20.000000000000")
        assert rows["clearing"].cash_amount_delta == Decimal("-3330.000000000000")
        registration = db.session.get(PositionRegistration, buy.registration_id)
        assert quantity_as_of(registration, TRADE_DATE) == Decimal("700.000000000000")


def test_entire_holding_preview_and_post_use_exact_fractional_quantity(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records()
        buy = post_trade(
            _command(
                portfolio,
                account,
                instrument,
                activity_type="buy",
                quantity="1247.634",
                price="10",
            )
        )
        command = TradeCommand(
            portfolio_id=portfolio.id,
            activity_type="sell",
            effective_date=TRADE_DATE,
            account_id=account.id,
            instrument_id=instrument.id,
            quantity=None,
            quantity_mode="entire_holding",
            unit_price=Decimal("11"),
            fee_amount=Decimal("5"),
        )
        transaction_count = db.session.scalar(select(func.count(Transaction.id)))

        preview = preview_trade(command)

        assert preview.quantity_mode == "entire_holding"
        assert preview.quantity == Decimal("1247.634000000000")
        assert preview.quantity_after == Decimal("0")
        assert preview.gross_amount == Decimal("13723.97")
        assert db.session.scalar(select(func.count(Transaction.id))) == transaction_count

        sale = post_trade(command)
        rows = _posting_by_kind(sale.transaction_id)
        registration = db.session.get(PositionRegistration, buy.registration_id)
        assert rows["instrument"].quantity_delta == Decimal("-1247.634000000000")
        assert quantity_as_of(registration, TRADE_DATE) == Decimal("0")

        reverse_activity(
            ReversalCommand(
                portfolio_id=portfolio.id,
                transaction_id=sale.transaction_id,
                reason="Sale entered in error",
            )
        )
        assert quantity_as_of(registration, TRADE_DATE) == Decimal(
            "1247.634000000000"
        )


def test_entire_holding_is_resolved_again_when_posted_after_a_stale_preview(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records()
        buy = post_trade(
            _command(
                portfolio,
                account,
                instrument,
                activity_type="buy",
                quantity="100",
                price="10",
            )
        )
        sell_all = TradeCommand(
            portfolio_id=portfolio.id,
            activity_type="sell",
            effective_date=TRADE_DATE,
            account_id=account.id,
            instrument_id=instrument.id,
            quantity=None,
            quantity_mode="entire_holding",
            unit_price=Decimal("11"),
        )

        preview = preview_trade(sell_all)
        assert preview.quantity == Decimal("100")
        post_trade(
            _command(
                portfolio,
                account,
                instrument,
                activity_type="buy",
                quantity="5",
                price="10",
            )
        )

        sale = post_trade(sell_all)

        assert sale.preview.quantity == Decimal("105.000000000000")
        registration = db.session.get(PositionRegistration, buy.registration_id)
        assert quantity_as_of(registration, TRADE_DATE) == Decimal("0")


def test_entire_holding_mode_is_rejected_for_a_buy(app: Flask) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records()

        with pytest.raises(ActivityValidationError) as raised:
            preview_trade(
                TradeCommand(
                    portfolio_id=portfolio.id,
                    activity_type="buy",
                    effective_date=TRADE_DATE,
                    account_id=account.id,
                    instrument_id=instrument.id,
                    quantity=None,
                    quantity_mode="entire_holding",
                    unit_price=Decimal("10"),
                )
            )

        assert raised.value.field == "quantity_mode"
        assert "only for a Sell" in raised.value.message
        assert db.session.scalar(select(func.count(Transaction.id))) == 0


def test_entire_holding_requires_positive_quantity_on_the_sale_date(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records()
        post_trade(
            _command(
                portfolio,
                account,
                instrument,
                activity_type="buy",
                quantity="10",
                price="10",
            )
        )
        post_trade(
            _command(
                portfolio,
                account,
                instrument,
                activity_type="sell",
                quantity="10",
                price="11",
            )
        )

        with pytest.raises(ActivityValidationError) as raised:
            preview_trade(
                TradeCommand(
                    portfolio_id=portfolio.id,
                    activity_type="sell",
                    effective_date=TRADE_DATE,
                    account_id=account.id,
                    instrument_id=instrument.id,
                    quantity=None,
                    quantity_mode="entire_holding",
                    unit_price=Decimal("11"),
                )
            )

        assert raised.value.field == "quantity_mode"
        assert "No units are available" in raised.value.message


def test_oversell_is_blocked_without_partial_writes(app: Flask) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records()
        post_trade(
            _command(
                portfolio,
                account,
                instrument,
                activity_type="buy",
                quantity="100",
                price="10",
            )
        )
        transaction_count = db.session.scalar(select(func.count(Transaction.id)))
        posting_count = db.session.scalar(select(func.count(Posting.id)))

        with pytest.raises(ActivityValidationError) as raised:
            post_trade(
                _command(
                    portfolio,
                    account,
                    instrument,
                    activity_type="sell",
                    quantity="101",
                    price="11",
                )
            )

        assert raised.value.field == "quantity"
        assert "Only 100" in raised.value.message
        assert db.session.scalar(select(func.count(Transaction.id))) == transaction_count
        assert db.session.scalar(select(func.count(Posting.id))) == posting_count


def test_sell_uses_quantity_available_on_its_effective_date(app: Flask) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records()
        post_trade(
            _command(
                portfolio,
                account,
                instrument,
                activity_type="buy",
                quantity="100",
                price="10",
            )
        )
        backdated_sale = TradeCommand(
            portfolio_id=portfolio.id,
            activity_type="sell",
            effective_date=date(2026, 8, 3),
            account_id=account.id,
            instrument_id=instrument.id,
            quantity=Decimal("1"),
            unit_price=Decimal("11"),
        )

        with pytest.raises(ActivityValidationError) as raised:
            preview_trade(backdated_sale)

        assert raised.value.field == "quantity"
        assert "Only 0" in raised.value.message


def test_statement_valued_position_cannot_receive_trade_postings(app: Flask) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records()
        registration = PositionRegistration(
            account_id=account.id,
            instrument_id=instrument.id,
            tracking_mode="statement_valued",
            opening_date=TRADE_DATE,
        )
        db.session.add(registration)
        db.session.flush()
        db.session.add(
            ValuationObservation(
                position_registration_id=registration.id,
                effective_date=TRADE_DATE,
                native_value_amount=Decimal("10000"),
                currency_code="EUR",
            )
        )
        db.session.commit()

        with pytest.raises(ActivityValidationError) as raised:
            preview_trade(
                _command(
                    portfolio,
                    account,
                    instrument,
                    activity_type="buy",
                    quantity="10",
                    price="10",
                )
            )

        assert raised.value.field == "instrument_id"
        assert "statement value" in raised.value.message
        assert db.session.scalar(select(func.count(Transaction.id))) == 0


def test_cross_account_buy_cannot_mix_open_tracking_modes(app: Flask) -> None:
    with app.app_context():
        portfolio, buy_account, instrument = _base_records()
        statement_account = Account(
            portfolio_id=portfolio.id,
            institution_id=buy_account.institution_id,
            name="Statement account",
            account_type="brokerage",
            default_currency_code="EUR",
            is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add(statement_account)
        db.session.flush()
        db.session.add(
            PositionRegistration(
                account_id=statement_account.id,
                instrument_id=instrument.id,
                tracking_mode="statement_valued",
                opening_date=TRADE_DATE,
            )
        )
        db.session.commit()

        with pytest.raises(ActivityValidationError) as raised:
            preview_trade(
                _command(
                    portfolio,
                    buy_account,
                    instrument,
                    activity_type="buy",
                    quantity="10",
                    price="10",
                )
            )

        assert raised.value.field == "instrument_id"
        assert "open position" in raised.value.message
        assert db.session.scalar(select(func.count(Transaction.id))) == 0


@pytest.mark.parametrize("instrument_type", ["cash", "fixed_deposit"])
def test_trade_service_rejects_non_quantity_instrument_types(
    app: Flask, instrument_type: str
) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records()
        instrument.instrument_type = instrument_type
        db.session.commit()

        with pytest.raises(ActivityValidationError) as raised:
            preview_trade(
                _command(
                    portfolio,
                    account,
                    instrument,
                    activity_type="buy",
                    quantity="10",
                    price="10",
                )
            )

        assert raised.value.field == "instrument_id"
        assert "cannot receive Buy/Sell" in raised.value.message


def test_same_currency_guard_blocks_unresolved_cross_currency_settlement(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records(
            account_currency="EUR", instrument_currency="USD"
        )

        with pytest.raises(ActivityValidationError) as raised:
            preview_trade(
                _command(
                    portfolio,
                    account,
                    instrument,
                    activity_type="buy",
                    quantity="10",
                    price="10",
                )
            )

        assert raised.value.field == "instrument_id"
        assert "Cross-currency settlement" in raised.value.message


def test_multicurrency_account_accepts_instrument_currency(app: Flask) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records(
            account_currency="EUR",
            instrument_currency="USD",
            is_multicurrency=True,
        )

        preview = preview_trade(
            _command(
                portfolio,
                account,
                instrument,
                activity_type="buy",
                quantity="10",
                price="12",
            )
        )

        assert preview.settlement_currency == "USD"
        assert preview.cash_effect == Decimal("-120")


def test_service_rejects_an_instrument_from_another_portfolio(app: Flask) -> None:
    with app.app_context():
        portfolio, account, _ = _base_records()
        _, _, other_instrument = _base_records()

        with pytest.raises(ActivityValidationError) as raised:
            preview_trade(
                _command(
                    portfolio,
                    account,
                    other_instrument,
                    activity_type="buy",
                    quantity="10",
                    price="12",
                )
            )

        assert raised.value.field == "instrument_id"
        assert "this portfolio" in raised.value.message


def test_included_aggregate_cash_mode_warns_but_does_not_block_trade(app: Flask) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records(
            cash_tracking_mode="included_in_aggregate"
        )

        preview = preview_trade(
            _command(
                portfolio,
                account,
                instrument,
                activity_type="buy",
                quantity="10",
                price="12",
            )
        )

        assert preview.cash_effect == Decimal("-120")
        assert len(preview.warnings) == 1
        assert "will not be valued as a separate cash balance" in preview.warnings[0]


def test_commit_failure_rolls_back_registration_transaction_and_postings(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records()

        def fail_commit() -> None:
            db.session.flush()
            raise RuntimeError("injected commit failure")

        monkeypatch.setattr(db.session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected commit failure"):
            post_trade(
                _command(
                    portfolio,
                    account,
                    instrument,
                    activity_type="buy",
                    quantity="10",
                    price="12",
                    fee="1",
                )
            )

        assert db.session.scalar(select(func.count(PositionRegistration.id))) == 0
        assert db.session.scalar(select(func.count(Transaction.id))) == 0
        assert db.session.scalar(select(func.count(Posting.id))) == 0


def test_service_rejects_binary_float_amounts(app: Flask) -> None:
    with app.app_context():
        portfolio, account, instrument = _base_records()
        command = TradeCommand(
            portfolio_id=portfolio.id,
            activity_type="buy",
            effective_date=TRADE_DATE,
            account_id=account.id,
            instrument_id=instrument.id,
            quantity=10.0,  # type: ignore[arg-type]
            unit_price=Decimal("12"),
        )

        with pytest.raises(ActivityValidationError) as raised:
            preview_trade(command)

        assert raised.value.field == "quantity"
