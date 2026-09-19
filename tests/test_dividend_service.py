"""Dividend arithmetic, posting, grouping, and cash-cutoff contracts."""

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
from app.services.activity import ActivityValidationError
from app.services.cash import CashConfirmationCommand, confirm_cash, resolve_cash
from app.services.dividends import (
    DividendCommand,
    get_dividend_receipt,
    post_dividend,
    preview_dividend,
)
from app.services.positions import quantity_as_of


PAYMENT_DATE = date(2026, 8, 4)


def _records(
    *,
    cash_mode: str = "separate_cash",
    multicurrency: bool = False,
    tracking_mode: str = "transaction_tracked",
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
        default_currency_code="EUR",
        is_multicurrency=multicurrency,
        cash_tracking_mode=cash_mode,
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        relationship_eligible=True,
        is_active=True,
    )
    source = Instrument(
        portfolio_id=portfolio.id,
        name="Income Fund",
        instrument_type="fund",
        valuation_currency_code="EUR",
        is_active=True,
    )
    target = Instrument(
        portfolio_id=portfolio.id,
        name="Bond ETF",
        instrument_type="etf",
        valuation_currency_code="EUR",
        is_active=True,
    )
    db.session.add_all([account, source, target])
    db.session.flush()
    registration = PositionRegistration(
        account_id=account.id,
        instrument_id=source.id,
        tracking_mode=tracking_mode,
        opening_date=date(2026, 1, 1),
    )
    db.session.add(registration)
    db.session.commit()
    return portfolio, account, source, target, registration


def _command(
    portfolio: Portfolio,
    account: Account,
    source: Instrument,
    **overrides,
) -> DividendCommand:
    values = {
        "portfolio_id": portfolio.id,
        "effective_date": PAYMENT_DATE,
        "account_id": account.id,
        "instrument_id": source.id,
        "currency_code": "EUR",
        "net_amount": Decimal("425"),
        "outcome": "cash",
    }
    values.update(overrides)
    return DividendCommand(**values)


def _postings(transaction_id: int) -> dict[str, Posting]:
    return {
        row.posting_kind: row
        for row in db.session.scalars(
            select(Posting)
            .where(Posting.transaction_id == transaction_id)
            .order_by(Posting.id)
        )
    }


def test_net_only_preview_invents_no_gross_or_withholding(app: Flask) -> None:
    with app.app_context():
        portfolio, account, source, _, _ = _records()

        preview = preview_dividend(_command(portfolio, account, source))

        assert preview.net_amount == Decimal("425")
        assert preview.gross_amount is None
        assert preview.income_amount == Decimal("425")
        assert preview.withholding_amount == Decimal("0")
        assert preview.cash_effect == Decimal("425")
        assert preview.buy_preview is None
        assert db.session.scalar(select(func.count(Transaction.id))) == 0
        assert db.session.scalar(select(func.count(Posting.id))) == 0


def test_cash_dividend_golden_500_75_425_postings_and_cash(app: Flask) -> None:
    with app.app_context():
        portfolio, account, source, _, _ = _records()

        posted = post_dividend(
            _command(
                portfolio,
                account,
                source,
                gross_amount=Decimal("500"),
                withholding_amount=Decimal("75"),
            )
        )

        transaction = db.session.get(Transaction, posted.dividend_transaction_id)
        rows = _postings(transaction.id)
        assert transaction.transaction_type == "dividend"
        assert transaction.activity_group_id is None
        assert rows["income"].cash_amount_delta == Decimal("-500.000000000000")
        assert rows["expense"].cash_amount_delta == Decimal("75.000000000000")
        assert rows["cash"].cash_amount_delta == Decimal("425.000000000000")
        assert sum(row.cash_amount_delta for row in rows.values()) == Decimal("0")
        cash = resolve_cash(portfolio.id, account.id, "EUR", PAYMENT_DATE)
        assert cash.amount == Decimal("425.000000000000")
        receipt = get_dividend_receipt(
            transaction.id,
            portfolio_id=portfolio.id,
        )
        assert receipt.income_amount == Decimal("500.000000000000")
        assert receipt.withholding_amount == Decimal("75.000000000000")
        assert receipt.net_amount == Decimal("425.000000000000")


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        (
            {
                "gross_amount": Decimal("500"),
                "withholding_amount": Decimal("74"),
            },
            "net_amount",
        ),
        ({"withholding_amount": Decimal("75")}, "gross_amount"),
    ],
)
def test_gross_and_withholding_must_be_known_and_reconcile_exactly(
    app: Flask,
    overrides: dict,
    field: str,
) -> None:
    with app.app_context():
        portfolio, account, source, _, _ = _records()

        with pytest.raises(ActivityValidationError) as raised:
            post_dividend(_command(portfolio, account, source, **overrides))

        assert raised.value.field == field
        assert db.session.scalar(select(func.count(Transaction.id))) == 0
        assert db.session.scalar(select(func.count(Posting.id))) == 0


