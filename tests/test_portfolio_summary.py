"""Exact portfolio-summary totals and explicit incomplete value states."""

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
    Price,
    Transaction,
    ValuationObservation,
)
from app.services.portfolio_summary import build_portfolio_summary
from app.services.activity_history import ReversalCommand, reverse_activity


AS_OF = date(2026, 8, 2)


def _portfolio_and_account():
    portfolio = Portfolio(
        name="Portfolio",
        reporting_currency_code="EUR",
        annual_spending_amount=Decimal("48000"),
        default_as_of_date=AS_OF,
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
        is_multicurrency=True,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        relationship_eligible=True,
        is_active=True,
    )
    db.session.add(account)
    db.session.flush()
    return portfolio, account


def _transaction_position(portfolio, account, *, name: str, currency: str, quantity: str):
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name=name,
        instrument_type="fund",
        valuation_currency_code=currency,
        is_active=True,
    )
    db.session.add(instrument)
    db.session.flush()
    registration = PositionRegistration(
        account_id=account.id,
        instrument_id=instrument.id,
        tracking_mode="transaction_tracked",
        opening_date=date(2026, 7, 1),
    )
    transaction = Transaction(
        portfolio_id=portfolio.id,
        transaction_type="opening_balance",
        effective_date=date(2026, 7, 1),
        status="posted",
    )
    db.session.add_all([registration, transaction])
    db.session.flush()
    db.session.add(
        Posting(
            transaction_id=transaction.id,
            account_id=account.id,
            posting_kind="instrument",
            instrument_id=instrument.id,
            currency_code=currency,
            quantity_delta=Decimal(quantity),
        )
    )
    return instrument, registration


def _statement_position(portfolio, account, *, name: str, currency: str, amount: str):
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name=name,
        instrument_type="other",
        valuation_currency_code=currency,
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
            effective_date=date(2026, 8, 1),
            native_value_amount=Decimal(amount),
            currency_code=currency,
        )
    )
    return instrument, registration


