"""Frozen snapshot persistence, comparison, routes, and honesty boundaries."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app.extensions import db
from app.models import (
    Account,
    CashBalanceCheckpoint,
    Institution,
    Instrument,
    Portfolio,
    PortfolioSnapshot,
    PositionRegistration,
    Posting,
    Price,
    Transaction,
)
from app.services.snapshots import (
    SnapshotValidationError,
    compare_snapshots,
    save_snapshot,
    snapshot_payload,
)


FIRST = date(2026, 1, 1)
SECOND = date(2026, 2, 1)


def _seed(app: Flask) -> dict[str, int]:
    with app.app_context():
        portfolio = Portfolio(
            name="Snapshot portfolio",
            reporting_currency_code="USD",
            annual_spending_amount=Decimal("30000"),
            annual_spending_currency_code="USD",
            default_as_of_date=FIRST,
            price_stale_days=365,
            fx_stale_days=365,
            statement_value_stale_days=365,
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
            default_currency_code="USD",
            is_multicurrency=True,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            is_active=True,
        )
        instrument = Instrument(
            portfolio_id=portfolio.id,
            name="Fund",
            instrument_type="fund",
            valuation_currency_code="USD",
            fire_bucket_code="growth",
            is_active=True,
        )
        db.session.add_all([account, instrument])
        db.session.flush()
        registration = PositionRegistration(
            account_id=account.id,
            instrument_id=instrument.id,
            tracking_mode="transaction_tracked",
            opening_date=FIRST,
        )
        opening = Transaction(
            portfolio_id=portfolio.id,
            transaction_type="opening_balance",
            effective_date=FIRST,
            status="posted",
        )
        db.session.add_all([registration, opening])
        db.session.flush()
        db.session.add_all(
            [
                Posting(
                    transaction_id=opening.id,
                    account_id=account.id,
                    posting_kind="instrument",
                    instrument_id=instrument.id,
                    currency_code="USD",
                    quantity_delta=Decimal("10"),
                ),
                Price(
                    instrument_id=instrument.id,
                    effective_date=FIRST,
                    price_amount=Decimal("10"),
                    currency_code="USD",
                ),
                CashBalanceCheckpoint(
                    account_id=account.id,
                    currency_code="USD",
                    effective_date=FIRST,
                    confirmed_balance_amount=Decimal("100"),
                    prior_calculated_balance_amount=Decimal("0"),
                    correction_amount=Decimal("100"),
                ),
            ]
        )
        db.session.commit()
        return {
            "portfolio_id": portfolio.id,
            "account_id": account.id,
            "instrument_id": instrument.id,
        }


def _add_second_date(app: Flask, ids: dict[str, int]) -> None:
    with app.app_context():
        deposit = Transaction(
            portfolio_id=ids["portfolio_id"],
            transaction_type="deposit",
            effective_date=SECOND,
            status="posted",
        )
        db.session.add(deposit)
        db.session.flush()
        db.session.add_all(
            [
                Posting(
                    transaction_id=deposit.id,
                    account_id=ids["account_id"],
                    posting_kind="cash",
                    currency_code="USD",
                    cash_amount_delta=Decimal("50"),
                ),
                Price(
                    instrument_id=ids["instrument_id"],
                    effective_date=SECOND,
                    price_amount=Decimal("12"),
                    currency_code="USD",
                ),
                CashBalanceCheckpoint(
                    account_id=ids["account_id"],
                    currency_code="USD",
                    effective_date=SECOND,
                    confirmed_balance_amount=Decimal("180"),
                    prior_calculated_balance_amount=Decimal("150"),
                    correction_amount=Decimal("30"),
                ),
            ]
        )
        db.session.commit()


def test_snapshot_freezes_exact_shared_result_and_cannot_duplicate(app: Flask) -> None:
    ids = _seed(app)
    with app.app_context():
        portfolio = db.session.get(Portfolio, ids["portfolio_id"])
        snapshot = save_snapshot(portfolio, FIRST, note=" Month-end ")
        frozen = snapshot_payload(snapshot)
        assert frozen["portfolio"]["gross_reporting_amount"] == (
            "200.000000000000"
        )
        assert frozen["source_evidence"]["holdings"][0]["quantity"] == "10.000000"
        assert snapshot.note == "Month-end"

        price = db.session.scalar(
            db.select(Price).where(Price.instrument_id == ids["instrument_id"])
        )
        price.price_amount = Decimal("99")
        db.session.commit()
        assert snapshot_payload(snapshot) == frozen

        with pytest.raises(SnapshotValidationError, match="already exists"):
            save_snapshot(portfolio, FIRST)


def test_comparison_separates_supported_movements_from_unattributed_change(
    app: Flask,
) -> None:
    ids = _seed(app)
    with app.app_context():
        first_id = save_snapshot(
            db.session.get(Portfolio, ids["portfolio_id"]), FIRST
        ).id
    _add_second_date(app, ids)
    with app.app_context():
        first = db.session.get(PortfolioSnapshot, first_id)
        second = save_snapshot(db.session.get(Portfolio, ids["portfolio_id"]), SECOND)
        comparison = compare_snapshots(first, second)

        assert comparison["status"] == "current"
        assert comparison["gross_value_change"] == Decimal("100")
        assert comparison["external_cash_flow"] == Decimal("50")
        assert comparison["cash_reconciliation"] == Decimal("30")
        assert comparison["unattributed_value_change"] == Decimal("20")


def test_missing_movement_fx_withholds_unattributed_change(app: Flask) -> None:
    ids = _seed(app)
    with app.app_context():
        portfolio = db.session.get(Portfolio, ids["portfolio_id"])
        first = save_snapshot(portfolio, FIRST)
        deposit = Transaction(
            portfolio_id=portfolio.id,
            transaction_type="deposit",
            effective_date=SECOND,
            status="posted",
        )
        db.session.add(deposit)
        db.session.flush()
        db.session.add(
            Posting(
                transaction_id=deposit.id,
                account_id=ids["account_id"],
                posting_kind="cash",
                currency_code="EUR",
                cash_amount_delta=Decimal("10"),
            )
        )
        db.session.commit()
        second = save_snapshot(portfolio, SECOND)
        comparison = compare_snapshots(first, second)

        assert snapshot_payload(second)["movement_totals"]["external_cash_flow"][
            "status"
        ] == "missing"
        assert comparison["status"] == "partial"
        assert comparison["unattributed_value_change"] is None


def test_snapshot_browser_flow_requires_confirmation_and_shows_comparison(
    app: Flask, client: FlaskClient
) -> None:
    ids = _seed(app)
    preview = client.get("/snapshots/new")
    assert preview.status_code == 200
    assert "Gross tracked value" in preview.get_data(as_text=True)

    refused = client.post(
        "/snapshots/new", data={"as_of_date": FIRST.isoformat(), "note": "First"}
    )
    assert refused.status_code == 400
    assert "Confirm before saving" in refused.get_data(as_text=True)

    saved = client.post(
        "/snapshots/new",
        data={
            "as_of_date": FIRST.isoformat(),
            "note": "First",
            "confirm_snapshot": "y",
        },
        follow_redirects=True,
    )
    assert saved.status_code == 200
    assert "This is the first snapshot" in saved.get_data(as_text=True)

    _add_second_date(app, ids)
    second = client.post(
        "/snapshots/new",
        data={
            "as_of_date": SECOND.isoformat(),
            "confirm_snapshot": "y",
        },
        follow_redirects=True,
    )
    body = second.get_data(as_text=True)
    assert second.status_code == 200
    assert "Unattributed value change" in body
    assert '<span class="ccy">USD</span> +20.00' in body
    assert "not a return calculation" in body
    with app.app_context():
        assert db.session.query(PortfolioSnapshot).count() == 2
