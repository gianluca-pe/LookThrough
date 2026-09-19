"""Cash replay, confirmation cutoff, replacement, and warning contracts."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from flask import Flask
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
from app.services.activity import TradeCommand, post_trade, preview_trade
from app.services.cash import (
    CashConfirmationCommand,
    CashValidationError,
    backdated_cash_warning,
    cash_setup_complete,
    confirm_cash,
    resolve_cash,
)


def _account(*, mode: str = "separate_cash", multicurrency: bool = False):
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
        is_multicurrency=multicurrency,
        cash_tracking_mode=mode,
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        relationship_eligible=True,
        is_active=True,
    )
    db.session.add(account)
    db.session.commit()
    return portfolio, account


def _cash_effect(
    portfolio,
    account,
    *,
    on: date,
    amount: str,
    kind: str = "deposit",
    currency: str = "EUR",
):
    transaction = Transaction(
        portfolio_id=portfolio.id,
        transaction_type=kind,
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
            currency_code=currency,
            cash_amount_delta=Decimal(amount),
        )
    )
    db.session.commit()
    return transaction


def _confirmation(portfolio, account, *, on: date, amount: str, note: str | None = None):
    return confirm_cash(
        CashConfirmationCommand(
            portfolio_id=portfolio.id,
            account_id=account.id,
            currency_code="EUR",
            effective_date=on,
            confirmed_balance_amount=Decimal(amount),
            source_note=note,
        )
    )


def test_without_confirmation_cash_sums_only_posted_cash_legs(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _account()
        first = _cash_effect(
            portfolio, account, on=date(2026, 6, 1), amount="-10275", kind="buy"
        )
        _cash_effect(
            portfolio, account, on=date(2026, 7, 1), amount="3310", kind="sell"
        )
        db.session.add(
            Posting(
                transaction_id=first.id,
                account_id=account.id,
                posting_kind="expense",
                currency_code="EUR",
                cash_amount_delta=Decimal("25"),
            )
        )
        reversed_transaction = Transaction(
            portfolio_id=portfolio.id,
            transaction_type="deposit",
            effective_date=date(2026, 7, 2),
            status="reversed",
        )
        db.session.add(reversed_transaction)
        db.session.flush()
        db.session.add(
            Posting(
                transaction_id=reversed_transaction.id,
                account_id=account.id,
                posting_kind="cash",
                currency_code="EUR",
                cash_amount_delta=Decimal("999"),
            )
        )
        db.session.commit()

        result = resolve_cash(
            portfolio.id, account.id, "eur", date(2026, 7, 31)
        )

        assert result.amount == Decimal("-6965.000000000000")
        assert result.status == "calculated"
        assert result.source_mode == "transaction"
        assert [row.amount for row in result.effects] == [
            Decimal("-10275.000000000000"),
            Decimal("3310.000000000000"),
        ]
        assert result.warnings and "negative" in result.warnings[0]


def test_confirmation_reconciles_18400_to_17950_without_creating_activity(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account = _account()
        _cash_effect(
            portfolio, account, on=date(2026, 6, 29), amount="18400"
        )
        transaction_count = db.session.scalar(select(func.count(Transaction.id)))
        posting_count = db.session.scalar(select(func.count(Posting.id)))

        saved = _confirmation(
            portfolio,
            account,
            on=date(2026, 6, 30),
            amount="17950",
            note="June statement",
        )

        assert saved.prior_calculated_balance_amount == Decimal("18400.000000000000")
        assert saved.correction_amount == Decimal("-450.000000000000")
        assert db.session.scalar(select(func.count(Transaction.id))) == transaction_count
        assert db.session.scalar(select(func.count(Posting.id))) == posting_count
        checkpoint = db.session.get(CashBalanceCheckpoint, saved.checkpoint_id)
        assert checkpoint.source_note == "June statement"
        assert checkpoint.prior_calculated_balance_amount == Decimal("18400.000000000000")


def test_later_sale_carries_forward_from_confirmation_to_21260(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _account()
        _cash_effect(
            portfolio, account, on=date(2026, 6, 29), amount="18400"
        )
        _confirmation(
            portfolio, account, on=date(2026, 6, 30), amount="17950"
        )
        _cash_effect(
            portfolio,
            account,
            on=date(2026, 7, 1),
            amount="3310",
            kind="sell",
        )

        result = resolve_cash(
            portfolio.id, account.id, "EUR", date(2026, 7, 31)
        )

        assert result.amount == Decimal("21260.000000000000")
        assert result.status == "confirmed"
        assert result.checkpoint.effective_date == date(2026, 6, 30)
        assert result.later_cash_effect_amount == Decimal("3310.000000000000")


def test_confirmation_is_end_of_day_and_excludes_same_day_cash_effects(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account = _account()
        _confirmation(
            portfolio, account, on=date(2026, 6, 30), amount="1000"
        )
        _cash_effect(
            portfolio, account, on=date(2026, 6, 30), amount="500", kind="sell"
        )
        _cash_effect(
            portfolio, account, on=date(2026, 7, 1), amount="100", kind="sell"
        )

        result = resolve_cash(
            portfolio.id, account.id, "EUR", date(2026, 7, 1)
        )

        assert result.amount == Decimal("1100.000000000000")
        assert len(result.effects) == 1
        assert result.effects[0].effective_date == date(2026, 7, 1)


def test_as_of_before_confirmation_replays_earlier_activity(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _account()
        _cash_effect(portfolio, account, on=date(2026, 6, 1), amount="800")
        _confirmation(
            portfolio, account, on=date(2026, 6, 30), amount="1000"
        )

        historical = resolve_cash(
            portfolio.id, account.id, "EUR", date(2026, 6, 15)
        )

        assert historical.amount == Decimal("800.000000000000")
        assert historical.status == "calculated"
        assert historical.checkpoint is None


def test_same_date_reconfirmation_supersedes_old_row(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _account()
        first = _confirmation(
            portfolio, account, on=date(2026, 6, 30), amount="1000"
        )
        fixed_now = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)
        second = confirm_cash(
            CashConfirmationCommand(
                portfolio_id=portfolio.id,
                account_id=account.id,
                currency_code="EUR",
                effective_date=date(2026, 6, 30),
                confirmed_balance_amount=Decimal("950"),
            ),
            timestamp_provider=lambda: fixed_now,
        )

        old = db.session.get(CashBalanceCheckpoint, first.checkpoint_id)
        assert second.superseded_checkpoint_id == first.checkpoint_id
        assert second.prior_calculated_balance_amount == Decimal("1000.000000000000")
        assert second.correction_amount == Decimal("-50.000000000000")
        assert old.superseded_at == fixed_now
        active_count = db.session.scalar(
            select(func.count(CashBalanceCheckpoint.id)).where(
                CashBalanceCheckpoint.superseded_at.is_(None)
            )
        )
        assert active_count == 1


def test_included_aggregate_is_explicit_and_blocks_confirmation(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _account(mode="included_in_aggregate")
        result = resolve_cash(
            portfolio.id, account.id, "EUR", date(2026, 8, 4)
        )

        assert result.amount is None
        assert result.status == "included_in_aggregate"
        assert result.source_mode == "aggregate"
        with pytest.raises(CashValidationError) as raised:
            _confirmation(
                portfolio, account, on=date(2026, 8, 4), amount="1000"
            )
        assert raised.value.field == "currency_code"
        assert "count the same value twice" in raised.value.message
        assert db.session.scalar(select(func.count(CashBalanceCheckpoint.id))) == 0


def test_cash_setup_requires_each_known_separate_currency(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _account(multicurrency=True)
        _cash_effect(
            portfolio,
            account,
            on=date(2026, 6, 1),
            amount="500",
            currency="USD",
        )

        assert cash_setup_complete(portfolio.id) is False
        _confirmation(portfolio, account, on=date(2026, 6, 30), amount="1000")
        assert cash_setup_complete(portfolio.id) is False
        confirm_cash(
            CashConfirmationCommand(
                portfolio_id=portfolio.id,
                account_id=account.id,
                currency_code="USD",
                effective_date=date(2026, 6, 30),
                confirmed_balance_amount=Decimal("500"),
            )
        )
        assert cash_setup_complete(portfolio.id) is True


def test_cash_setup_is_complete_when_every_account_includes_cash(app: Flask) -> None:
    with app.app_context():
        portfolio, _ = _account(mode="included_in_aggregate")
        assert cash_setup_complete(portfolio.id) is True


def test_backdated_warning_is_added_to_trade_preview(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _account()
        instrument = Instrument(
            portfolio_id=portfolio.id,
            name="Fund",
            instrument_type="fund",
            valuation_currency_code="EUR",
            is_active=True,
        )
        db.session.add(instrument)
        db.session.commit()
        _confirmation(
            portfolio, account, on=date(2026, 6, 30), amount="1000"
        )

        warning = backdated_cash_warning(
            portfolio.id, account.id, "EUR", date(2026, 6, 30)
        )
        command = TradeCommand(
            portfolio_id=portfolio.id,
            activity_type="buy",
            effective_date=date(2026, 6, 29),
            account_id=account.id,
            instrument_id=instrument.id,
            quantity=Decimal("10"),
            unit_price=Decimal("12"),
        )
        preview = preview_trade(command)

        assert warning is not None
        assert len(preview.warnings) == 1
        assert "not current confirmed-and-carried-forward cash" in preview.warnings[0]
        post_trade(command)
        current = resolve_cash(
            portfolio.id, account.id, "EUR", date(2026, 7, 1)
        )
        assert current.amount == Decimal("1000.000000000000")
        assert current.later_cash_effect_amount == Decimal("0")


def test_negative_confirmation_is_allowed_with_warning(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _account()
        saved = _confirmation(
            portfolio, account, on=date(2026, 8, 4), amount="-250"
        )
        result = resolve_cash(
            portfolio.id, account.id, "EUR", date(2026, 8, 4)
        )

        assert saved.warnings and "negative" in saved.warnings[0]
        assert result.amount == Decimal("-250.000000000000")
        assert result.warnings


def test_replacement_commit_failure_restores_prior_active_checkpoint(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        portfolio, account = _account()
        original = _confirmation(
            portfolio, account, on=date(2026, 6, 30), amount="1000"
        )

        def fail_commit() -> None:
            db.session.flush()
            raise RuntimeError("injected failure")

        monkeypatch.setattr(db.session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected failure"):
            _confirmation(
                portfolio, account, on=date(2026, 6, 30), amount="950"
            )

        old = db.session.get(CashBalanceCheckpoint, original.checkpoint_id)
        assert old.superseded_at is None
        assert db.session.scalar(select(func.count(CashBalanceCheckpoint.id))) == 1