def test_summary_exactly_totals_both_tracking_modes_and_retains_traceability(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_and_account()
        fund, _ = _transaction_position(
            portfolio, account, name="USD Fund", currency="USD", quantity="100"
        )
        _statement_position(
            portfolio, account, name="SGD Wrapper", currency="SGD", amount="1000"
        )
        db.session.add_all(
            [
                Price(
                    instrument_id=fund.id,
                    effective_date=date(2026, 8, 1),
                    price_amount=Decimal("20"),
                    currency_code="USD",
                ),
                FxRate(
                    effective_date=date(2026, 8, 1),
                    base_currency_code="USD",
                    quote_currency_code="EUR",
                    quote_per_base_amount=Decimal("0.9"),
                ),
                FxRate(
                    effective_date=date(2026, 8, 1),
                    base_currency_code="SGD",
                    quote_currency_code="EUR",
                    quote_per_base_amount=Decimal("0.7"),
                ),
            ]
        )
        db.session.commit()

        summary = build_portfolio_summary(portfolio, AS_OF)

        assert summary["total_reporting_amount"] == Decimal("2500.000000000000000000000000")
        assert summary["total_status"] == "current"
        assert summary["reporting_valued_count"] == 2
        assert summary["missing_count"] == 0
        assert summary["attention_items"] == []
        assert summary["native_totals"] == [
            {"currency": "SGD", "amount": Decimal("1000.000000000000")},
            {"currency": "USD", "amount": Decimal("2000.000000000000000000000000")},
        ]
        currency_totals = {
            row["native_currency"]: row
            for row in summary["reporting_totals_by_currency"]
        }
        assert currency_totals["SGD"]["included_reporting_amount"] == Decimal(
            "700.0000000000000000000000000"
        )
        assert currency_totals["SGD"]["included_percentage"] == Decimal("0.28")
        assert currency_totals["USD"]["included_reporting_amount"] == Decimal(
            "1800.000000000000000000000000"
        )
        assert currency_totals["USD"]["included_percentage"] == Decimal("0.72")
        assert all(row["status"] == "current" for row in currency_totals.values())
        assert summary["reporting_totals_by_institution"] == [
            {
                "institution_name": "Broker",
                "item_count": 2,
                "reporting_valued_count": 2,
                "stale_count": 0,
                "included_reporting_amount": Decimal(
                    "2500.000000000000000000000000"
                ),
                "included_percentage": Decimal("1"),
                "native_valued_count": 0,
                "native_amount": None,
                "status": "current",
            }
        ]
        rows = {row["instrument_name"]: row for row in summary["holdings"]}
        assert rows["USD Fund"]["source_mode"] == "price"
        assert rows["USD Fund"]["value_date"] == date(2026, 8, 1)
        assert rows["USD Fund"]["fx_date"] == date(2026, 8, 1)
        assert rows["SGD Wrapper"]["tracking_mode"] == "statement_valued"


def test_partial_total_is_known_subtotal_and_missing_fx_stays_visible(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_and_account()
        euro_fund, _ = _transaction_position(
            portfolio, account, name="EUR Fund", currency="EUR", quantity="100"
        )
        _statement_position(
            portfolio, account, name="USD Wrapper", currency="USD", amount="2000"
        )
        db.session.add(
            Price(
                instrument_id=euro_fund.id,
                effective_date=date(2026, 8, 1),
                price_amount=Decimal("10"),
                currency_code="EUR",
            )
        )
        db.session.commit()

        summary = build_portfolio_summary(portfolio, AS_OF)

        assert summary["total_reporting_amount"] == Decimal("1000.000000000000000000000000")
        assert summary["total_status"] == "partial"
        assert summary["reporting_valued_count"] == 1
        assert summary["missing_count"] == 1
        assert summary["native_totals"] == [
            {"currency": "EUR", "amount": Decimal("1000.000000000000000000000000")},
            {"currency": "USD", "amount": Decimal("2000.000000000000")},
        ]
        assert summary["attention_items"][0]["kind"] == "missing"
        assert summary["attention_items"][0]["detail"] == "No eligible FX path from USD to EUR"
        assert summary["attention_items"][0]["missing_source"] == "fx"
        assert summary["attention_items"][0]["native_currency"] == "USD"
        assert summary["attention_items"][0]["reporting_currency"] == "EUR"
        currency_totals = {
            row["native_currency"]: row
            for row in summary["reporting_totals_by_currency"]
        }
        assert currency_totals["EUR"]["status"] == "current"
        assert currency_totals["USD"]["status"] == "missing"
        assert currency_totals["USD"]["native_amount"] == Decimal("2000")
        assert currency_totals["USD"]["included_reporting_amount"] is None
        assert currency_totals["EUR"]["included_percentage"] == Decimal("1")
        assert currency_totals["USD"]["included_percentage"] is None
        institution = summary["reporting_totals_by_institution"][0]
        assert institution["status"] == "partial"
        assert institution["reporting_valued_count"] == 1
        assert institution["item_count"] == 2


def test_all_missing_positions_return_none_instead_of_an_invented_zero(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_and_account()
        _transaction_position(
            portfolio, account, name="Unpriced fund", currency="EUR", quantity="100"
        )
        db.session.commit()

        summary = build_portfolio_summary(portfolio, AS_OF)

        assert summary["total_reporting_amount"] is None
        assert summary["total_status"] == "missing"
        assert summary["reporting_valued_count"] == 0
        assert summary["missing_count"] == 1
        assert summary["native_totals"] == []
        assert summary["holdings"][0]["reporting_amount"] is None


def test_complete_but_old_values_make_the_total_stale(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_and_account()
        _, registration = _statement_position(
            portfolio, account, name="Old statement", currency="EUR", amount="5000"
        )
        # Replace the fresh opening observation with an old one.
        row = db.session.query(ValuationObservation).filter_by(
            position_registration_id=registration.id
        ).one()
        row.effective_date = date(2026, 6, 1)
        db.session.commit()

        summary = build_portfolio_summary(portfolio, AS_OF)

        assert summary["total_reporting_amount"] == Decimal("5000.000000000000")
        assert summary["total_status"] == "stale"
        assert summary["stale_count"] == 1
        assert summary["attention_items"][0]["kind"] == "stale"
        assert summary["attention_items"][0]["stale_sources"] == ("statement",)
        assert summary["attention_items"][0]["action_source"] == "statement"
        assert summary["attention_items"][0]["instrument_id"] == registration.instrument_id
        assert summary["attention_items"][0]["detail"] == (
            "The latest eligible statement value is older than its freshness setting."
        )


def test_one_instrument_price_attention_names_every_affected_account(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, first_account = _portfolio_and_account()
        second_account = Account(
            portfolio_id=portfolio.id,
            institution_id=first_account.institution_id,
            name="Second brokerage",
            account_type="brokerage",
            default_currency_code="EUR",
            is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add(second_account)
        db.session.flush()
        instrument, _ = _transaction_position(
            portfolio,
            first_account,
            name="Shared stale fund",
            currency="EUR",
            quantity="10",
        )
        second_registration = PositionRegistration(
            account_id=second_account.id,
            instrument_id=instrument.id,
            tracking_mode="transaction_tracked",
            opening_date=date(2026, 7, 1),
        )
        second_opening = Transaction(
            portfolio_id=portfolio.id,
            transaction_type="opening_balance",
            effective_date=date(2026, 7, 1),
            status="posted",
        )
        db.session.add_all([second_registration, second_opening])
        db.session.flush()
        db.session.add_all(
            [
                Posting(
                    transaction_id=second_opening.id,
                    account_id=second_account.id,
                    posting_kind="instrument",
                    instrument_id=instrument.id,
                    currency_code="EUR",
                    quantity_delta=Decimal("20"),
                ),
                Price(
                    instrument_id=instrument.id,
                    effective_date=date(2026, 6, 1),
                    price_amount=Decimal("10"),
                    currency_code="EUR",
                ),
            ]
        )
        db.session.commit()

        summary = build_portfolio_summary(portfolio, AS_OF)

        assert len(summary["value_attention_items"]) == 1
        warning = summary["value_attention_items"][0]
        assert warning["action_source"] == "price"
        assert warning["instrument_id"] == instrument.id
        assert warning["affected_count"] == 2
        assert warning["affected_account_names"] == (
            "Brokerage",
            "Second brokerage",
        )


def test_fully_sold_position_is_omitted_current_but_visible_historically_and_after_reversal(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_and_account()
        instrument, _ = _transaction_position(
            portfolio,
            account,
            name="Exited fractional fund",
            currency="EUR",
            quantity="1247.634",
        )
        sale = Transaction(
            portfolio_id=portfolio.id,
            transaction_type="sell",
            effective_date=date(2026, 8, 3),
            status="posted",
        )
        db.session.add(sale)
        db.session.flush()
        db.session.add(
            Posting(
                transaction_id=sale.id,
                account_id=account.id,
                posting_kind="instrument",
                instrument_id=instrument.id,
                currency_code="EUR",
                quantity_delta=Decimal("-1247.634"),
            )
        )
        db.session.commit()
        sale_id = sale.id

        historical = build_portfolio_summary(portfolio, date(2026, 8, 2))
        current = build_portfolio_summary(portfolio, date(2026, 8, 4))

        assert historical["position_count"] == 1
        assert historical["holdings"][0]["instrument_name"] == "Exited fractional fund"
        assert historical["holdings"][0]["quantity"] == Decimal(
            "1247.634000000000"
        )
        assert current["holdings"] == []
        assert current["position_count"] == 0
        assert current["reporting_valued_count"] == 0
        assert current["missing_count"] == 0
        assert current["attention_items"] == []
        assert current["native_totals"] == []
        assert current["total_reporting_amount"] is None
        assert current["total_status"] == "empty"

        reverse_activity(
            ReversalCommand(
                portfolio_id=portfolio.id,
                transaction_id=sale_id,
                reason="Sale entered in error",
            )
        )
        restored = build_portfolio_summary(portfolio, date(2026, 8, 4))
        assert restored["position_count"] == 1
        assert restored["holdings"][0]["instrument_name"] == "Exited fractional fund"


def test_zero_statement_value_remains_a_visible_statement_valued_position(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_and_account()
        _statement_position(
            portfolio,
            account,
            name="Zero statement wrapper",
            currency="EUR",
            amount="0",
        )
        db.session.commit()

        summary = build_portfolio_summary(portfolio, AS_OF)

        assert summary["position_count"] == 1
        assert summary["reporting_valued_count"] == 1
        assert summary["total_reporting_amount"] == Decimal("0")
        assert summary["holdings"][0]["tracking_mode"] == "statement_valued"
        assert summary["holdings"][0]["quantity"] is None
