"""Classification, metadata, and allocation contracts."""

from datetime import date
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
    InstrumentClassification,
    Portfolio,
    PositionRegistration,
    Posting,
    Price,
    Transaction,
)
from app.services.classification import (
    ClassificationCommand,
    ClassificationValidationError,
    classifications_as_of,
    save_classification,
)
from app.services.portfolio_summary import build_portfolio_summary


AS_OF = date(2026, 8, 9)


def _portfolio_account(*, share: str = "1", access: str = "1"):
    portfolio = Portfolio(
        name="Portfolio",
        reporting_currency_code="EUR",
        annual_spending_amount=Decimal("48000"),
        default_as_of_date=AS_OF,
    )
    db.session.add(portfolio)
    db.session.flush()
    institution = Institution(portfolio_id=portfolio.id, name="Bank")
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
        portfolio_share_decimal=Decimal(share),
        present_access_decimal=Decimal(access),
        relationship_eligible=True,
        is_active=True,
    )
    db.session.add(account)
    db.session.flush()
    return portfolio, account


def _priced_position(portfolio, account, *, name: str, amount: str):
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name=name,
        instrument_type="fund",
        valuation_currency_code="EUR",
        is_active=True,
    )
    db.session.add(instrument)
    db.session.flush()
    registration = PositionRegistration(
        account_id=account.id,
        instrument_id=instrument.id,
        tracking_mode="transaction_tracked",
        opening_date=date(2026, 8, 1),
    )
    transaction = Transaction(
        portfolio_id=portfolio.id,
        transaction_type="opening_balance",
        effective_date=date(2026, 8, 1),
        status="posted",
    )
    db.session.add_all([registration, transaction])
    db.session.flush()
    db.session.add_all(
        [
            Posting(
                transaction_id=transaction.id,
                account_id=account.id,
                posting_kind="instrument",
                instrument_id=instrument.id,
                currency_code="EUR",
                quantity_delta=Decimal("1"),
            ),
            Price(
                instrument_id=instrument.id,
                effective_date=date(2026, 8, 8),
                price_amount=Decimal(amount),
                currency_code="EUR",
            ),
        ]
    )
    return instrument


