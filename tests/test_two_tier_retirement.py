"""Exact two-tier payments, baseline ownership and no-JavaScript adoption."""

import json
import re
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import FxRate, RetirementPlan, RetirementIncome, RetirementAssumption, Portfolio
from app.services.backup import export_backup, validate_backup, restore_backup, BackupValidationError
from app.services.funding import build_funding_summary
from app.services.portfolio_summary import build_portfolio_summary
from app.services.retirement import build_retirement_projection
from app.services.retirement_plans import (
    adopted_plan, save_plan, save_income, validate_plan, PlanValidationError,
    flexible_policy, completed_years,
)
from app.services.spending import build_spending_value
from app.services.planning import save_annual_inflation, PlanningValidationError
from app.services.routine_updates import build_routine_update_session
from test_m5_retirement import AS_OF, _portfolio_account, _cash, _position, _assumptions, _project


D = Decimal


def plan_data(**changes):
    result = dict(base_date=AS_OF, currency_code="USD", spending_policy="guardrails", current_age_years=50,
                  withdrawal_start_age_years=50, final_age_years=53,
                  core_amount=D("60"), flexible_amount=D("40"),
                  core_inflation_decimal=D("0"), flexible_inflation_decimal=D("0"),
                  equity_return_decimal=D("0"), income_return_decimal=D("0"),
                  liquidity_return_decimal=D("0"), alternatives_return_decimal=D("0"),
                  lower_rate_decimal=D("0.03"), upper_rate_decimal=D("0.05"),
                  lower_multiplier_decimal=D("1"), middle_multiplier_decimal=D("0.75"),
                  upper_multiplier_decimal=D("0.25"), terminal_legacy_target_amount=None,
                  terminal_legacy_target_currency_code=None)
    result.update(changes)
    return result


def post_data(**changes):
    result = {}
    for key, value in plan_data(**changes).items():
        if key.endswith("_decimal"):
            key = key.removesuffix("_decimal") + "_percent"
            if value is not None:
                value *= 100
        result[key] = value.isoformat() if isinstance(value, date) else str(value) if value is not None else ""
    return result


def income_data(**changes):
    result = dict(name="Pension", annual_amount=D("80"), currency_code="USD",
                  start_date=AS_OF, end_date=None, inflation_decimal=D("0"))
    result.update(changes)
    return result


def seed(cash="1000", **changes):
    portfolio, account = _portfolio_account()
    _cash(account, cash)
    plan = save_plan(portfolio.id, plan_data(**changes), confirmed=True)
    db.session.commit()
    return portfolio, account, plan


def project(portfolio, as_of=AS_OF, currency="USD"):
    summary = build_portfolio_summary(portfolio, as_of, reporting_currency=currency)
    return build_retirement_projection(portfolio, summary, as_of, reporting_currency=currency)


@pytest.mark.parametrize("rate,band,multiplier", [("0.0299", "lower", "1"), ("0.03", "middle", "0.75"),
                                                ("0.0499", "middle", "0.75"), ("0.05", "upper", "0.25")])
def test_exact_policy_boundaries(rate, band, multiplier):
    result = flexible_policy(SimpleNamespace(**plan_data()), capital=D("10000"), core_need=D(rate)*10000-D("40"), target=D("40"), income_left=D("0"))
    assert result["rate"] == D(rate)
    assert result["band"] == band
    assert result["allowed"] == D("40") * D(multiplier)


def test_core_first_guardrail_cut_and_actual_payments(app):
    with app.app_context():
        portfolio, _, _ = seed("65")
        result = project(portfolio)
        row = result["years"][0]
        assert row["core_paid_amount"] == D("60")
        assert row["flexible_allowed_amount"] == D("10")
        assert row["flexible_paid_amount"] == D("5")
        assert row["flexible_policy_cut_amount"] == D("30")
        assert row["flexible_resource_shortfall_amount"] == D("5")
        assert row["ending_value_amount"] == D("0")
        assert result["lifestyle"]["first_core_shortfall"]["age"] == 51
        assert result["lifestyle"]["longest_flexible_cut_streak"] == 3


