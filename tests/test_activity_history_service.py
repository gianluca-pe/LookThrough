"""Activity listing and reversal invariants for."""

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
)
from app.services.activity import ActivityValidationError, TradeCommand, post_trade
from app.services.activity_history import (
    ActivityHistoryFilters,
    ReversalCommand,
    get_activity,
    list_activities,
    preview_reversal,
    reverse_activity,
)
from app.services.cash import CashConfirmationCommand, confirm_cash, resolve_cash
from app.services.dividends import DividendCommand, post_dividend
from app.services.positions import quantity_as_of


def _records():
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
    return portfolio, account, instrument


def _trade(
    portfolio,
    account,
    instrument,
    *,
    kind="buy",
    on=date(2026, 7, 2),
    quantity="100",
    price="10",
    fee="0",
):
    return post_trade(
        TradeCommand(
            portfolio_id=portfolio.id,
            activity_type=kind,
            effective_date=on,
            account_id=account.id,
            instrument_id=instrument.id,
            quantity=Decimal(quantity),
            unit_price=Decimal(price),
            fee_amount=Decimal(fee),
        )
    )


def _posting_effects(transaction_id: int):
    return list(
        db.session.scalars(
            select(Posting)
            .where(Posting.transaction_id == transaction_id)
            .order_by(Posting.id)
        )
    )


def test_history_returns_plain_language_effects_and_filters(app: Flask) -> None:
    with app.app_context():
        portfolio, account, instrument = _records()
        buy = _trade(
            portfolio, account, instrument, on=date(2026, 7, 1), fee="5"
        )
        sale = _trade(
            portfolio,
            account,
            instrument,
            kind="sell",
            on=date(2026, 7, 3),
            quantity="20",
            price="12",
            fee="2",
        )

        history = list_activities(portfolio.id)

        assert [row.transaction_id for row in history] == [
            sale.transaction_id,
            buy.transaction_id,
        ]
        assert history[0].activity_label == "Sell"
        assert history[0].institution_name == "Broker"
        assert history[0].account_name == "Brokerage"
        assert history[0].instrument_name == "Global Fund"
        assert history[0].quantity_effect == Decimal("-20")
        assert history[0].cash_effect == Decimal("238")
        assert history[0].fee_amount == Decimal("2")
        assert history[0].can_reverse is True
        filtered = list_activities(
            portfolio.id,
            ActivityHistoryFilters(
                date_from=date(2026, 7, 2),
                activity_type="sell",
                account_id=account.id,
                instrument_id=instrument.id,
                currency_code="EUR",
                status="posted",
            ),
        )
        assert [row.transaction_id for row in filtered] == [sale.transaction_id]


