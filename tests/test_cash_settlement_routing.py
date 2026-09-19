"""Custody positions whose activity cash settles in a linked cash account."""

from datetime import date
from decimal import Decimal

import pytest
from flask import Flask
from sqlalchemy import select

from app.extensions import db
from app.models import (
    Account,
    Institution,
    Instrument,
    Portfolio,
    Posting,
    PositionRegistration,
)
from app.services.activity import TradeCommand, post_trade, preview_trade
from app.services.activity_history import (
    ActivityHistoryFilters,
    ReversalCommand,
    list_activities,
    reverse_activity,
)
from app.services.cash import (
    CashConfirmationCommand,
    CashValidationError,
    cash_setup_complete,
    confirm_cash,
    resolve_cash,
)
from app.services.dividends import DividendCommand, get_dividend_receipt, post_dividend
from app.services.positions import quantity_as_of
from app.services.settlement import (
    SettlementAccountError,
    cash_settlement_account,
    validate_cash_settlement_account,
)


def _records() -> tuple[Portfolio, Account, Account, Instrument]:
    portfolio = Portfolio(
        name="Portfolio",
        reporting_currency_code="EUR",
        annual_spending_amount=Decimal("48000"),
    )
    db.session.add(portfolio)
    db.session.flush()
    institution = Institution(portfolio_id=portfolio.id, name="Example Bank")
    db.session.add(institution)
    db.session.flush()
    brokerage = Account(
        portfolio_id=portfolio.id,
        institution_id=institution.id,
        name="Brokerage SGD",
        account_type="brokerage",
        default_currency_code="SGD",
        is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        relationship_eligible=True,
        is_active=True,
    )
    wma = Account(
        portfolio_id=portfolio.id,
        institution_id=institution.id,
        name="Settlement SGD",
        account_type="cash",
        default_currency_code="SGD",
        is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        relationship_eligible=True,
        is_active=True,
    )
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name="SGD stock",
        instrument_type="stock",
        valuation_currency_code="SGD",
        is_active=True,
    )
    db.session.add_all([brokerage, wma, instrument])
    db.session.flush()
    brokerage.cash_settlement_account_id = wma.id
    db.session.commit()
    return portfolio, brokerage, wma, instrument


def _trade(
    portfolio: Portfolio,
    brokerage: Account,
    instrument: Instrument,
    *,
    activity_type: str,
    effective_date: date,
    quantity: str,
    price: str,
    fee: str = "0",
) -> TradeCommand:
    return TradeCommand(
        portfolio_id=portfolio.id,
        activity_type=activity_type,
        effective_date=effective_date,
        account_id=brokerage.id,
        instrument_id=instrument.id,
        quantity=Decimal(quantity),
        unit_price=Decimal(price),
        fee_amount=Decimal(fee),
    )


