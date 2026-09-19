"""Cash-aware portfolio summary contract."""

from datetime import date
from decimal import Decimal

from flask import Flask

from app.extensions import db
from app.models import (
    Account,
    FxRate,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    Posting,
    Transaction,
    ValuationObservation,
)
from app.services.activity_history import ReversalCommand, reverse_activity
from app.services.cash import CashConfirmationCommand, confirm_cash
from app.services.portfolio_summary import build_portfolio_summary


AS_OF = date(2026, 8, 8)


def _portfolio() -> Portfolio:
    portfolio = Portfolio(
        name="Portfolio",
        reporting_currency_code="EUR",
        annual_spending_amount=Decimal("48000"),
        default_as_of_date=AS_OF,
    )
    db.session.add(portfolio)
    db.session.flush()
    return portfolio


def _account(
    portfolio: Portfolio,
    institution: Institution,
    name: str,
    *,
    currency: str = "EUR",
    mode: str = "separate_cash",
    settlement_account: Account | None = None,
) -> Account:
    account = Account(
        portfolio_id=portfolio.id,
        institution_id=institution.id,
        name=name,
        account_type="brokerage",
        default_currency_code=currency,
        is_multicurrency=False,
        cash_tracking_mode=mode,
        cash_settlement_account_id=(
            settlement_account.id if settlement_account is not None else None
        ),
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        relationship_eligible=True,
        is_active=True,
    )
    db.session.add(account)
    db.session.flush()
    return account


def _records(*, currency: str = "EUR") -> tuple[Portfolio, Institution, Account]:
    portfolio = _portfolio()
    institution = Institution(portfolio_id=portfolio.id, name="Bank")
    db.session.add(institution)
    db.session.flush()
    account = _account(
        portfolio, institution, "Main account", currency=currency
    )
    db.session.commit()
    return portfolio, institution, account


def _confirm(
    portfolio: Portfolio,
    account: Account,
    amount: str,
    *,
    currency: str | None = None,
    on: date = date(2026, 8, 7),
) -> None:
    confirm_cash(
        CashConfirmationCommand(
            portfolio_id=portfolio.id,
            account_id=account.id,
            currency_code=currency or account.default_currency_code,
            effective_date=on,
            confirmed_balance_amount=Decimal(amount),
        )
    )


def _statement_value(
    portfolio: Portfolio,
    account: Account,
    amount: str,
) -> None:
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name="Statement investment",
        instrument_type="other",
        valuation_currency_code="EUR",
        is_active=True,
    )
    db.session.add(instrument)
    db.session.flush()
    registration = PositionRegistration(
        account_id=account.id,
        instrument_id=instrument.id,
        tracking_mode="statement_valued",
        opening_date=date(2026, 8, 1),
    )
    db.session.add(registration)
    db.session.flush()
    db.session.add(
        ValuationObservation(
            position_registration_id=registration.id,
            effective_date=date(2026, 8, 7),
            native_value_amount=Decimal(amount),
            currency_code="EUR",
        )
    )
    db.session.commit()


def _cash_effect(
    portfolio: Portfolio,
    account: Account,
    amount: str,
    *,
    on: date,
    currency: str | None = None,
) -> Transaction:
    transaction = Transaction(
        portfolio_id=portfolio.id,
        transaction_type="sell",
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
            currency_code=currency or account.default_currency_code,
            cash_amount_delta=Decimal(amount),
        )
    )
    db.session.commit()
    return transaction


