"""Finite funding requirements and cash-first waterfall."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from flask import Flask, template_rendered
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
    ValuationObservation,
)
from app.services.funding import build_funding_summary
from app.services.portfolio_summary import build_portfolio_summary


AS_OF = date(2026, 8, 29)


def _portfolio_account(
    *,
    spending: str = "30000",
    inflation: str | None = "0.03",
) -> tuple[Portfolio, Account]:
    portfolio = Portfolio(
        name="Funding portfolio",
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
        present_access_decimal=Decimal("1"),
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
    bucket: str,
    roles: dict[str, str],
    maturity_date: date | None = None,
) -> None:
    is_fd = maturity_date is not None
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name=name,
        instrument_type="fixed_deposit" if is_fd else "fund",
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
        opening_date=date(2026, 8, 1),
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
    if maturity_date is not None:
        db.session.add(
            FixedDeposit(
                position_registration_id=registration.id,
                currency_code="USD",
                start_date=date(2026, 8, 1),
                maturity_date=maturity_date,
                annual_rate_decimal=None,
                expected_maturity_proceeds_amount=None,
                maturity_action="undecided",
            )
        )


def _funding(portfolio: Portfolio) -> dict[str, object]:
    summary = build_portfolio_summary(portfolio, AS_OF)
    return build_funding_summary(
        portfolio, summary, AS_OF, reporting_currency="USD"
    )


def test_requirements_hold_real_spending_constant_and_waterfall_cash_first(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account()
        _cash(account, "50000")
        _position(
            portfolio,
            account,
            name="Now income",
            amount="50000",
            bucket="now",
            roles={"income": "1"},
        )
        _position(
            portfolio,
            account,
            name="Bridge equity",
            amount="100000",
            bucket="bridge",
            roles={"equity": "1"},
        )
        _position(
            portfolio,
            account,
            name="Growth equity",
            amount="500000",
            bucket="growth",
            roles={"equity": "1"},
        )
        _position(
            portfolio,
            account,
            name="House project",
            amount="20000",
            bucket="projects",
            roles={"alternatives": "1"},
        )
        db.session.commit()

        funding = _funding(portfolio)
        now, bridge = funding["periods"]
        expected_now = Decimal("90000")
        expected_bridge = Decimal("210000")

        assert funding["status"] == "current"
        assert funding['money_basis'] == 'as_of_purchasing_power'
        assert now['annual_amounts'] == (Decimal('30000'),) * 3
        assert bridge['annual_amounts'] == (Decimal('30000'),) * 7
        assert portfolio.annual_inflation_decimal == Decimal('.03')
        assert now["spending_requirement_amount"] == expected_now
        assert now["cash_applied_amount"] == Decimal("50000")
        assert now["funded_amount"] == expected_now
        assert now["later_period_dependency_amount"] == 0
        assert now["equity_dependency_amount"] == 0
        assert bridge["spending_requirement_amount"] == expected_bridge
        assert bridge["is_funded"] is True
        assert bridge["later_period_dependency_amount"] > 0
        assert bridge["equity_dependency_amount"] > 0
        assert funding["projects_included_amount"] == Decimal("20000")
        assert funding["growth_remainder_amount"] == Decimal('400000')
        assert funding['assessment']['growth_remaining_amount'] == Decimal('400000')
        current_now, current_bridge = funding['assessment']['bucket_targets']
        assert current_now['current_amount'] == Decimal('100000')
        assert current_now['surplus_amount'] == Decimal('10000')
        assert current_bridge['current_amount'] == Decimal('100000')
        assert current_bridge['gap_amount'] == Decimal('110000')
        # The chart tests current placement; the waterfall also uses Now's carry.
        assert bridge['eligible_reserve_amount'] == Decimal('110000')
        assert current_now['cash_assigned_amount'] + current_bridge['cash_assigned_amount'] == Decimal('50000')
        assert funding['assessment']['allocation_capital_amount'] == Decimal('700000')
        assert current_now['current_percent'] == Decimal('100000') * 100 / Decimal('700000')
        assert current_bridge['required_percent'] == Decimal('30')


def test_mixed_fund_is_wholly_equity_exposed_not_fractionally_split(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account(spending="1", inflation="0")
        _cash(account, "0")
        _position(
            portfolio,
            account,
            name="Balanced fund",
            amount="100",
            bucket="now",
            roles={"equity": "0.5", "income": "0.5"},
        )
        db.session.commit()

        funding = _funding(portfolio)
        now, bridge = funding["periods"]

        assert now["equity_dependency_amount"] == Decimal("3")
        assert bridge["equity_dependency_amount"] == Decimal("7")
        assert funding["growth_remainder_amount"] == Decimal("90")


def test_fixed_deposit_cannot_fund_before_its_maturity_period(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account(spending="100", inflation="0")
        _cash(account, "0")
        _position(
            portfolio,
            account,
            name="Later deposit",
            amount="1000",
            bucket="now",
            roles={"liquidity": "1"},
            maturity_date=date(2030, 8, 29),
        )
        db.session.commit()

        funding = _funding(portfolio)
        now, bridge = funding["periods"]

        assert now["shortfall_amount"] == Decimal("300")
        assert bridge["funded_amount"] == Decimal("700")
        assert funding["growth_remainder_amount"] == Decimal("300")


def test_negative_cash_is_an_explicit_now_deficit(app: Flask) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account(spending="100", inflation="0")
        _cash(account, "-50")
        _position(
            portfolio,
            account,
            name="Now fund",
            amount="1000",
            bucket="now",
            roles={"income": "1"},
        )
        db.session.commit()

        funding = _funding(portfolio)
        now, bridge = funding["periods"]

        assert funding["cash_deficit_amount"] == Decimal("50")
        assert now["spending_requirement_amount"] == Decimal("300")
        assert now["required_amount"] == Decimal("350")
        assert bridge["shortfall_amount"] == Decimal("50")


def test_real_reserve_check_does_not_require_nominal_inflation(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account(inflation=None)
        _cash(account, "100000")
        db.session.commit()

        funding = _funding(portfolio)

        assert funding["status"] == "current"
        assert funding["periods"][0]['required_amount'] == Decimal('90000')
        assert funding["periods"][1]['required_amount'] == Decimal('210000')
        assert funding["assessment"]['shortfall_amount'] == Decimal('200000')
        assert funding['attention_items'] == ()
        assert portfolio.annual_inflation_decimal is None


def test_unknown_role_dependency_total_is_the_exact_period_sum(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account(spending="100", inflation="0")
        _cash(account, "0")
        _position(
            portfolio, account, name="Now unroled",
            amount="400", bucket="now", roles={},
        )
        _position(
            portfolio, account, name="Bridge unroled",
            amount="800", bucket="bridge", roles={},
        )
        db.session.commit()

        funding = _funding(portfolio)
        now, bridge = funding["periods"]

        assert funding["status"] == "partial"
        assert now["unknown_role_dependency_amount"] == Decimal("300")
        assert bridge["unknown_role_dependency_amount"] == Decimal("700")
        assert funding["unknown_role_dependency_total_amount"] == Decimal("1000")
        assert funding["unknown_role_dependency_total_amount"] == sum(
            (period["unknown_role_dependency_amount"] for period in funding["periods"]),
            Decimal("0"),
        )


def test_expected_cash_account_without_source_makes_known_result_partial(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account(spending="100", inflation="0")
        _position(
            portfolio,
            account,
            name="Growth fund",
            amount="2000",
            bucket="growth",
            roles={"equity": "1"},
        )
        db.session.commit()

        funding = _funding(portfolio)

        assert funding["status"] == "partial"
        assert funding["cash"]["status"] == "missing"
        assert funding["periods"][0]["is_funded"] is True


def test_retired_funding_form_preserves_stored_inflation(app, client):
    with app.app_context():
        portfolio, _ = _portfolio_account(inflation="0.03")
        portfolio_id = portfolio.id
        db.session.commit()
    assert client.get("/planning/funding").location == "/retirement"
    response = client.post("/planning/funding", data={"annual_inflation_percent": "5"})
    assert response.status_code == 303
    assert response.location == "/retirement"
    with app.app_context():
        assert db.session.get(Portfolio, portfolio_id).annual_inflation_decimal == Decimal("0.03")


def test_overview_exposes_shared_funding_contract(app: Flask, client: FlaskClient) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account(spending="100", inflation="0.03")
        _cash(account, "1000")
        _position(
            portfolio,
            account,
            name="Growth fund",
            amount="10000",
            bucket="growth",
            roles={"equity": "1"},
        )
        db.session.commit()

    rendered = []
    with template_rendered.connected_to(
        lambda sender, template, context, **extra: rendered.append(context), app
    ):
        response = client.get("/overview")

    assert response.status_code == 200
    assert rendered[0]["funding"]["annual_inflation_decimal"] == Decimal("0.03")
    assert len(rendered[0]["funding"]["periods"]) == 2


@pytest.mark.parametrize(
    "cash,now_value,bridge_value,growth_value,now_eligible,bridge_eligible,growth_used,gap,remainder",
    [
        ("0", "500", "800", "9000", "500", "1000", "0", "0", "300"),
        ("0", "200", "100", "2000", "200", "0", "700", "0", "0"),
        ("0", "200", "100", "50", "200", "0", "50", "650", "0"),
        ("1500", "500", "800", "9000", "800", "2000", "0", "0", "1800"),
        ("400", "0", "600", "9000", "300", "700", "0", "0", "0"),
        ("-50", "400", "600", "9000", "400", "650", "50", "0", "0"),
        ("0", "300", "700", "0", "300", "700", "0", "0", "0"),
    ],
)
def test_reserve_assessment_and_conservation(
    app, cash, now_value, bridge_value, growth_value, now_eligible,
    bridge_eligible, growth_used, gap, remainder,
):
    with app.app_context():
        portfolio, account = _portfolio_account(spending="100", inflation="0")
        _cash(account, cash)
        for bucket, value in (("now", now_value), ("bridge", bridge_value), ("growth", growth_value)):
            _position(portfolio, account, name=bucket, amount=value, bucket=bucket, roles={"income": "1"})
        db.session.commit()
        result = _funding(portfolio)
        now, bridge = result["periods"]
        assessment = result["assessment"]
        assert now["eligible_reserve_amount"] == Decimal(now_eligible)
        assert bridge["eligible_reserve_amount"] == Decimal(bridge_eligible)
        for period in (now, bridge):
            assert period["eligible_reserve_amount"] == (
                period["own_bucket_reserve_amount"] + period["cash_assigned_amount"] + period["earlier_reserve_amount"]
            )
            assert period["reserve_surplus_amount"] - period["reserve_gap_amount"] == (
                period["eligible_reserve_amount"] - period["required_amount"]
            )
            assert period["cash_assigned_amount"] == period["cash_applied_amount"]
        assert assessment["growth_applied_amount"] == Decimal(growth_used)
        assert assessment["shortfall_amount"] == Decimal(gap)
        assert assessment["covered_without_growth"] == (Decimal(growth_used) == 0 and Decimal(gap) == 0)
        unused = sum((source["amount"] for source in assessment["remainder_sources"]), Decimal("0"))
        assert unused == Decimal(remainder)
        assert assessment["earlier_reserve_amount"] == assessment["earlier_reserve_applied_amount"] + unused
        assert assessment["required_amount"] == (
            assessment["earlier_reserve_applied_amount"] + assessment["growth_applied_amount"] + assessment["shortfall_amount"]
        )
        assert assessment["growth_remaining_amount"] == Decimal(growth_value) - Decimal(growth_used)
        assert result["growth_remainder_amount"] == unused + assessment["growth_remaining_amount"]


def test_late_reserve_surplus_does_not_erase_now_gap(app, client):
    with app.app_context():
        portfolio, account = _portfolio_account(spending="100", inflation="0")
        _cash(account, "0")
        for name, amount, maturity in (
            ("Bridge maturity", "1000", date(2030, 8, 29)),
            ("Beyond ten years", "5000", date(2037, 8, 29)),
        ):
            _position(portfolio, account, name=name, amount=amount, bucket="now",
                      roles={"liquidity": "1"}, maturity_date=maturity)
        db.session.commit()
        result = _funding(portfolio)
        now, bridge = result["periods"]
        assert now["reserve_gap_amount"] == Decimal("300")
        assert bridge["reserve_surplus_amount"] == Decimal("300")
        assert bridge["earlier_reserve_amount"] == Decimal("1000")
        assessment = result["assessment"]
        assert assessment["earlier_reserve_amount"] == Decimal("1000")
        assert assessment["shortfall_amount"] == Decimal("300")
        assert assessment["covered_without_growth"] is False
        assert assessment["late_earlier_reserve_amount"] == Decimal("5000")
        assert assessment["growth_remaining_amount"] == 0
    body = client.get("/overview").get_data(as_text=True)
    assert "do not cover both periods on their own" in body
    assert "A later surplus cannot fill an earlier period" in body
    assert "maturing after year 10" in body


def test_reserves_apply_share_access_and_exclude_projects(app):
    with app.app_context():
        portfolio, account = _portfolio_account(spending="100", inflation="0")
        account.portfolio_share_decimal = Decimal("0.5")
        account.present_access_decimal = Decimal("0.5")
        _cash(account, "400")
        _position(portfolio, account, name="Now", amount="800", bucket="now", roles={"income": "1"})
        _position(portfolio, account, name="Project", amount="9000", bucket="projects", roles={"income": "1"})
        db.session.commit()
        result = _funding(portfolio)
        assert result["periods"][0]["eligible_reserve_amount"] == Decimal("300")
        assert result["assessment"]["earlier_reserve_amount"] == Decimal("300")
        assert result["assessment"]["shortfall_amount"] == Decimal("700")
        assert result["projects_included_amount"] == Decimal("4500")
        assert result['assessment']['allocation_capital_amount'] == Decimal('300')
        assert result['assessment']['bucket_targets'][0]['current_percent'] == Decimal('100')


@pytest.mark.parametrize('capital,confirmed', [('0', True), ('100', False), ('100', True)])
def test_bucket_percentage_basis_handles_zero_partial_and_targets_over_100(app, client, capital, confirmed):
    with app.app_context():
        portfolio, account = _portfolio_account(spending='100', inflation='0')
        if confirmed:
            _cash(account, '0')
        _position(portfolio, account, name='Growth', amount=capital, bucket='growth', roles={'equity':'1'})
        db.session.commit()
        funding = _funding(portfolio)
        now, bridge = funding['assessment']['bucket_targets']
        assert funding['assessment']['allocation_capital_amount'] == Decimal(capital)
        if not confirmed or capital == '0':
            assert now['current_percent'] is None and bridge['required_percent'] is None
        else:
            assert now['required_percent'] == Decimal('300')
            assert bridge['required_percent'] == Decimal('700')
    page = client.get('/overview')
    assert page.status_code == 200
    if confirmed and capital == '100':
        assert b'data-unit="percent"' in page.data
        assert Decimal(page.data.split(b'data-maximum="')[1].split(b'"')[0].decode()) == Decimal('700')
        assert b'700.0%' in page.data
    else:
        assert b'data-unit="amount"' in page.data
        assert b'Percentages are unavailable' in page.data
