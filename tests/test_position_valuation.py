"""Golden tests for opening quantities, FX resolution, and both valuation modes."""

from datetime import date
from decimal import Decimal

import pytest
from flask import Flask
from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    Account, FxRate, Institution, Instrument, Portfolio, PositionRegistration,
    Posting, Price, Transaction, ValuationObservation,
)
from app.services.fx import resolve_fx
from app.services.positions import quantity_as_of
from app.services.valuation import (
    StatementValueValidationError,
    record_statement_value,
    value_position,
)


def _base_records():
    portfolio = Portfolio(name="Portfolio", reporting_currency_code="SGD", annual_spending_amount=Decimal("48000"))
    db.session.add(portfolio); db.session.flush()
    institution = Institution(portfolio_id=portfolio.id, name="Broker")
    db.session.add(institution); db.session.flush()
    account = Account(
        portfolio_id=portfolio.id, institution_id=institution.id, name="Account",
        account_type="brokerage", default_currency_code="EUR", is_multicurrency=True,
        cash_tracking_mode="separate_cash", portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"), relationship_eligible=True, is_active=True,
    )
    db.session.add(account); db.session.flush()
    return portfolio, account


def test_opening_quantity_replays_from_posting(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _base_records()
        instrument = Instrument(portfolio_id=portfolio.id, name="Fund", instrument_type="fund", valuation_currency_code="EUR", is_active=True)
        db.session.add(instrument); db.session.flush()
        registration = PositionRegistration(account_id=account.id, instrument_id=instrument.id, tracking_mode="transaction_tracked", opening_date=date(2026, 1, 1))
        transaction = Transaction(portfolio_id=portfolio.id, transaction_type="opening_balance", effective_date=date(2026, 1, 1), status="posted")
        db.session.add_all([registration, transaction]); db.session.flush()
        db.session.add(Posting(transaction_id=transaction.id, account_id=account.id, posting_kind="instrument", instrument_id=instrument.id, currency_code="EUR", quantity_delta=Decimal("1000")))
        db.session.commit()

        assert quantity_as_of(registration, date(2025, 12, 31)) == Decimal("0")
        assert quantity_as_of(registration, date(2026, 1, 1)) == Decimal("1000.000000000000")


def test_direct_inverse_and_pivot_fx_are_deterministic(app: Flask) -> None:
    with app.app_context():
        db.session.add_all([
            FxRate(effective_date=date(2026, 7, 31), base_currency_code="EUR", quote_currency_code="SGD", quote_per_base_amount=Decimal("1.5")),
            FxRate(effective_date=date(2026, 7, 30), base_currency_code="USD", quote_currency_code="EUR", quote_per_base_amount=Decimal("0.9")),
        ])
        db.session.commit()

        direct = resolve_fx("EUR", "SGD", date(2026, 8, 2), stale_days=7)
        inverse = resolve_fx("SGD", "EUR", date(2026, 8, 2), stale_days=7)
        pivot = resolve_fx("USD", "SGD", date(2026, 8, 2), stale_days=7)

        assert direct.rate == Decimal("1.500000000000") and direct.source_mode == "direct"
        assert inverse.rate == Decimal("1") / Decimal("1.500000000000")
        assert inverse.source_mode == "inverse"
        assert pivot.rate == Decimal("1.350000000000000000000000")
        assert pivot.path == ("USD", "EUR", "SGD")
        assert pivot.effective_date == date(2026, 7, 30)


def test_unit_and_statement_values_share_reporting_conversion(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _base_records()
        fund = Instrument(portfolio_id=portfolio.id, name="Fund", instrument_type="fund", valuation_currency_code="EUR", is_active=True)
        wrapper = Instrument(portfolio_id=portfolio.id, name="Wrapper", instrument_type="other", valuation_currency_code="USD", is_active=True)
        db.session.add_all([fund, wrapper]); db.session.flush()
        unit_reg = PositionRegistration(account_id=account.id, instrument_id=fund.id, tracking_mode="transaction_tracked", opening_date=date(2026, 1, 1))
        statement_reg = PositionRegistration(account_id=account.id, instrument_id=wrapper.id, tracking_mode="statement_valued", opening_date=date(2026, 7, 31))
        tx = Transaction(portfolio_id=portfolio.id, transaction_type="opening_balance", effective_date=date(2026, 1, 1), status="posted")
        db.session.add_all([unit_reg, statement_reg, tx]); db.session.flush()
        db.session.add_all([
            Posting(transaction_id=tx.id, account_id=account.id, posting_kind="instrument", instrument_id=fund.id, currency_code="EUR", quantity_delta=Decimal("1000")),
            Price(instrument_id=fund.id, effective_date=date(2026, 7, 31), price_amount=Decimal("10.25"), currency_code="EUR"),
            ValuationObservation(position_registration_id=statement_reg.id, effective_date=date(2026, 7, 31), native_value_amount=Decimal("2000"), currency_code="USD"),
            FxRate(effective_date=date(2026, 7, 31), base_currency_code="EUR", quote_currency_code="SGD", quote_per_base_amount=Decimal("1.5")),
            FxRate(effective_date=date(2026, 7, 31), base_currency_code="USD", quote_currency_code="EUR", quote_per_base_amount=Decimal("0.9")),
        ])
        db.session.commit()

        kwargs = dict(reporting_currency="SGD", price_stale_days=14, fx_stale_days=7, statement_stale_days=45)
        unit = value_position(unit_reg, date(2026, 8, 2), **kwargs)
        statement = value_position(statement_reg, date(2026, 8, 2), **kwargs)

        assert unit.native_amount == Decimal("10250.000000000000000000000000")
        assert unit.reporting_amount == Decimal("15375.00000000000000000000000")
        assert statement.quantity is None
        assert statement.native_amount == Decimal("2000.000000000000")
        assert statement.reporting_amount == Decimal("2700.000000000000000000000000")
        assert statement.source_mode == "statement_observation"


def test_eur_sgd_and_usd_positions_share_disclosed_reporting_rates(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account = _base_records()
        instruments = {
            currency: Instrument(
                portfolio_id=portfolio.id,
                name=f"{currency} position",
                instrument_type="other",
                valuation_currency_code=currency,
                is_active=True,
            )
            for currency in ("EUR", "SGD", "USD")
        }
        db.session.add_all(instruments.values())
        db.session.flush()
        registrations = {
            currency: PositionRegistration(
                account_id=account.id,
                instrument_id=instrument.id,
                tracking_mode="statement_valued",
                opening_date=date(2026, 7, 31),
            )
            for currency, instrument in instruments.items()
        }
        db.session.add_all(registrations.values())
        db.session.flush()
        db.session.add_all(
            [
                ValuationObservation(
                    position_registration_id=registration.id,
                    effective_date=date(2026, 7, 31),
                    native_value_amount=Decimal("1000"),
                    currency_code=currency,
                )
                for currency, registration in registrations.items()
            ]
            + [
                FxRate(
                    effective_date=date(2026, 7, 31),
                    base_currency_code="EUR",
                    quote_currency_code="SGD",
                    quote_per_base_amount=Decimal("1.5"),
                ),
                FxRate(
                    effective_date=date(2026, 7, 30),
                    base_currency_code="USD",
                    quote_currency_code="EUR",
                    quote_per_base_amount=Decimal("0.9"),
                ),
            ]
        )
        db.session.commit()

        kwargs = {
            "reporting_currency": "SGD",
            "price_stale_days": 14,
            "fx_stale_days": 7,
            "statement_stale_days": 45,
        }
        values = {
            currency: value_position(registration, date(2026, 8, 2), **kwargs)
            for currency, registration in registrations.items()
        }

        assert values["EUR"].reporting_amount == Decimal("1500.000000000000000000000000")
        assert values["EUR"].fx_date == date(2026, 7, 31)
        assert values["EUR"].fx_path == ("EUR", "SGD")
        assert values["SGD"].reporting_amount == Decimal("1000.000000000000")
        assert values["SGD"].fx_date is None
        assert values["SGD"].fx_path == ("SGD",)
        assert values["USD"].reporting_amount == Decimal("1350.000000000000000000000000")
        assert values["USD"].fx_date == date(2026, 7, 30)
        assert values["USD"].fx_path == ("USD", "EUR", "SGD")


def test_statement_value_service_rejects_closed_registration_without_write(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account = _base_records()
        wrapper = Instrument(
            portfolio_id=portfolio.id,
            name="Closed wrapper",
            instrument_type="other",
            valuation_currency_code="USD",
            is_active=True,
        )
        db.session.add(wrapper)
        db.session.flush()
        registration = PositionRegistration(
            account_id=account.id,
            instrument_id=wrapper.id,
            tracking_mode="statement_valued",
            opening_date=date(2026, 1, 1),
            closing_date=date(2026, 8, 10),
        )
        db.session.add(registration)
        db.session.commit()

        with pytest.raises(StatementValueValidationError) as raised:
            record_statement_value(
                portfolio_id=portfolio.id,
                registration_id=registration.id,
                effective_date=date(2026, 8, 11),
                native_value_amount=Decimal("0"),
                currency_code="USD",
            )

        assert raised.value.field == "registration_id"
        assert "closed" in raised.value.message
        assert db.session.scalar(select(func.count(ValuationObservation.id))) == 0


def test_missing_and_stale_are_explicit_value_states(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _base_records()
        instrument = Instrument(
            portfolio_id=portfolio.id, name="Fund", instrument_type="fund",
            valuation_currency_code="SGD", is_active=True,
        )
        db.session.add(instrument); db.session.flush()
        registration = PositionRegistration(
            account_id=account.id, instrument_id=instrument.id,
            tracking_mode="transaction_tracked", opening_date=date(2026, 1, 1),
        )
        transaction = Transaction(
            portfolio_id=portfolio.id, transaction_type="opening_balance",
            effective_date=date(2026, 1, 1), status="posted",
        )
        db.session.add_all([registration, transaction]); db.session.flush()
        db.session.add(Posting(
            transaction_id=transaction.id, account_id=account.id,
            posting_kind="instrument", instrument_id=instrument.id,
            currency_code="SGD", quantity_delta=Decimal("10"),
        ))
        db.session.commit()

        kwargs = dict(
            reporting_currency="SGD", price_stale_days=14, fx_stale_days=7,
            statement_stale_days=45,
        )
        missing = value_position(registration, date(2026, 8, 2), **kwargs)
        assert missing.status == "missing"
        assert missing.tracking_mode == "transaction_tracked"
        assert missing.source_mode == "price"
        assert missing.missing_reason == "No eligible price"
        assert missing.missing_source == "price"
        assert missing.stale_sources == ()
        assert missing.reporting_amount is None

        db.session.add(Price(
            instrument_id=instrument.id, effective_date=date(2026, 6, 1),
            price_amount=Decimal("12"), currency_code="SGD",
        ))
        db.session.commit()
        stale = value_position(registration, date(2026, 8, 2), **kwargs)
        assert stale.status == "stale"
        assert stale.stale_sources == ("price",)
        assert stale.reporting_amount == Decimal("120.000000000000000000000000")

        wrapper = Instrument(
            portfolio_id=portfolio.id,
            name="Wrapper",
            instrument_type="other",
            valuation_currency_code="USD",
            is_active=True,
        )
        db.session.add(wrapper)
        db.session.flush()
        statement_registration = PositionRegistration(
            account_id=account.id,
            instrument_id=wrapper.id,
            tracking_mode="statement_valued",
            opening_date=date(2026, 8, 1),
        )
        db.session.add(statement_registration)
        db.session.commit()

        statement_missing = value_position(
            statement_registration, date(2026, 8, 2), **kwargs
        )
        assert statement_missing.missing_reason == "No eligible statement value"
        assert statement_missing.missing_source == "statement"


def test_stale_sources_identify_native_and_fx_components(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _base_records()
        instrument = Instrument(
            portfolio_id=portfolio.id,
            name="US Fund",
            instrument_type="fund",
            valuation_currency_code="USD",
            is_active=True,
        )
        db.session.add(instrument)
        db.session.flush()
        registration = PositionRegistration(
            account_id=account.id,
            instrument_id=instrument.id,
            tracking_mode="transaction_tracked",
            opening_date=date(2026, 1, 1),
        )
        transaction = Transaction(
            portfolio_id=portfolio.id,
            transaction_type="opening_balance",
            effective_date=date(2026, 1, 1),
            status="posted",
        )
        db.session.add_all([registration, transaction])
        db.session.flush()
        db.session.add_all([
            Posting(
                transaction_id=transaction.id,
                account_id=account.id,
                posting_kind="instrument",
                instrument_id=instrument.id,
                currency_code="USD",
                quantity_delta=Decimal("10"),
            ),
            Price(
                instrument_id=instrument.id,
                effective_date=date(2026, 6, 1),
                price_amount=Decimal("12"),
                currency_code="USD",
            ),
            FxRate(
                effective_date=date(2026, 6, 1),
                base_currency_code="USD",
                quote_currency_code="SGD",
                quote_per_base_amount=Decimal("1.3"),
            ),
        ])
        db.session.commit()

        kwargs = dict(
            reporting_currency="SGD",
            price_stale_days=14,
            fx_stale_days=7,
            statement_stale_days=45,
        )
        both_stale = value_position(registration, date(2026, 8, 2), **kwargs)
        assert both_stale.status == "stale"
        assert both_stale.stale_sources == ("price", "fx")

        db.session.add(Price(
            instrument_id=instrument.id,
            effective_date=date(2026, 8, 1),
            price_amount=Decimal("13"),
            currency_code="USD",
        ))
        db.session.commit()
        fx_only = value_position(registration, date(2026, 8, 2), **kwargs)
        assert fx_only.stale_sources == ("fx",)

        db.session.add(FxRate(
            effective_date=date(2026, 8, 1),
            base_currency_code="USD",
            quote_currency_code="SGD",
            quote_per_base_amount=Decimal("1.4"),
        ))
        db.session.commit()
        current = value_position(registration, date(2026, 8, 2), **kwargs)
        assert current.status == "current"
        assert current.stale_sources == ()
