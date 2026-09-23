"""Deterministic retirement assumptions and annual projection."""

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
    FixedDeposit,
    Institution,
    Instrument,
    InstrumentClassification,
    Portfolio,
    PositionRegistration,
    RetirementAssumption,
    ValuationObservation,
)
from app.services.portfolio_summary import build_portfolio_summary
from app.services.retirement import (
    RetirementValidationError,
    build_retirement_projection,
    save_retirement_assumption,
)


AS_OF = date(2026, 8, 30)
ZERO_RETURNS = {
    "equity": Decimal("0"),
    "income": Decimal("0"),
    "liquidity": Decimal("0"),
    "alternatives": Decimal("0"),
}


def _portfolio_account(
    *,
    spending: str = "100",
    inflation: str | None = "0",
    access: str = "1",
) -> tuple[Portfolio, Account]:
    portfolio = Portfolio(
        name="Retirement portfolio",
        reporting_currency_code="USD",
        annual_spending_amount=Decimal(spending),
        annual_spending_currency_code="USD",
        annual_inflation_decimal=(
            Decimal(inflation) if inflation is not None else None
        ),
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
        name="Account",
        account_type="brokerage",
        default_currency_code="USD",
        is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal(access),
        relationship_eligible=True,
        is_active=True,
    )
    db.session.add(account)
    db.session.flush()
    return portfolio, account


def _cash(account: Account, amount: str) -> None:
    db.session.add(
        CashBalanceCheckpoint(
            account_id=account.id,
            currency_code="USD",
            effective_date=AS_OF,
            confirmed_balance_amount=Decimal(amount),
        )
    )


def _position(
    portfolio: Portfolio,
    account: Account,
    *,
    name: str,
    amount: str,
    bucket: str | None,
    roles: dict[str, str],
    maturity: date | None = None,
) -> None:
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name=name,
        instrument_type="fixed_deposit" if maturity else "fund",
        valuation_currency_code="USD",
        fire_bucket_code=bucket,
        is_active=True,
    )
    db.session.add(instrument)
    db.session.flush()
    registration = PositionRegistration(
        account_id=account.id,
        instrument_id=instrument.id,
        tracking_mode="statement_valued",
        opening_date=AS_OF,
    )
    db.session.add(registration)
    db.session.flush()
    db.session.add(
        ValuationObservation(
            position_registration_id=registration.id,
            effective_date=AS_OF,
            native_value_amount=Decimal(amount),
            currency_code="USD",
        )
    )
    for role, weight in roles.items():
        db.session.add(
            InstrumentClassification(
                instrument_id=instrument.id,
                economic_role_code=role,
                weight_decimal=Decimal(weight),
                effective_date=AS_OF,
            )
        )
    if maturity:
        db.session.add(
            FixedDeposit(
                position_registration_id=registration.id,
                currency_code="USD",
                start_date=AS_OF,
                maturity_date=maturity,
                maturity_action="undecided",
            )
        )


def _assumptions(
    portfolio: Portfolio,
    *,
    current: int = 50,
    withdrawal: int = 52,
    final: int = 54,
    returns: dict[str, Decimal] = ZERO_RETURNS,
    legacy: Decimal | None = None,
) -> RetirementAssumption:
    return save_retirement_assumption(
        portfolio.id,
        current_age_years=current,
        withdrawal_start_age_years=withdrawal,
        final_age_years=final,
        role_returns=returns,
        terminal_legacy_target_amount=legacy,
    )


def _project(portfolio: Portfolio) -> dict[str, object]:
    summary = build_portfolio_summary(portfolio, AS_OF)
    return build_retirement_projection(
        portfolio, summary, AS_OF, reporting_currency="USD"
    )