def test_sale_keeps_position_in_brokerage_and_settles_cash_in_wma(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, brokerage, wma, instrument = _records()
        opening = post_trade(
            _trade(
                portfolio,
                brokerage,
                instrument,
                activity_type="buy",
                effective_date=date(2026, 8, 1),
                quantity="10",
                price="80",
            )
        )
        confirm_cash(
            CashConfirmationCommand(
                portfolio_id=portfolio.id,
                account_id=wma.id,
                currency_code="SGD",
                effective_date=date(2026, 8, 5),
                confirmed_balance_amount=Decimal("1000"),
            )
        )
        command = _trade(
            portfolio,
            brokerage,
            instrument,
            activity_type="sell",
            effective_date=date(2026, 8, 7),
            quantity="2",
            price="100",
            fee="5",
        )

        preview = preview_trade(command)
        posted = post_trade(command)

        assert preview.account_id == brokerage.id
        assert preview.cash_account_id == wma.id
        assert preview.cash_account_name == "Settlement SGD"
        rows = {
            row.posting_kind: row
            for row in db.session.scalars(
                select(Posting).where(Posting.transaction_id == posted.transaction_id)
            )
        }
        assert rows["instrument"].account_id == brokerage.id
        assert rows["clearing"].account_id == brokerage.id
        assert rows["expense"].account_id == brokerage.id
        assert rows["cash"].account_id == wma.id
        assert rows["cash"].cash_amount_delta == Decimal("195")
        assert resolve_cash(
            portfolio.id, wma.id, "SGD", date(2026, 8, 7)
        ).amount == Decimal("1195")
        brokerage_cash = resolve_cash(
            portfolio.id, brokerage.id, "SGD", date(2026, 8, 7)
        )
        assert brokerage_cash.amount is None
        assert brokerage_cash.status == "settled_elsewhere"

        registration = db.session.get(PositionRegistration, opening.registration_id)
        assert quantity_as_of(registration, date(2026, 8, 7)) == Decimal("8")
        activity = list_activities(
            portfolio.id, ActivityHistoryFilters(account_id=wma.id)
        )[0]
        assert activity.account_id == brokerage.id
        assert activity.account_name == "Brokerage SGD"
        assert activity.cash_effect == Decimal("195")

        reverse_activity(
            ReversalCommand(
                portfolio_id=portfolio.id,
                transaction_id=posted.transaction_id,
                reason="Entered for routing test",
            )
        )
        assert quantity_as_of(registration, date(2026, 8, 7)) == Decimal("10")
        assert resolve_cash(
            portfolio.id, wma.id, "SGD", date(2026, 8, 7)
        ).amount == Decimal("1000")


def test_routed_custody_account_cannot_accept_its_own_cash_confirmation(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, brokerage, wma, _ = _records()

        with pytest.raises(CashValidationError, match="settles in Settlement SGD"):
            confirm_cash(
                CashConfirmationCommand(
                    portfolio_id=portfolio.id,
                    account_id=brokerage.id,
                    currency_code="SGD",
                    effective_date=date(2026, 8, 8),
                    confirmed_balance_amount=Decimal("10"),
                )
            )

        confirm_cash(
            CashConfirmationCommand(
                portfolio_id=portfolio.id,
                account_id=wma.id,
                currency_code="SGD",
                effective_date=date(2026, 8, 8),
                confirmed_balance_amount=Decimal("10"),
            )
        )
        assert cash_setup_complete(portfolio.id) is True


def test_backdated_warning_uses_the_linked_cash_account_checkpoint(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, brokerage, wma, instrument = _records()
        confirm_cash(
            CashConfirmationCommand(
                portfolio_id=portfolio.id,
                account_id=wma.id,
                currency_code="SGD",
                effective_date=date(2026, 8, 5),
                confirmed_balance_amount=Decimal("1000"),
            )
        )

        preview = preview_trade(
            _trade(
                portfolio,
                brokerage,
                instrument,
                activity_type="buy",
                effective_date=date(2026, 8, 4),
                quantity="1",
                price="100",
            )
        )

        assert any("2026-08-05" in warning for warning in preview.warnings)


def test_reinvested_dividend_routes_both_cash_legs_to_the_same_wma(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, brokerage, wma, instrument = _records()
        db.session.add(
            PositionRegistration(
                account_id=brokerage.id,
                instrument_id=instrument.id,
                tracking_mode="transaction_tracked",
                opening_date=date(2026, 1, 1),
            )
        )
        db.session.commit()

        posted = post_dividend(
            DividendCommand(
                portfolio_id=portfolio.id,
                effective_date=date(2026, 8, 7),
                account_id=brokerage.id,
                instrument_id=instrument.id,
                currency_code="SGD",
                net_amount=Decimal("100"),
                outcome="reinvest_same",
                reinvestment_quantity=Decimal("2"),
                reinvestment_unit_price=Decimal("50"),
            ),
            group_id_provider=lambda: "linked-settlement-dividend",
        )

        cash_postings = list(
            db.session.scalars(
                select(Posting)
                .where(
                    Posting.transaction_id.in_(
                        [posted.dividend_transaction_id, posted.buy_transaction_id]
                    ),
                    Posting.posting_kind == "cash",
                )
                .order_by(Posting.id)
            )
        )
        assert [row.account_id for row in cash_postings] == [wma.id, wma.id]
        assert [row.cash_amount_delta for row in cash_postings] == [
            Decimal("100"),
            Decimal("-100"),
        ]
        assert resolve_cash(
            portfolio.id, wma.id, "SGD", date(2026, 8, 7)
        ).amount == Decimal("0")


def test_dividend_income_stays_with_custody_and_cash_settles_in_wma(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, brokerage, wma, instrument = _records()
        registration = PositionRegistration(
            account_id=brokerage.id,
            instrument_id=instrument.id,
            tracking_mode="transaction_tracked",
            opening_date=date(2026, 1, 1),
        )
        db.session.add(registration)
        db.session.commit()

        posted = post_dividend(
            DividendCommand(
                portfolio_id=portfolio.id,
                effective_date=date(2026, 8, 7),
                account_id=brokerage.id,
                instrument_id=instrument.id,
                currency_code="SGD",
                net_amount=Decimal("425"),
                gross_amount=Decimal("500"),
                withholding_amount=Decimal("75"),
                outcome="cash",
            )
        )
        rows = {
            row.posting_kind: row
            for row in db.session.scalars(
                select(Posting).where(
                    Posting.transaction_id == posted.dividend_transaction_id
                )
            )
        }

        assert rows["income"].account_id == brokerage.id
        assert rows["expense"].account_id == brokerage.id
        assert rows["cash"].account_id == wma.id
        assert resolve_cash(
            portfolio.id, wma.id, "SGD", date(2026, 8, 7)
        ).amount == Decimal("425")
        receipt = get_dividend_receipt(
            posted.dividend_transaction_id, portfolio_id=portfolio.id
        )
        assert receipt is not None
        assert receipt.account_name == "Brokerage SGD"
        assert receipt.cash_account_id == wma.id
        assert receipt.cash_account_name == "Settlement SGD"


def test_settlement_link_rejects_cross_institution_aggregate_and_chained_targets(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, brokerage, wma, _ = _records()
        other_institution = Institution(portfolio_id=portfolio.id, name="Other bank")
        db.session.add(other_institution)
        db.session.flush()
        other = Account(
            portfolio_id=portfolio.id,
            institution_id=other_institution.id,
            name="Other SGD",
            account_type="cash",
            default_currency_code="SGD",
            is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add(other)
        db.session.flush()

        with pytest.raises(SettlementAccountError, match="same institution"):
            validate_cash_settlement_account(brokerage, other)

        wma.cash_tracking_mode = "included_in_aggregate"
        with pytest.raises(SettlementAccountError, match="separately"):
            validate_cash_settlement_account(brokerage, wma)

        wma.cash_tracking_mode = "separate_cash"
        wma.cash_settlement_account_id = other.id
        with pytest.raises(SettlementAccountError, match="direct"):
            validate_cash_settlement_account(brokerage, wma)

        wma.cash_settlement_account_id = None
        assert cash_settlement_account(brokerage, "SGD").id == wma.id