def test_income_priority_surplus_returns_and_no_ledger_write(app):
    with app.app_context():
        portfolio, _, _ = seed("1000", liquidity_return_decimal=D("0.1"))
        save_income(portfolio.id, income_data(annual_amount=D("120")))
        db.session.commit()
        result = project(portfolio)
        row = result["years"][0]
        assert row["income_to_core_amount"] == D("60")
        assert row["income_to_flexible_amount"] == D("40")
        assert row["withdrawn_amount"] == D("0")
        assert row["income_saved_amount"] == D("20")
        assert row["return_amount"] == D("102")
        assert row["ending_value_amount"] == D("1122")
        assert db.session.execute(select(db.metadata.tables["transactions"])).all() == []


def test_zero_capital_income_only_and_exact_final_payment(app):
    with app.app_context():
        portfolio, _, _ = seed("0", final_age_years=51)
        save_income(portfolio.id, income_data(annual_amount=D("100")))
        db.session.commit()
        result = project(portfolio)
        row = result["years"][0]
        assert row["full_lifestyle_withdrawal_rate"] is None
        assert row["flexible_band"] == "income_only"
        assert row["core_paid_amount"] == D("60")
        assert row["flexible_paid_amount"] == D("40")
        assert result["lifestyle"]["core_funded_all_years"] is True
        assert result["final_value_amount"] == D("0")


def test_income_after_remaining_opening_obligation(app):
    with app.app_context():
        portfolio, _, _ = seed("-50", final_age_years=51)
        save_income(portfolio.id, income_data(annual_amount=D("100")))
        db.session.commit()
        row = project(portfolio)["years"][0]
        assert row["income_to_obligation_amount"] == D("50")
        assert row["core_paid_amount"] == D("50")
        assert row["core_shortfall_amount"] == D("10")
        assert row["flexible_paid_amount"] == D("0")
        assert row["opening_obligation_remaining_amount"] == D("0")


def test_income_dates_no_proration_no_pre_retirement_payment(app):
    with app.app_context():
        portfolio, _, _ = seed(withdrawal_start_age_years=51, final_age_years=54)
        save_income(portfolio.id, income_data(start_date=date(2026, 9, 1), end_date=date(2028, 8, 30), inflation_decimal=D("0.1")))
        db.session.commit()
        rows = project(portfolio)["years"]
        assert [row["external_income_amount"] for row in rows] == [D("0"), D("80"), D("88"), D("0")]
        assert rows[0]["core_target_amount"] == D("0")


def test_overview_freezes_real_budget_while_projection_retains_tier_inflation(app):
    with app.app_context():
        portfolio, _, plan = seed(core_amount=D("100"), flexible_amount=D("100"), core_inflation_decimal=D("0.1"))
        original = portfolio.annual_spending_amount
        summary = build_portfolio_summary(portfolio, AS_OF)
        funding = build_funding_summary(portfolio, summary, AS_OF, reporting_currency="USD")
        assert funding["periods"][0]["spending_requirement_amount"] == D("300")
        assert funding["assessment"]["required_amount"] == D('1000')
        assert funding["assessment"]["earlier_reserve_amount"] == D("1000")
        assert funding["assessment"]["shortfall_amount"] == D('0')
        assert build_spending_value(portfolio, AS_OF, 'USD')['reporting_amount'] == D('200')
        assert project(portfolio)["years"][1]["core_target_amount"] == D("110")
        assert portfolio.annual_spending_amount == original
        with pytest.raises(PlanningValidationError):
            save_annual_inflation(portfolio.id, D("0.9"))
        assert plan.core_inflation_decimal == D("0.1")


def test_real_reserves_use_budget_at_selected_date_without_projecting_income(app):
    from app.services.backup import export_backup
    with app.app_context():
        portfolio, _, plan = seed(core_amount=D('100'), flexible_amount=D('100'),
                                  core_inflation_decimal=D('.1'), flexible_inflation_decimal=D('.03'))
        save_income(portfolio.id, income_data(annual_amount=D('80')))
        db.session.commit()
        before = export_backup()
        selected = AS_OF.replace(year=AS_OF.year + 1)
        summary = build_portfolio_summary(portfolio, selected)
        funding = build_funding_summary(portfolio, summary, selected, reporting_currency='USD')
        assert funding['spending']['reporting_amount'] == D('110')
        assert funding['periods'][0]['annual_amounts'] == (D('110'),) * 3
        assert funding['periods'][0]['required_amount'] == D('330')
        assert funding['periods'][1]['required_amount'] == D('770')
        assert funding['assessment']['required_amount'] == D('1100')
        assert build_spending_value(portfolio, selected, 'USD')['reporting_amount'] == D('213')
        # Compare source tables; backup generation timestamps may differ.
        import json
        assert json.loads(export_backup())['tables'] == json.loads(before)['tables']