def test_reinvest_same_golden_group_adds_40_units_and_zero_cash(app: Flask) -> None:
    with app.app_context():
        portfolio, account, source, _, registration = _records()

        posted = post_dividend(
            _command(
                portfolio,
                account,
                source,
                outcome="reinvest_same",
                gross_amount=Decimal("500"),
                withholding_amount=Decimal("75"),
                reinvestment_quantity=Decimal("40"),
                reinvestment_unit_price=Decimal("10.60"),
                reinvestment_fee_amount=Decimal("1"),
            ),
            group_id_provider=lambda: "dividend-group-1",
        )

        dividend = db.session.get(Transaction, posted.dividend_transaction_id)
        buy = db.session.get(Transaction, posted.buy_transaction_id)
        assert dividend.activity_group_id == "dividend-group-1"
        assert buy.activity_group_id == "dividend-group-1"
        assert {dividend.transaction_type, buy.transaction_type} == {
            "dividend",
            "buy",
        }
        dividend_rows = _postings(dividend.id)
        buy_rows = _postings(buy.id)
        assert dividend_rows["cash"].cash_amount_delta == Decimal("425")
        assert buy_rows["cash"].cash_amount_delta == Decimal("-425")
        assert buy_rows["instrument"].quantity_delta == Decimal("40")
        assert buy_rows["clearing"].cash_amount_delta == Decimal("424")
        assert buy_rows["expense"].cash_amount_delta == Decimal("1")
        assert quantity_as_of(registration, PAYMENT_DATE) == Decimal("40")
        assert resolve_cash(
            portfolio.id, account.id, "EUR", PAYMENT_DATE
        ).amount == Decimal("0")
        assert posted.preview.cash_effect == Decimal("0.00")
        assert not any(row.transaction_type == "deposit" for row in (dividend, buy))