def test_combined_total_keeps_investment_and_cash_subtotals_traceable(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, _, account = _records()
        _statement_value(portfolio, account, "10000")
        _confirm(portfolio, account, "2000")

        summary = build_portfolio_summary(portfolio, AS_OF)

        assert summary["investment_reporting_amount"] == Decimal(
            "10000.000000000000"
        )
        assert summary["cash_reporting_amount"] == Decimal("2000.000000000000")
        assert summary["total_reporting_amount"] == Decimal(
            "12000.000000000000"
        )
        assert summary["investment_status"] == "current"
        assert summary["cash_status"] == "current"
        assert summary["total_status"] == "current"
        assert summary["total_item_count"] == 2
        assert summary["portfolio_native_totals"] == [
            {"currency": "EUR", "amount": Decimal("12000.000000000000")}
        ]
        cash = summary["cash_balances"][0]
        assert cash["account_name"] == "Main account"
        assert cash["source_mode"] == "cash_confirmation"
        assert cash["checkpoint_date"] == date(2026, 8, 7)
        assert cash["fx_source_mode"] == "identity"


def test_untouched_cash_is_not_inferred_zero_but_confirmed_zero_is_real(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, institution, account = _records()
        _account(portfolio, institution, "Untouched account")
        db.session.commit()

        untouched = build_portfolio_summary(portfolio, AS_OF)
        assert untouched["cash_balances"] == []
        assert untouched["cash_reporting_amount"] is None
        assert untouched["total_reporting_amount"] is None
        assert untouched["total_status"] == "empty"

        _confirm(portfolio, account, "0")
        confirmed = build_portfolio_summary(portfolio, AS_OF)
        assert confirmed["cash_balance_count"] == 1
        assert confirmed["cash_reporting_valued_count"] == 1
        assert confirmed["cash_reporting_amount"] == Decimal("0")
        assert confirmed["total_reporting_amount"] == Decimal("0")
        assert confirmed["total_status"] == "current"


def test_missing_cash_fx_keeps_a_known_subtotal_and_actionable_warning(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        institution = Institution(portfolio_id=portfolio.id, name="Bank")
        db.session.add(institution)
        db.session.flush()
        usd = _account(portfolio, institution, "USD cash", currency="USD")
        sgd = _account(portfolio, institution, "SGD cash", currency="SGD")
        db.session.commit()
        _confirm(portfolio, usd, "1000")
        _confirm(portfolio, sgd, "500")
        db.session.add(
            FxRate(
                effective_date=date(2026, 8, 7),
                base_currency_code="USD",
                quote_currency_code="EUR",
                quote_per_base_amount=Decimal("0.9"),
            )
        )
        db.session.commit()

        summary = build_portfolio_summary(portfolio, AS_OF)

        assert summary["cash_reporting_amount"] == Decimal(
            "900.0000000000000"
        )
        assert summary["cash_status"] == "partial"
        assert summary["total_status"] == "partial"
        assert summary["cash_missing_count"] == 1
        assert summary["total_missing_count"] == 1
        missing = summary["attention_items"][0]
        assert missing["subject_kind"] == "cash"
        assert missing["instrument_name"] == "SGD cash"
        assert missing["account_name"] == "SGD cash"
        assert missing["missing_source"] == "fx"
        assert missing["native_currency"] == "SGD"
        assert missing["reporting_currency"] == "EUR"
        assert missing["detail"] == "No eligible FX path from SGD to EUR"


def test_cash_fx_uses_existing_pivot_and_exposes_staleness(app: Flask) -> None:
    with app.app_context():
        portfolio, _, account = _records(currency="USD")
        _confirm(portfolio, account, "1000")
        db.session.add_all(
            [
                FxRate(
                    effective_date=date(2026, 7, 1),
                    base_currency_code="USD",
                    quote_currency_code="GBP",
                    quote_per_base_amount=Decimal("0.8"),
                ),
                FxRate(
                    effective_date=date(2026, 7, 1),
                    base_currency_code="GBP",
                    quote_currency_code="EUR",
                    quote_per_base_amount=Decimal("1.2"),
                ),
            ]
        )
        db.session.commit()

        summary = build_portfolio_summary(portfolio, AS_OF)

        cash = summary["cash_balances"][0]
        assert cash["reporting_amount"] == Decimal("960.00000000000000")
        assert cash["fx_path"] == ("USD", "GBP", "EUR")
        assert cash["fx_source_mode"] == "pivot"
        assert cash["fx_date"] == date(2026, 7, 1)
        assert cash["status"] == "stale"
        assert summary["cash_status"] == "stale"
        assert summary["total_status"] == "stale"
        stale = summary["attention_items"][0]
        assert stale["subject_kind"] == "cash"
        assert stale["stale_sources"] == ("fx",)
        assert stale["action_source"] == "fx"


def test_aggregate_and_settled_elsewhere_cash_never_double_count(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        institution = Institution(portfolio_id=portfolio.id, name="Example Bank")
        db.session.add(institution)
        db.session.flush()
        wma = _account(portfolio, institution, "Settlement SGD", currency="SGD")
        _account(
            portfolio,
            institution,
            "Brokerage SGD",
            currency="SGD",
            settlement_account=wma,
        )
        _account(
            portfolio,
            institution,
            "Aggregate wrapper",
            currency="EUR",
            mode="included_in_aggregate",
        )
        db.session.commit()
        _confirm(portfolio, wma, "1250")
        db.session.add(
            FxRate(
                effective_date=date(2026, 8, 7),
                base_currency_code="SGD",
                quote_currency_code="EUR",
                quote_per_base_amount=Decimal("0.7"),
            )
        )
        db.session.commit()

        summary = build_portfolio_summary(portfolio, AS_OF)

        assert summary["cash_balance_count"] == 1
        assert [row["account_name"] for row in summary["cash_balances"]] == [
            "Settlement SGD"
        ]
        assert summary["cash_reporting_amount"] == Decimal(
            "875.0000000000000"
        )


def test_historical_cash_uses_dated_checkpoint_and_effects_and_reversal(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, _, account = _records()
        _confirm(
            portfolio,
            account,
            "1000",
            on=date(2026, 6, 30),
        )
        sale = _cash_effect(
            portfolio,
            account,
            "250",
            on=date(2026, 7, 5),
        )
        _cash_effect(
            portfolio,
            account,
            "999",
            on=date(2026, 8, 10),
            currency="JPY",
        )

        before_sale = build_portfolio_summary(portfolio, date(2026, 7, 4))
        after_sale = build_portfolio_summary(portfolio, date(2026, 7, 5))
        current = build_portfolio_summary(portfolio, AS_OF)

        assert before_sale["cash_reporting_amount"] == Decimal(
            "1000.000000000000"
        )
        assert after_sale["cash_reporting_amount"] == Decimal(
            "1250.000000000000"
        )
        assert current["cash_balance_count"] == 1
        assert current["cash_balances"][0]["native_currency"] == "EUR"
        assert current["cash_balances"][0]["latest_cash_effect_date"] == date(
            2026, 7, 5
        )

        reverse_activity(
            ReversalCommand(
                portfolio_id=portfolio.id,
                transaction_id=sale.id,
                reason="Sale was entered in error",
            )
        )
        restored = build_portfolio_summary(portfolio, AS_OF)
        assert restored["cash_reporting_amount"] == Decimal(
            "1000.000000000000"
        )