def test_legacy_parity_preserves_existing_assumptions(app):
    with app.app_context():
        portfolio, account = _portfolio_account()
        _cash(account, "-50")
        _position(portfolio, account, name="Mixed holding", amount="700", bucket="growth", roles={"equity": "0.7", "income": "0.3"})
        _position(portfolio, account, name="Term source", amount="300", bucket="now", roles={"liquidity": "1"}, maturity=date(2027, 8, 30))
        _position(portfolio, account, name="Projects", amount="400", bucket="projects", roles={"equity": "1"})
        _assumptions(portfolio)
        db.session.commit()
        before = _project(portfolio)
        assumption = db.session.scalar(select(RetirementAssumption))
        save_plan(portfolio.id, plan_data(core_amount=D("100"), flexible_amount=D("0"),
                  current_age_years=assumption.current_age_years, withdrawal_start_age_years=assumption.withdrawal_start_age_years,
                  final_age_years=assumption.final_age_years,
                  **{f"{role}_return_decimal": getattr(assumption, f"{role}_return_decimal") for role in ("equity", "income", "liquidity", "alternatives")}), confirmed=True)
        db.session.commit()
        after = _project(portfolio)
        for old, new in zip(before["years"], after["years"], strict=True):
            for field in ("starting_value_amount", "withdrawn_amount", "ending_value_amount", "return_amount", "shortfall_amount"):
                assert new[field] == old[field]
        assert before["final_value_amount"] == after["final_value_amount"]
        assert db.session.get(RetirementAssumption, assumption.id) is assumption


def test_restricted_assets_can_have_core_shortfall_with_positive_capital(app):
    with app.app_context():
        portfolio, account, _ = seed("0", final_age_years=51)
        account.present_access_decimal = D("0")
        _position(portfolio, account, name="Restricted fund", amount="10000", bucket="growth", roles={"equity": "1"})
        db.session.commit()
        result = project(portfolio)
        assert result["final_value_amount"] == D("10000")
        assert result["lifestyle"]["core_funded_all_years"] is False
        assert result["years"][0]["core_shortfall_amount"] == D("60")


@pytest.mark.parametrize("change,field", [({"core_amount": D("NaN")}, "core_amount"),
    ({"core_amount": D("1e100")}, "core_amount"), ({"core_amount": 1.2}, "core_amount"),
    ({"core_amount": D("0"), "flexible_amount": D("0")}, "core_amount"),
    ({"upper_rate_decimal": D("0.02")}, "upper_rate_decimal"),
    ({"upper_multiplier_decimal": D("1.5")}, "upper_multiplier_decimal"),
    ({"withdrawal_start_age_years": 49}, "withdrawal_start_age_years"),
    ({"currency_code": "US"}, "currency_code")])
def test_plan_validation(change, field):
    with pytest.raises(PlanValidationError) as caught:
        validate_plan(plan_data(**change))
    assert caught.value.field == field


def test_no_js_review_adoption_and_input_recovery(app, client):
    with app.app_context():
        portfolio, account = _portfolio_account()
        _cash(account, "1000")
        _position(portfolio, account, name="Fund", amount="100", bucket="growth", roles={"equity": "1"})
        db.session.commit()
        portfolio_id = portfolio.id
    response = client.get("/retirement/budget")
    assert response.status_code == 200
    assert b"Existing retirement projection" in response.data
    data = post_data()
    preview = client.post("/retirement/budget", data={**data, "preview_plan": "1"})
    assert preview.status_code == 200 and b"Review adoption" in preview.data
    with app.app_context():
        assert adopted_plan(portfolio_id) is None
    invalid = client.post("/retirement/budget", data={**data, "core_amount": "bad", "confirm_adoption": "y", "save_projection": "1"})
    assert b'value="bad"' in invalid.data and b'aria-invalid="true"' in invalid.data
    import re
    focused = re.findall(r'<input\b[^>]*\bautofocus\b[^>]*>', invalid.get_data(as_text=True))
    assert len(focused) == 1 and 'id="core_amount"' in focused[0]
    adopted = client.post("/retirement/budget", data={**data, "confirm_adoption": "y", "save_projection": "1"}, follow_redirects=True)
    assert adopted.status_code == 200
    assert b"Core lifestyle" in adopted.data
    assert b"Annual spending and portfolio path" in adopted.data
    assert b'aria-current="page">Retirement' in adopted.data
    for url in ("/overview", "/settings", "/values", "/setup?step=portfolio"):
        assert client.get(url).status_code == 200
    assert client.get("/planning/retirement").status_code == 302
    assert client.get("/planning/funding").status_code == 302