def test_simple_classification_saves_dated_role_and_optional_metadata(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        instrument = _priced_position(
            portfolio, account, name="Global Fund", amount="1000"
        )
        db.session.commit()

        snapshot = save_classification(
            portfolio.id,
            ClassificationCommand(
                instrument_id=instrument.id,
                effective_date=date(2026, 8, 8),
                role_weights={"equity": Decimal("1")},
                source_note="Factsheet",
                fund_base_currency_code=" usd ",
                hedging_status="unhedged",
                fire_bucket_code="growth",
                capital_certainty_code="low",
                equity_sensitivity_code="often",
                liquidity_profile_code="days",
                duration_band_code="none",
                credit_band_code="not_applicable",
                currency_treatment_code="unhedged",
            ),
        )
        db.session.commit()

        assert snapshot.primary_role_code == "equity"
        assert classifications_as_of(
            [instrument.id], date(2026, 8, 7)
        ) == {}
        eligible = classifications_as_of([instrument.id], AS_OF)[instrument.id]
        assert eligible.role_weights == (("equity", Decimal("1.00000000")),)
        assert eligible.source_note == "Factsheet"
        saved = db.session.get(Instrument, instrument.id)
        assert saved.fund_base_currency_code == "USD"
        assert saved.fire_bucket_code == "growth"
        assert saved.capital_certainty_code == "low"
        assert saved.currency_treatment_code == "unhedged"


def test_advanced_total_and_same_date_replacement_are_explicit(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        instrument = _priced_position(
            portfolio, account, name="Balanced Fund", amount="1000"
        )
        db.session.commit()

        with pytest.raises(ClassificationValidationError, match="current total: 90"):
            save_classification(
                portfolio.id,
                ClassificationCommand(
                    instrument_id=instrument.id,
                    effective_date=AS_OF,
                    role_weights={
                        "equity": Decimal("0.5"),
                        "liquidity": Decimal("0.4"),
                    },
                    fire_bucket_code="bridge",
                ),
            )
        assert instrument.fire_bucket_code is None

        save_classification(
            portfolio.id,
            ClassificationCommand(
                instrument_id=instrument.id,
                effective_date=AS_OF,
                role_weights={"equity": Decimal("1")},
            ),
        )
        db.session.commit()

        with pytest.raises(ClassificationValidationError, match="Confirm replacement"):
            save_classification(
                portfolio.id,
                ClassificationCommand(
                    instrument_id=instrument.id,
                    effective_date=AS_OF,
                    role_weights={"liquidity": Decimal("1")},
                ),
            )
        db.session.rollback()

        replaced = save_classification(
            portfolio.id,
            ClassificationCommand(
                instrument_id=instrument.id,
                effective_date=AS_OF,
                role_weights={
                    "liquidity": Decimal("0.4"),
                    "equity": Decimal("0.6"),
                },
                replace_existing=True,
            ),
        )
        db.session.commit()
        assert replaced.role_weights == (
            ("equity", Decimal("0.6")),
            ("liquidity", Decimal("0.4")),
        )
        assert db.session.scalar(
            select(func.count(InstrumentClassification.id))
        ) == 2


def test_allocation_uses_included_value_and_keeps_gaps_outside_denominators(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account(share="0.5", access="0.8")
        balanced = _priced_position(
            portfolio, account, name="Balanced Fund", amount="1000"
        )
        unclassified = _priced_position(
            portfolio, account, name="Unclassified Fund", amount="500"
        )
        balanced.fire_bucket_code = "bridge"
        db.session.add_all(
            [
                InstrumentClassification(
                    instrument_id=balanced.id,
                    economic_role_code="ballast",
                    weight_decimal=Decimal("0.4"),
                    effective_date=date(2026, 8, 8),
                ),
                InstrumentClassification(
                    instrument_id=balanced.id,
                    economic_role_code="growth",
                    weight_decimal=Decimal("0.6"),
                    effective_date=date(2026, 8, 8),
                ),
                CashBalanceCheckpoint(
                    account_id=account.id,
                    currency_code="EUR",
                    effective_date=date(2026, 8, 8),
                    confirmed_balance_amount=Decimal("200"),
                ),
            ]
        )
        db.session.commit()

        summary = build_portfolio_summary(portfolio, AS_OF)
        roles = {row["code"]: row for row in summary["role_allocation"]}
        buckets = {row["code"]: row for row in summary["bucket_allocation"]}

        assert summary["role_classified_denominator_amount"] == Decimal("600")
        assert summary["role_unclassified_investment_amount"] == Decimal("250")
        assert summary["role_cash_amount"] == Decimal("100")
        assert roles["equity"]["included_reporting_amount"] == Decimal("300")
        assert roles["equity"]["included_percentage"] == Decimal("0.5")
        assert roles["liquidity"]["included_reporting_amount"] == Decimal("300")
        assert summary["role_accessible_denominator_amount"] == Decimal("480")

        assert summary["bucket_classified_denominator_amount"] == Decimal("500")
        assert buckets["bridge"]["included_reporting_amount"] == Decimal("500")
        assert buckets["bridge"]["included_percentage"] == Decimal("1")
        assert summary["bucket_unclassified_investment_amount"] == Decimal("250")
        assert summary["bucket_unassigned_cash_amount"] == Decimal("100")

        warning = summary["classification_attention_items"][0]
        assert warning["instrument_id"] == unclassified.id
        assert warning["included_reporting_amount"] == Decimal("250")
        assert warning["missing_role"] is True
        assert warning["missing_bucket"] is True
        assert summary["actionable_attention_items"] == [warning]

        holdings = {row["instrument_id"]: row for row in summary["holdings"]}
        assert holdings[balanced.id]["role_weights"] == [
            {"code": "equity", "weight_decimal": Decimal("0.60000000")},
            {"code": "liquidity", "weight_decimal": Decimal("0.40000000")},
        ]
        assert holdings[balanced.id]["fire_bucket_code"] == "bridge"


def test_classification_routes_save_and_preserve_invalid_submission(
    app: Flask, client
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        instrument = _priced_position(
            portfolio, account, name="Route Fund", amount="1000"
        )
        db.session.commit()
        instrument_id = instrument.id

    response = client.post(
        f"/instruments/{instrument_id}/classification",
        data={
            "effective_date": "2026-08-09",
            "classification_mode": "advanced",
            "role_equity_percent": "70",
            "role_liquidity_percent": "20",
            "fire_bucket_code": "growth",
        },
    )
    assert response.status_code == 200
    assert b"current total: 90%" in response.data

    response = client.post(
        f"/instruments/{instrument_id}/classification",
        data={
            "effective_date": "2026-08-09",
            "classification_mode": "simple",
            "primary_role_code": "equity",
            "fire_bucket_code": "growth",
            "hedging_status": "unknown",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/instruments")

    with app.app_context():
        saved = db.session.get(Instrument, instrument_id)
        assert saved.fire_bucket_code == "growth"
        snapshot = classifications_as_of([instrument_id], AS_OF)[instrument_id]
        assert snapshot.primary_role_code == "equity"

    index = client.get("/instruments?as_of=2026-08-09")
    assert index.status_code == 200
    assert b"Route Fund" in index.data
    assert b"Equity" in index.data
