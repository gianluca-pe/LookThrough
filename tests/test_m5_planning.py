"""Planning vocabulary, targets, and shared-summary contract."""

from datetime import date
from decimal import Decimal

import pytest
from flask import Flask

from app.extensions import db
from app.models import (
    Account,
    AllocationTarget,
    CashBalanceCheckpoint,
    Institution,
    Instrument,
    InstrumentClassification,
    Portfolio,
    PositionRegistration,
    ValuationObservation,
)
from app.services.classification import classifications_as_of, current_fire_bucket_code
from app.services.planning import PlanningValidationError, save_allocation_targets
from app.services.portfolio_summary import build_portfolio_summary


AS_OF = date(2026, 8, 29)


def _portfolio_account() -> tuple[Portfolio, Account]:
    portfolio = Portfolio(
        name="Planning portfolio",
        reporting_currency_code="EUR",
        annual_spending_amount=Decimal("40000"),
        annual_spending_currency_code="EUR",
    )
    db.session.add(portfolio)
    db.session.flush()
    institution = Institution(portfolio_id=portfolio.id, name="Bank")
    db.session.add(institution)
    db.session.flush()
    account = Account(
        portfolio_id=portfolio.id,
        institution_id=institution.id,
        name="Account",
        account_type="brokerage",
        default_currency_code="EUR",
        is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        is_active=True,
    )
    db.session.add(account)
    db.session.flush()
    return portfolio, account


def _statement_position(
    portfolio: Portfolio, account: Account, *, amount: str = "600"
) -> Instrument:
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name="Fund",
        instrument_type="fund",
        valuation_currency_code="EUR",
        fire_bucket_code="growth",
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
            effective_date=AS_OF,
            native_value_amount=Decimal(amount),
            currency_code="EUR",
        )
    )
    return instrument


def _targets() -> dict[str, tuple[Decimal, Decimal]]:
    return {
        "equity": (Decimal("0.70"), Decimal("0.80")),
        "income": (Decimal("0"), Decimal("0.10")),
        "liquidity": (Decimal("0.10"), Decimal("0.20")),
        "alternatives": (Decimal("0"), Decimal("0.10")),
    }


def test_legacy_roles_and_buckets_use_owner_approved_consolidation(app: Flask) -> None:
    expected_roles = {
        "growth": "equity",
        "opportunistic": "equity",
        "income_credit": "income",
        "inflation_defence": "income",
        "ballast": "liquidity",
        "liquidity": "liquidity",
        "diversifiers": "alternatives",
    }
    with app.app_context():
        portfolio, account = _portfolio_account()
        instruments = []
        for index, legacy_role in enumerate(expected_roles, start=1):
            instrument = Instrument(
                portfolio_id=portfolio.id,
                name=f"Legacy {index}",
                instrument_type="fund",
                valuation_currency_code="EUR",
                is_active=True,
            )
            db.session.add(instrument)
            db.session.flush()
            db.session.add(
                InstrumentClassification(
                    instrument_id=instrument.id,
                    economic_role_code=legacy_role,
                    weight_decimal=Decimal("1"),
                    effective_date=date(2026, 8, 1),
                )
            )
            instruments.append((instrument, legacy_role))
        db.session.commit()

        snapshots = classifications_as_of(
            [instrument.id for instrument, _ in instruments], AS_OF
        )
        for instrument, legacy_role in instruments:
            assert snapshots[instrument.id].primary_role_code == expected_roles[legacy_role]

    assert current_fire_bucket_code("now") == "now"
    assert current_fire_bucket_code("next") == "bridge"
    assert current_fire_bucket_code("bridge") == "bridge"
    assert current_fire_bucket_code("growth") == "growth"
    assert current_fire_bucket_code("flex") == "projects"


def test_target_ranges_are_complete_feasible_and_replaced_atomically(app: Flask) -> None:
    with app.app_context():
        portfolio, _ = _portfolio_account()
        with pytest.raises(PlanningValidationError, match="all four"):
            save_allocation_targets(portfolio.id, {"equity": (0, 1)})
        impossible = {
            "equity": (Decimal("0.7"), Decimal("0.8")),
            "income": (Decimal("0.4"), Decimal("0.5")),
            "liquidity": (Decimal("0"), Decimal("0.1")),
            "alternatives": (Decimal("0"), Decimal("0.1")),
        }
        with pytest.raises(PlanningValidationError, match="complete 100%"):
            save_allocation_targets(portfolio.id, impossible)

        saved = save_allocation_targets(portfolio.id, _targets())
        db.session.commit()
        assert len(saved) == 4
        assert db.session.query(AllocationTarget).count() == 4

        replacement = dict(_targets())
        replacement["equity"] = (Decimal("0.6"), Decimal("0.7"))
        save_allocation_targets(portfolio.id, replacement)
        db.session.commit()
        assert db.session.query(AllocationTarget).count() == 4
        equity = db.session.query(AllocationTarget).filter_by(
            asset_role_code="equity"
        ).one()
        assert equity.minimum_decimal == Decimal("0.60000000")


def test_shared_summary_reports_exact_gap_and_cash_as_liquidity(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        instrument = _statement_position(portfolio, account)
        db.session.add(
            InstrumentClassification(
                instrument_id=instrument.id,
                economic_role_code="equity",
                weight_decimal=Decimal("1"),
                effective_date=AS_OF,
            )
        )
        db.session.add(
            CashBalanceCheckpoint(
                account_id=account.id,
                currency_code="EUR",
                effective_date=AS_OF,
                confirmed_balance_amount=Decimal("400"),
            )
        )
        save_allocation_targets(portfolio.id, _targets())
        db.session.commit()

        summary = build_portfolio_summary(portfolio, AS_OF)
        roles = {row["code"]: row for row in summary["role_allocation"]}
        assert summary["included_reporting_amount"] == Decimal("1000")
        assert roles["equity"]["included_percentage"] == Decimal("0.6")
        assert roles["equity"]["range_state"] == "below"
        assert roles["equity"]["gap_to_range_percentage"] == Decimal("0.10")
        assert roles["equity"]["gap_to_range_amount"] == Decimal("100.00000000")
        assert roles["liquidity"]["included_reporting_amount"] == Decimal("400")
        assert roles["liquidity"]["range_state"] == "above"
        assert summary["allocation_targets_complete"] is True
        assert summary["allocation_target_calculation_complete"] is True
        assert summary["planning_now_years"] == Decimal("3")
        assert summary["planning_bridge_years"] == Decimal("7")
        assert summary["projects_in_retirement_runway"] is False


def test_target_gap_is_not_invented_while_classification_is_incomplete(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _statement_position(portfolio, account)
        save_allocation_targets(portfolio.id, _targets())
        db.session.commit()
        summary = build_portfolio_summary(portfolio, AS_OF)
        assert summary["allocation_target_calculation_complete"] is False
        assert all(row["range_state"] is None for row in summary["role_allocation"])