def test_income_crud_and_confirmed_removal(app, client):
    with app.app_context():
        portfolio, _, _ = seed()
    data = dict(name="Pension", annual_amount="80", currency_code="USD", start_date=AS_OF.isoformat(), end_date="", inflation_percent="0")
    assert client.post("/retirement/income/new", data=data).status_code == 302
    with app.app_context():
        row = db.session.scalar(select(RetirementIncome))
        income_id = row.id
    page = client.get(f"/retirement/income/{income_id}")
    assert page.status_code == 200 and b'value="80"' in page.data
    invalid = client.post(f"/retirement/income/{income_id}", data={**data, "end_date": "2025-01-01"})
    assert b'aria-invalid="true"' in invalid.data
    assert client.post(f"/retirement/income/{income_id}", data={"remove_income": "1"}).status_code == 200
    with app.app_context():
        assert db.session.get(RetirementIncome, income_id) is not None
    assert client.post(f"/retirement/income/{income_id}", data={"remove_income": "1", "confirm_remove": "y"}).status_code == 302


@pytest.mark.parametrize("already_adopted", [False, True])
def test_unconfirmed_adoption_preserves_plan_and_focuses_confirmation(app, client, already_adopted):
    with app.app_context():
        if already_adopted:
            portfolio, _, _ = seed()
        else:
            portfolio, account = _portfolio_account()
            _cash(account, "1000")
            db.session.commit()
        portfolio_id = portfolio.id
    data = {**post_data(core_amount=D("123")), "save_projection": "1"}
    response = client.post("/retirement/budget", data=data)
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Confirm that Retirement and Overview should use this plan before adopting it." in html
    assert 'href="#confirm_adoption"' in html
    assert 'value="123"' in html
    focused = re.findall(r'<[^>]+\bautofocus\b[^>]*>', html)
    assert len(focused) == 1 and 'id="confirm_adoption"' in focused[0]
    with app.app_context():
        plan = adopted_plan(portfolio_id)
        assert plan.core_amount == D("60") if already_adopted else plan is None
    assert client.post("/retirement/budget", data={**data, "confirm_adoption": "y"}).status_code == 302
    with app.app_context():
        assert adopted_plan(portfolio_id).core_amount == D("123")


def test_annual_presentation_preserves_chart_payments_and_allocation_evidence(app, client):
    from html import unescape
    from app.retirement import _spending_chart
    with app.app_context():
        portfolio, account, _ = seed("0", final_age_years=51)
        _position(portfolio, account, name="Mixed holding", amount="50", bucket="growth", roles={"equity": "0.7", "income": "0.3"})
        db.session.commit()
        result = project(portfolio)
        chart = _spending_chart(result)
        assert result["years"][0]["core_paid_amount"] == D("50")
        assert chart["core_values"] == [50.0]
        assert chart["flexible_values"] == [0.0]
        assert chart["desired_values"] == [100.0]
    html = client.get("/retirement/budget").get_data(as_text=True)
    assert json.loads(unescape(re.search(r'data-core="([^"]+)"', html)[1])) == [50.0]
    assert "Ending allocation by role at age 50" in html
    assert "Ending capital by bucket at age 50" in html
    assert "Mixed holding · Growth (10+ years) · Core" in html
    assert "Modelled return after withdrawals" in html
    assert "Not applicable" in html  # No invented allocation percentage after exhaustion.


def test_backup_v6_roundtrip_and_v5_preservation(app):
    with app.app_context():
        portfolio, _, _ = seed()
        save_income(portfolio.id, income_data())
        db.session.commit()
        before = project(portfolio)["years"][0]["ending_value_amount"]
        blob = export_backup()
        assert json.loads(blob)["version"] == 11
        restore_backup(validate_backup(blob))
        portfolio = db.session.scalar(select(Portfolio))
        assert project(portfolio)["years"][0]["ending_value_amount"] == before
        old = json.loads(blob)
        old["version"] = 5
        old["tables"].pop("decimal_conversions", None)
        old["tables"].pop("retirement_scenarios")
        old["tables"].pop("fx_reference_sets")
        old["tables"].pop("retirement_plans")
        old["tables"].pop("retirement_income")
        restore_backup(validate_backup(json.dumps(old).encode()))
        assert db.session.scalar(select(RetirementPlan)) is None
        assert db.session.scalar(select(Portfolio)).annual_spending_amount == D("100")