def test_assumptions_require_ordered_ages_all_roles_and_bounded_returns(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, _ = _portfolio_account()
        with pytest.raises(RetirementValidationError, match="greater than current"):
            _assumptions(portfolio, current=50, withdrawal=50)
        with pytest.raises(RetirementValidationError, match="all four"):
            save_retirement_assumption(
                portfolio.id,
                current_age_years=50,
                withdrawal_start_age_years=55,
                final_age_years=90,
                role_returns={"equity": Decimal("0.05")},
            )
        invalid = dict(ZERO_RETURNS)
        invalid["equity"] = Decimal("1.01")
        with pytest.raises(RetirementValidationError, match="-100% and 100%"):
            _assumptions(portfolio, returns=invalid)

        row = _assumptions(portfolio, legacy=Decimal("500000"))
        db.session.commit()
        assert row.terminal_legacy_target_amount == Decimal("500000")
        assert row.terminal_legacy_target_currency_code == "USD"
        replacement = _assumptions(portfolio, final=95)
        db.session.commit()
        assert replacement.id == row.id
        assert db.session.query(RetirementAssumption).count() == 1


def test_accumulation_then_beginning_of_year_withdrawals_and_returns(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _cash(account, "1000")
        returns = dict(ZERO_RETURNS)
        returns["liquidity"] = Decimal("0.10")
        _assumptions(portfolio, returns=returns)
        db.session.commit()

        projection = _project(portfolio)
        age_50, age_51, age_52, age_53 = projection["years"]

        assert projection["status"] == "current"
        assert age_50["spending_required_amount"] == 0
        assert age_50["ending_value_amount"] == Decimal("1100")
        assert age_51["ending_value_amount"] == Decimal("1210")
        assert age_52["starting_value_amount"] == Decimal("1210")
        assert age_52["withdrawn_amount"] == Decimal("100")
        assert age_52["return_amount"] == Decimal("111")
        assert age_52["annual_surplus_drawdown_amount"] == Decimal("11")
        assert age_52["ending_value_amount"] == Decimal("1221")
        assert age_53["ending_value_amount"] == Decimal("1233.1")
        assert projection["initial_withdrawal_rate"] == Decimal("100") / Decimal(
            "1210"
        )


def test_spending_inflates_through_accumulation_years(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account(inflation="0.03")
        _cash(account, "1000")
        _assumptions(portfolio, current=50, withdrawal=52, final=53)
        db.session.commit()

        first_withdrawal = _project(portfolio)["years"][2]
        assert first_withdrawal["age"] == 52
        assert first_withdrawal["spending_required_amount"] == (
            Decimal("100") * (Decimal("1.03") ** 2)
        )


def test_negative_cash_is_an_opening_obligation_not_future_return(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _cash(account, "-50")
        _position(
            portfolio,
            account,
            name="Growth",
            amount="100",
            bucket="growth",
            roles={"equity": "1"},
        )
        _assumptions(portfolio, current=50, withdrawal=51, final=52)
        db.session.commit()

        projection = _project(portfolio)
        accumulation = projection["years"][0]
        withdrawal = projection["years"][1]
        assert projection["opening_negative_cash_amount"] == Decimal("50")
        assert accumulation["starting_value_amount"] == Decimal("50")
        assert accumulation["opening_obligation_applied_amount"] == Decimal("50")
        assert accumulation["ending_value_amount"] == Decimal("50")
        assert withdrawal["withdrawn_amount"] == Decimal("50")
        assert withdrawal["shortfall_amount"] == Decimal("50")


def test_liquidation_is_cash_then_bucket_then_non_equity(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _cash(account, "20")
        _position(
            portfolio,
            account,
            name="Now income",
            amount="30",
            bucket="now",
            roles={"income": "1"},
        )
        _position(
            portfolio,
            account,
            name="Now equity",
            amount="30",
            bucket="now",
            roles={"equity": "1"},
        )
        _position(
            portfolio,
            account,
            name="Bridge income",
            amount="30",
            bucket="bridge",
            roles={"income": "1"},
        )
        _assumptions(portfolio, current=50, withdrawal=51, final=52)
        db.session.commit()

        year = _project(portfolio)["years"][1]
        assert [row["source_name"] for row in year["applications"]] == [
            "Account · USD",
            "Now income",
            "Now equity",
            "Bridge income",
        ]
        assert [row["amount"] for row in year["applications"]] == [
            Decimal("20"),
            Decimal("30"),
            Decimal("30"),
            Decimal("20"),
        ]
        assert year["buckets_used"] == ("cash", "now", "bridge")
        assert year["equity_reliance_amount"] == Decimal("30")


def test_mixed_instrument_liquidation_is_proportional_without_rebalancing(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account(spending="50")
        _cash(account, "0")
        _position(
            portfolio,
            account,
            name="Balanced",
            amount="100",
            bucket="now",
            roles={"equity": "0.5", "income": "0.5"},
        )
        returns = dict(ZERO_RETURNS)
        returns.update({"equity": Decimal("0.10"), "income": Decimal("0.02")})
        _assumptions(
            portfolio,
            current=50,
            withdrawal=51,
            final=52,
            returns=returns,
        )
        db.session.commit()

        withdrawal_year = _project(portfolio)["years"][1]
        application = withdrawal_year["applications"][0]
        equity_removed = Decimal("50") * Decimal("55") / Decimal("106")
        income_removed = Decimal("50") - equity_removed
        assert application["role_amounts"] == {
            "equity": equity_removed,
            "income": income_removed,
        }
        assert withdrawal_year["ending_role_values"]["equity"] == (
            (Decimal("55") - equity_removed) * Decimal("1.10")
        )
        assert withdrawal_year["ending_role_values"]["income"] == (
            (Decimal("51") - income_removed) * Decimal("1.02")
        )


def test_projects_are_excluded_while_undated_restricted_value_remains_capital(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        account.present_access_decimal = Decimal("0")
        _cash(account, "0")
        _position(
            portfolio,
            account,
            name="House project",
            amount="1000",
            bucket="projects",
            roles={"alternatives": "1"},
        )
        restricted = Account(
            portfolio_id=portfolio.id,
            institution_id=account.institution_id,
            name="Restricted",
            account_type="retirement",
            default_currency_code="USD",
            is_multicurrency=False,
            cash_tracking_mode="included_in_aggregate",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("0"),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add(restricted)
        db.session.flush()
        _position(
            portfolio,
            restricted,
            name="Restricted growth",
            amount="500",
            bucket="growth",
            roles={"equity": "1"},
        )
        returns = dict(ZERO_RETURNS)
        returns["alternatives"] = Decimal("0.50")
        _assumptions(
            portfolio,
            current=50,
            withdrawal=51,
            final=52,
            returns=returns,
        )
        db.session.commit()

        projection = _project(portfolio)
        assert projection["calculation_complete"] is True
        assert projection["projects_included_amount"] == Decimal("1000")
        assert projection["restricted_amount"] == Decimal("1500")
        assert projection["non_project_restricted_amount"] == Decimal("500")
        assert projection["years"][0]["starting_value_amount"] == Decimal("500")
        assert projection["years"][0]["return_amount"] == 0
        assert projection["years"][1]["shortfall_amount"] == Decimal("100")
        assert projection["years"][1]["ending_value_amount"] == Decimal("500")


def test_future_access_date_controls_spending_not_owned_capital(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account(spending="80", access="0")
        account.earliest_access_date = date(2028, 2, 1)
        _cash(account, "0")
        _position(
            portfolio,
            account,
            name="Future-access income",
            amount="100",
            bucket="bridge",
            roles={"income": "1"},
        )
        returns = dict(ZERO_RETURNS)
        returns["income"] = Decimal("0.10")
        _assumptions(
            portfolio,
            current=50,
            withdrawal=51,
            final=53,
            returns=returns,
        )
        db.session.commit()

        projection = _project(portfolio)
        age_50, age_51, age_52 = projection["years"]
        assert projection["future_access_amount"] == Decimal("100")
        assert projection["future_access_sources"][0]["access_age"] == 52
        assert age_50["starting_value_amount"] == Decimal("100")
        assert age_50["ending_value_amount"] == Decimal("110")
        assert age_51["withdrawn_amount"] == 0
        assert age_51["shortfall_amount"] == Decimal("80")
        assert age_51["ending_value_amount"] == Decimal("121")
        assert age_52["withdrawn_amount"] == Decimal("80")
        assert age_52["ending_value_amount"] == Decimal("45.10")


def test_fixed_deposit_waits_for_first_annual_boundary_after_maturity(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account(spending="80")
        _cash(account, "0")
        _position(
            portfolio,
            account,
            name="Later FD",
            amount="100",
            bucket="now",
            roles={"liquidity": "1"},
            maturity=date(2028, 2, 1),
        )
        _assumptions(portfolio, current=50, withdrawal=51, final=53)
        db.session.commit()

        projection = _project(portfolio)
        age_51 = projection["years"][1]
        age_52 = projection["years"][2]
        assert age_51["withdrawn_amount"] == 0
        assert age_51["shortfall_amount"] == Decimal("80")
        assert age_52["withdrawn_amount"] == Decimal("80")
        assert age_52["ending_value_amount"] == Decimal("20")
        assert projection["depletion_age"] is None


def test_depletion_stays_zero_and_legacy_gap_is_exact(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _cash(account, "150")
        _assumptions(
            portfolio,
            current=50,
            withdrawal=51,
            final=54,
            legacy=Decimal("25"),
        )
        db.session.commit()

        projection = _project(portfolio)
        assert [row["ending_value_amount"] for row in projection["years"]] == [
            Decimal("150"),
            Decimal("50"),
            Decimal("0"),
            Decimal("0"),
        ]
        assert projection["depletion_age"] == 52
        assert projection["first_shortfall_age"] == 52
        assert projection["first_drawdown_age"] == 51
        assert projection["first_drawdown_calendar_year"] == AS_OF.year + 1
        assert projection["final_value_amount"] == 0
        assert projection["legacy_target_state"] == "shortfall"
        assert projection["legacy_target_gap_amount"] == Decimal("-25")


def test_incomplete_sources_withhold_all_annual_results(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _cash(account, "0")
        _position(
            portfolio,
            account,
            name="Unclassified",
            amount="1000",
            bucket="growth",
            roles={},
        )
        _assumptions(portfolio)
        db.session.commit()

        projection = _project(portfolio)
        assert projection["status"] == "partial"
        assert projection["calculation_complete"] is False
        assert projection["years"] == ()
        assert any(row["kind"] == "incomplete_sources" for row in projection["reasons"])


def test_retired_projection_form_preserves_stored_assumptions(app, client):
    with app.app_context():
        portfolio, account = _portfolio_account(spending="30000", inflation="0.03")
        _cash(account, "1000000")
        _assumptions(portfolio, current=50, withdrawal=55, final=60)
        db.session.commit()
        before = db.session.query(RetirementAssumption).one().current_age_years
    page = client.get("/planning/retirement?as_of=2026-08-30")
    assert page.location == "/retirement?as_of=2026-08-30"
    response = client.post("/planning/retirement", data={"current_age_years": "70"}, follow_redirects=True)
    assert b"No changes were saved" in response.data
    with app.app_context():
        assert db.session.query(RetirementAssumption).one().current_age_years == before