def test_reinvest_other_accepts_purchase_amount_and_opens_target_position(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, source, target, _ = _records()

        posted = post_dividend(
            _command(
                portfolio,
                account,
                source,
                outcome="reinvest_other",
                reinvestment_instrument_id=target.id,
                reinvestment_quantity=Decimal("3"),
                reinvestment_purchase_amount=Decimal("100"),
            )
        )

        target_registration = db.session.scalar(
            select(PositionRegistration).where(
                PositionRegistration.account_id == account.id,
                PositionRegistration.instrument_id == target.id,
            )
        )
        buy_rows = _postings(posted.buy_transaction_id)
        assert target_registration.tracking_mode == "transaction_tracked"
        assert quantity_as_of(target_registration, PAYMENT_DATE) == Decimal("3")
        assert buy_rows["clearing"].cash_amount_delta == Decimal("100")
        receipt = get_dividend_receipt(
            posted.dividend_transaction_id,
            portfolio_id=portfolio.id,
        )
        assert receipt.outcome == "reinvest_other"
        assert receipt.reinvestment_purchase_amount == Decimal("100")
        assert receipt.cash_effect == Decimal("325")


def test_reinvestment_price_and_purchase_amount_must_reconcile(app: Flask) -> None:
    with app.app_context():
        portfolio, account, source, _, _ = _records()

        with pytest.raises(ActivityValidationError) as raised:
            preview_dividend(
                _command(
                    portfolio,
                    account,
                    source,
                    outcome="reinvest_same",
                    reinvestment_quantity=Decimal("40"),
                    reinvestment_unit_price=Decimal("10.60"),
                    reinvestment_purchase_amount=Decimal("425"),
                )
            )

        assert raised.value.field == "reinvestment_purchase_amount"
        assert "exactly" in raised.value.message


def test_reinvestment_cannot_add_units_to_statement_valued_position(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, source, _, _ = _records(
            tracking_mode="statement_valued"
        )

        with pytest.raises(ActivityValidationError) as raised:
            post_dividend(
                _command(
                    portfolio,
                    account,
                    source,
                    outcome="reinvest_same",
                    reinvestment_quantity=Decimal("10"),
                    reinvestment_unit_price=Decimal("10"),
                )
            )

        assert raised.value.field == "instrument_id"
        assert "statement value" in raised.value.message
        assert db.session.scalar(select(func.count(Transaction.id))) == 0


@pytest.mark.parametrize("instrument_type", ["cash", "fixed_deposit"])
def test_dividend_service_rejects_non_dividend_instrument_types(
    app: Flask, instrument_type: str
) -> None:
    with app.app_context():
        portfolio, account, source, _, _ = _records()
        source.instrument_type = instrument_type
        db.session.commit()

        with pytest.raises(ActivityValidationError) as raised:
            preview_dividend(_command(portfolio, account, source))

        assert raised.value.field == "instrument_id"
        assert "do not produce dividend activities" in raised.value.message
        assert db.session.scalar(select(func.count(Transaction.id))) == 0


def test_cross_currency_reinvestment_is_blocked_before_amounts_are_combined(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, source, target, _ = _records(multicurrency=True)
        target.valuation_currency_code = "USD"
        db.session.commit()

        with pytest.raises(ActivityValidationError) as raised:
            preview_dividend(
                _command(
                    portfolio,
                    account,
                    source,
                    outcome="reinvest_other",
                    reinvestment_instrument_id=target.id,
                    reinvestment_quantity=Decimal("10"),
                    reinvestment_purchase_amount=Decimal("100"),
                )
            )

        assert raised.value.field == "reinvestment_instrument_id"
        assert "same currency" in raised.value.message
        assert db.session.scalar(select(func.count(Transaction.id))) == 0


def test_included_aggregate_warns_and_never_creates_separate_cash_value(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, source, _, _ = _records(
            cash_mode="included_in_aggregate"
        )

        preview = preview_dividend(_command(portfolio, account, source))
        post_dividend(_command(portfolio, account, source))
        cash = resolve_cash(portfolio.id, account.id, "EUR", PAYMENT_DATE)

        assert preview.warnings
        assert "not valued as a separate balance" in preview.warnings[0]
        assert cash.amount is None
        assert cash.status == "included_in_aggregate"


def test_later_cash_confirmation_controls_cash_but_not_dividend_income(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, source, _, _ = _records()
        confirm_cash(
            CashConfirmationCommand(
                portfolio_id=portfolio.id,
                account_id=account.id,
                currency_code="EUR",
                effective_date=date(2026, 8, 5),
                confirmed_balance_amount=Decimal("1000"),
            )
        )

        preview = preview_dividend(_command(portfolio, account, source))
        posted = post_dividend(_command(portfolio, account, source))
        cash = resolve_cash(
            portfolio.id,
            account.id,
            "EUR",
            date(2026, 8, 5),
        )

        assert any("not current confirmed" in row for row in preview.warnings)
        assert cash.amount == Decimal("1000")
        assert _postings(posted.dividend_transaction_id)[
            "income"
        ].cash_amount_delta == Decimal("-425")


def test_omitted_dividend_can_be_absorbed_by_confirmation_without_income(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, _, _, _ = _records()
        confirm_cash(
            CashConfirmationCommand(
                portfolio_id=portfolio.id,
                account_id=account.id,
                currency_code="EUR",
                effective_date=PAYMENT_DATE,
                confirmed_balance_amount=Decimal("425"),
            )
        )

        cash = resolve_cash(portfolio.id, account.id, "EUR", PAYMENT_DATE)
        income_count = db.session.scalar(
            select(func.count(Posting.id)).where(Posting.posting_kind == "income")
        )
        assert cash.amount == Decimal("425")
        assert income_count == 0
        assert db.session.scalar(select(func.count(Transaction.id))) == 0


def test_reinvestment_above_net_uses_cash_without_inventing_contribution(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account, source, _, _ = _records()

        posted = post_dividend(
            _command(
                portfolio,
                account,
                source,
                outcome="reinvest_same",
                reinvestment_quantity=Decimal("50"),
                reinvestment_unit_price=Decimal("10"),
            )
        )

        assert posted.preview.cash_effect == Decimal("-75")
        assert any("reduces account cash" in row for row in posted.preview.warnings)
        transactions = list(db.session.scalars(select(Transaction)))
        assert {row.transaction_type for row in transactions} == {"dividend", "buy"}
        assert all(row.transaction_type != "deposit" for row in transactions)


def test_reinvested_dividend_commit_failure_rolls_back_both_events(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with app.app_context():
        portfolio, account, source, target, _ = _records()
        registration_count = db.session.scalar(
            select(func.count(PositionRegistration.id))
        )

        def fail_commit() -> None:
            db.session.flush()
            raise RuntimeError("injected dividend failure")

        monkeypatch.setattr(db.session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected dividend failure"):
            post_dividend(
                _command(
                    portfolio,
                    account,
                    source,
                    outcome="reinvest_other",
                    reinvestment_instrument_id=target.id,
                    reinvestment_quantity=Decimal("10"),
                    reinvestment_unit_price=Decimal("10"),
                )
            )

        assert db.session.scalar(select(func.count(Transaction.id))) == 0
        assert db.session.scalar(select(func.count(Posting.id))) == 0
        assert (
            db.session.scalar(select(func.count(PositionRegistration.id)))
            == registration_count
        )