def test_invalid_plan_backup_is_rejected(app):
    with app.app_context():
        seed()
        payload = json.loads(export_backup())
        payload["tables"]["retirement_plans"][0]["upper_rate_decimal"] = "0.01"
        with pytest.raises(BackupValidationError):
            validate_backup(json.dumps(payload).encode())


def test_income_fx_missing_stale_and_routine_dependency(app):
    with app.app_context():
        portfolio, _, _ = seed()
        save_income(portfolio.id, income_data(currency_code="GBP"))
        db.session.commit()
        result = project(portfolio)
        assert not result["calculation_complete"]
        assert result["years"] == ()
        assert any(reason["kind"] == "missing_income_fx" for reason in result["reasons"])
        rows = build_routine_update_session(portfolio, AS_OF)["fx_rows"]
        assert any({row["base_currency_code"], row["quote_currency_code"]} == {"GBP", "USD"} for row in rows)
        db.session.add(FxRate(base_currency_code="GBP", quote_currency_code="USD", effective_date=AS_OF-timedelta(days=10), quote_per_base_amount=D("1.25")))
        db.session.commit()
        result = project(portfolio)
        assert result["status"] == "stale"
        assert result["years"][0]["external_income_amount"] == D("100")


def test_plan_date_age_anchor_and_currency_view(app):
    with app.app_context():
        portfolio, _, _ = seed()
        db.session.add(FxRate(base_currency_code="USD", quote_currency_code="EUR", effective_date=AS_OF, quote_per_base_amount=D("0.8")))
        db.session.commit()
        before = project(portfolio)
        euros = project(portfolio, currency="EUR")
        assert euros["years"][0]["core_paid_amount"] == D("48")
        assert euros["years"][0]["full_lifestyle_withdrawal_rate"] == before["years"][0]["full_lifestyle_withdrawal_rate"]
        assert adopted_plan(portfolio.id).currency_code == "USD"
        assert project(portfolio, as_of=date(2027, 8, 30))["years"][0]["age"] == 51
        assert not project(portfolio, as_of=date(2029, 8, 30))["calculation_complete"]
        assert build_spending_value(portfolio, date(2026, 8, 29), "USD")["reporting_amount"] is None
        assert completed_years(date(2024, 2, 29), date(2025, 2, 28)) == 1


@pytest.mark.parametrize("state", ["funded", "shortfall", "missing", "stale", "restricted", "legacy", "surplus"])
def test_long_horizon_demo_states_render(app, client, state):
    from retirement_demo import seed_demo
    with app.app_context():
        seed_demo(state)
        if state == "funded":
            portfolio = db.session.scalar(select(Portfolio))
            assert project(portfolio)["lifestyle"]["core_funded_all_years"] is True
    assert client.get("/retirement/budget").status_code == 200


def test_full_mixed_sale_leaves_exact_zero():
    from app.services.retirement import _remove_proportionally, _pool_total
    pool = {"roles": {"equity": D("787427.2926283737288233134739"), "income": D("131943.2983932239343227382113")}}
    removed = _remove_proportionally(pool, _pool_total(pool))
    assert pool["roles"] == {"equity": D("0"), "income": D("0")}
    assert removed["equity"] == D("787427.2926283737288233134739")


def test_large_rate_display_preserves_domain_value():
    from app.template_filters import percentage_amount
    value = D("1e29")
    assert percentage_amount(value, 2) == "10,000,000,000,000,000,000,000,000,000,000.00"
    assert value == D("1e29")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e100"])
def test_nonfinite_and_oversized_form_values_recover(app, client, value):
    with app.app_context():
        seed()
    result = client.post("/retirement/budget", data={**post_data(), "core_amount": value})
    assert result.status_code == 200 and b'aria-invalid="true"' in result.data