def test_reversal_uses_original_date_and_restores_post_checkpoint_state(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, instrument = _records()
        confirm_cash(
            CashConfirmationCommand(
                portfolio_id=portfolio.id,
                account_id=account.id,
                currency_code="EUR",
                effective_date=date(2026, 6, 30),
                confirmed_balance_amount=Decimal("5000"),
            )
        )
        buy = _trade(portfolio, account, instrument)
        registration = db.session.get(PositionRegistration, buy.registration_id)
        assert quantity_as_of(registration, date(2026, 8, 1)) == Decimal("100")
        assert resolve_cash(
            portfolio.id, account.id, "EUR", date(2026, 8, 1)
        ).amount == Decimal("4000")

        result = reverse_activity(
            ReversalCommand(
                portfolio_id=portfolio.id,
                transaction_id=buy.transaction_id,
                reason="Entered in the wrong account",
            )
        )

        original = db.session.get(Transaction, buy.transaction_id)
        reversal = db.session.get(Transaction, result.reversal_transaction_ids[0])
        assert original.status == "reversed"
        assert reversal.status == "posted"
        assert reversal.transaction_type == "reversal"
        assert reversal.reverses_transaction_id == original.id
        assert reversal.effective_date == original.effective_date
        assert reversal.note == "Entered in the wrong account"
        original_rows = _posting_effects(original.id)
        reversal_rows = _posting_effects(reversal.id)
        assert len(original_rows) == len(reversal_rows)
        for source, opposite in zip(original_rows, reversal_rows, strict=True):
            assert opposite.posting_kind == source.posting_kind
            assert opposite.quantity_delta == (
                -source.quantity_delta if source.quantity_delta is not None else None
            )
            assert opposite.cash_amount_delta == (
                -source.cash_amount_delta
                if source.cash_amount_delta is not None
                else None
            )
        assert quantity_as_of(registration, date(2026, 8, 1)) == Decimal("0")
        cash = resolve_cash(portfolio.id, account.id, "EUR", date(2026, 8, 1))
        assert cash.amount == Decimal("5000")
        assert cash.effects == ()


def test_reversing_pre_checkpoint_trade_does_not_create_false_later_cash(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, instrument = _records()
        buy = _trade(
            portfolio, account, instrument, on=date(2026, 6, 29)
        )
        confirm_cash(
            CashConfirmationCommand(
                portfolio_id=portfolio.id,
                account_id=account.id,
                currency_code="EUR",
                effective_date=date(2026, 6, 30),
                confirmed_balance_amount=Decimal("5000"),
            )
        )

        reverse_activity(
            ReversalCommand(portfolio.id, buy.transaction_id, "Duplicate entry")
        )

        registration = db.session.get(PositionRegistration, buy.registration_id)
        assert quantity_as_of(registration, date(2026, 8, 1)) == Decimal("0")
        assert resolve_cash(
            portfolio.id, account.id, "EUR", date(2026, 8, 1)
        ).amount == Decimal("5000")


def test_linked_dividend_and_buy_reverse_as_one_atomic_group(app: Flask) -> None:
    with app.app_context():
        portfolio, account, instrument = _records()
        opening = _trade(
            portfolio,
            account,
            instrument,
            on=date(2026, 1, 1),
            quantity="100",
        )
        confirm_cash(
            CashConfirmationCommand(
                portfolio.id,
                account.id,
                "EUR",
                date(2026, 6, 30),
                Decimal("1000"),
            )
        )
        dividend = post_dividend(
            DividendCommand(
                portfolio_id=portfolio.id,
                effective_date=date(2026, 7, 2),
                account_id=account.id,
                instrument_id=instrument.id,
                currency_code="EUR",
                net_amount=Decimal("100"),
                outcome="reinvest_same",
                reinvestment_quantity=Decimal("10"),
                reinvestment_unit_price=Decimal("10"),
            ),
            group_id_provider=lambda: "income-and-buy",
        )
        registration = db.session.get(PositionRegistration, opening.registration_id)
        assert quantity_as_of(registration, date(2026, 8, 1)) == Decimal("110")

        preview = preview_reversal(
            dividend.buy_transaction_id, portfolio_id=portfolio.id
        )
        result = reverse_activity(
            ReversalCommand(
                portfolio.id,
                dividend.dividend_transaction_id,
                "Distribution was entered twice",
            ),
            group_id_provider=lambda: "reversal-group",
        )

        assert preview.is_group is True
        assert set(preview.original_transaction_ids) == {
            dividend.dividend_transaction_id,
            dividend.buy_transaction_id,
        }
        assert len(result.reversal_transaction_ids) == 2
        assert result.activity_group_id == "reversal-group"
        assert {
            db.session.get(Transaction, row_id).reverses_transaction_id
            for row_id in result.reversal_transaction_ids
        } == set(result.original_transaction_ids)
        assert all(
            db.session.get(Transaction, row_id).status == "reversed"
            for row_id in result.original_transaction_ids
        )
        assert quantity_as_of(registration, date(2026, 8, 1)) == Decimal("100")
        assert resolve_cash(
            portfolio.id, account.id, "EUR", date(2026, 8, 1)
        ).amount == Decimal("1000")


def test_history_retains_original_and_links_its_reversal(app: Flask) -> None:
    with app.app_context():
        portfolio, account, instrument = _records()
        buy = _trade(portfolio, account, instrument)
        result = reverse_activity(
            ReversalCommand(portfolio.id, buy.transaction_id, "Duplicate")
        )

        original = get_activity(buy.transaction_id, portfolio_id=portfolio.id)
        reversal = get_activity(
            result.reversal_transaction_ids[0], portfolio_id=portfolio.id
        )

        assert original.status == "reversed"
        assert original.can_reverse is False
        assert original.reversed_by_transaction_ids == (
            result.reversal_transaction_ids[0],
        )
        assert reversal.activity_label == "Reversal of Buy"
        assert reversal.reverses_transaction_id == buy.transaction_id
        assert reversal.can_reverse is False
        assert reversal.note == "Duplicate"


def test_double_reversal_and_reversing_a_reversal_are_blocked(app: Flask) -> None:
    with app.app_context():
        portfolio, account, instrument = _records()
        buy = _trade(portfolio, account, instrument)
        result = reverse_activity(
            ReversalCommand(portfolio.id, buy.transaction_id, "Duplicate")
        )

        with pytest.raises(ActivityValidationError, match="already been reversed"):
            reverse_activity(
                ReversalCommand(portfolio.id, buy.transaction_id, "Again")
            )
        with pytest.raises(ActivityValidationError, match="cannot itself"):
            reverse_activity(
                ReversalCommand(
                    portfolio.id, result.reversal_transaction_ids[0], "Again"
                )
            )
        assert db.session.scalar(select(func.count(Transaction.id))) == 2


def test_reversal_that_would_make_later_quantity_negative_is_blocked(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, instrument = _records()
        buy = _trade(
            portfolio,
            account,
            instrument,
            on=date(2026, 7, 1),
            quantity="100",
        )
        _trade(
            portfolio,
            account,
            instrument,
            kind="sell",
            on=date(2026, 7, 2),
            quantity="80",
        )

        with pytest.raises(ActivityValidationError, match="dependent sales"):
            reverse_activity(
                ReversalCommand(portfolio.id, buy.transaction_id, "Wrong buy")
            )

        assert db.session.get(Transaction, buy.transaction_id).status == "posted"
        assert db.session.scalar(select(func.count(Transaction.id))) == 2


@pytest.mark.parametrize("reason", ["", "   ", "x" * 1001])
def test_reversal_requires_a_bounded_reason(app: Flask, reason: str) -> None:
    with app.app_context():
        portfolio, account, instrument = _records()
        buy = _trade(portfolio, account, instrument)

        with pytest.raises(ActivityValidationError) as raised:
            reverse_activity(
                ReversalCommand(portfolio.id, buy.transaction_id, reason)
            )

        assert raised.value.field == "reason"
        assert db.session.get(Transaction, buy.transaction_id).status == "posted"


def test_reversal_commit_failure_rolls_back_every_effect(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        portfolio, account, instrument = _records()
        buy = _trade(portfolio, account, instrument)

        def fail_commit():
            raise RuntimeError("injected commit failure")

        monkeypatch.setattr(db.session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected"):
            reverse_activity(
                ReversalCommand(portfolio.id, buy.transaction_id, "Duplicate")
            )

        assert db.session.get(Transaction, buy.transaction_id).status == "posted"
        assert db.session.scalar(select(func.count(Transaction.id))) == 1


def test_opening_position_is_reversible_without_deleting_its_audit_record(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, instrument = _records()
        registration = PositionRegistration(
            account_id=account.id,
            instrument_id=instrument.id,
            tracking_mode="transaction_tracked",
            opening_date=date(2026, 1, 1),
        )
        opening = Transaction(
            portfolio_id=portfolio.id,
            transaction_type="opening_balance",
            effective_date=date(2026, 1, 1),
            status="posted",
        )
        db.session.add_all([registration, opening])
        db.session.flush()
        db.session.add(
            Posting(
                transaction_id=opening.id,
                account_id=account.id,
                posting_kind="instrument",
                instrument_id=instrument.id,
                currency_code="EUR",
                quantity_delta=Decimal("25"),
            )
        )
        db.session.commit()

        reverse_activity(
            ReversalCommand(portfolio.id, opening.id, "Opening quantity was wrong")
        )

        assert quantity_as_of(registration, date(2026, 8, 1)) == Decimal("0")
        assert db.session.get(Transaction, opening.id).status == "reversed"
        assert db.session.get(PositionRegistration, registration.id) is not None