def test_csrf_required_for_plan_and_income_writes(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    with app.app_context():
        seed()
    assert client.post("/retirement/budget", data=post_data()).status_code == 400
    assert client.post("/retirement/income/new", data={}).status_code == 400


def test_adopted_plan_closes_legacy_spending_write_path(app, client):
    with app.app_context():
        portfolio, _, _ = seed()
        portfolio_id = portfolio.id
    result = client.post("/setup/portfolio", data={"name": "New name", "reporting_currency_code": "EUR",
        "annual_spending_amount": "9999", "annual_spending_currency_code": "GBP", "default_as_of_date": AS_OF.isoformat()})
    assert result.status_code == 302
    with app.app_context():
        portfolio = db.session.get(Portfolio, portfolio_id)
        assert portfolio.annual_spending_amount == D("100")
        assert portfolio.annual_spending_currency_code == "USD"
        assert adopted_plan(portfolio_id).currency_code == "USD"
        assert adopted_plan(portfolio_id).core_amount == D("60")


def test_missing_core_target_is_not_a_core_shortfall(app, client):
    with app.app_context():
        portfolio, _, _ = seed(core_amount=D("0"))
        result = project(portfolio)
        assert result["lifestyle"]["core_funded_all_years"] is None
    assert b"No Core spending specified" in client.get("/retirement/budget").data


def test_additive_migration_preserves_prior_data_and_has_no_drift(unmigrated_app):
    from sqlalchemy import text
    runner = unmigrated_app.test_cli_runner()
    prior = runner.invoke(args=["db", "upgrade", "e2f6a8c0d3b5"])
    assert prior.exit_code == 0, prior.output
    from legacy_decimal_fixture import legacy_decimal_types, unscale_rows
    with unmigrated_app.app_context(), legacy_decimal_types():
        portfolio, account = _portfolio_account()
        _cash(account, "1000")
        _assumptions(portfolio)
        db.session.commit()
        before = db.session.execute(text("SELECT * FROM retirement_assumptions")).mappings().all()
    upgraded = runner.invoke(args=["db", "upgrade"])
    assert upgraded.exit_code == 0, upgraded.output
    with unmigrated_app.app_context():
        after = db.session.execute(text("SELECT * FROM retirement_assumptions")).mappings().all()
        assert unscale_rows("retirement_assumptions", after) == unscale_rows("retirement_assumptions", before, legacy=True)
        assert db.session.scalar(select(RetirementPlan)) is None
        assert db.session.scalar(select(Portfolio)).annual_spending_amount == D("100")
        validate_backup(export_backup())
    drift = runner.invoke(args=["db", "check"])
    assert drift.exit_code == 0, drift.output


def test_income_edit_is_scoped_and_failed_plan_save_is_atomic(app):
    with app.app_context():
        portfolio, _, plan = seed()
        other, _ = _portfolio_account()
        save_plan(other.id, plan_data(), confirmed=True)
        row = save_income(other.id, income_data())
        db.session.commit()
        with pytest.raises(PlanValidationError):
            save_income(portfolio.id, income_data(annual_amount=D("999")), income_id=row.id)
        with pytest.raises(PlanValidationError):
            save_plan(portfolio.id, plan_data(core_amount=D("900"), upper_rate_decimal=D("0.01")), confirmed=True)
        assert plan.core_amount == D("60")
        assert row.annual_amount == D("80")


def test_fx_rows_include_plan_currency_when_reporting_differs(app):
    with app.app_context():
        portfolio, _, plan = seed(currency_code="EUR")
        save_income(portfolio.id, income_data(currency_code="GBP"))
        db.session.commit()
        pairs = {frozenset((row["base_currency_code"], row["quote_currency_code"])) for row in build_routine_update_session(portfolio, AS_OF)["fx_rows"]}
        assert {frozenset(("EUR", "USD")), frozenset(("GBP", "EUR")), frozenset(("GBP", "USD"))} <= pairs


def test_invalid_view_date_blocks_adoption_without_losing_input(app, client):
    import re
    with app.app_context():
        portfolio, _, _ = seed()
        portfolio_id = portfolio.id
    result = client.post("/retirement/budget?as_of=bad", data={**post_data(core_amount=D("123")), "confirm_adoption": "y", "save_projection": "1"})
    assert result.status_code == 200 and b'value="123"' in result.data
    focused = re.findall(r'<input\b[^>]*\bautofocus\b[^>]*>', result.get_data(as_text=True))
    assert len(focused) == 1 and 'id="retirement-as-of"' in focused[0]
    with app.app_context():
        assert adopted_plan(portfolio_id).core_amount == D("60")
