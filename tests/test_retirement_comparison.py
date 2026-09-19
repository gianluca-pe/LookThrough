"""Spending comparisons never adopt an alternative or infer safety from a rate."""

import json
from datetime import datetime
from decimal import Decimal as D

import pytest
from sqlalchemy import MetaData, Table, select, text

from app.extensions import db
from app.models import Portfolio, RetirementIncome
from app.services.backup import export_backup, restore_backup, validate_backup, BackupValidationError
from app.services.retirement_comparison import build_retirement_comparison
from app.services.retirement_plans import RULE_FIELDS, adopted_plan, save_plan, save_income, validate_plan, PlanValidationError
from app.services.portfolio_summary import build_portfolio_summary
from test_two_tier_retirement import seed, plan_data, post_data, project, income_data
from test_m5_retirement import AS_OF, _portfolio_account, _cash


def compare(portfolio):
    summary = build_portfolio_summary(portfolio, AS_OF, reporting_currency="USD")
    return build_retirement_comparison(portfolio, summary, AS_OF, reporting_currency="USD")


def test_comparison_exposes_unneeded_rule_cuts_without_mutating_saved_plan(app):
    with app.app_context():
        portfolio, _, plan = seed(terminal_legacy_target_amount=D("600"), terminal_legacy_target_currency_code="USD")
        before = export_backup()
        result = compare(portfolio)
        full, rules = result["full_budget"], result["guardrails"]
        assert full["lifestyle"]["full_budget_funded_all_years"] is True
        assert rules["lifestyle"]["flexible_cut_years"] == 3
        assert full["final_value_amount"] == D("700")
        assert rules["final_value_amount"] == D("790")
        assert full["legacy_target_gap_amount"] == D("100")
        assert result["difference"] == {"flexible_spending_difference_amount": D("90"), "ending_capital_difference_amount": D("90")}
        first = rules["years"][0]
        assert first["flexible_cut_reason"] == "rule"
        assert first["flexible_full_target_payable"] is True
        assert first["flexible_target_resource_shortfall_amount"] == 0
        assert plan.spending_policy == "guardrails"
        assert not db.session.dirty
        # Export timestamp is independent; all persisted tables are unchanged.
        assert json.loads(export_backup())["tables"] == json.loads(before)["tables"]


@pytest.mark.parametrize("cash,reason,gap", [("65", "rule_and_resources", "35"), ("1000", "rule", "0")])
def test_annual_cut_explanation_checks_resources_on_its_own_path(app, cash, reason, gap):
    with app.app_context():
        portfolio, _, _ = seed(cash)
        result = compare(portfolio)
        first = result["guardrails"]["years"][0]
        assert first["flexible_cut_reason"] == reason
        assert first["flexible_target_resource_shortfall_amount"] == D(gap)
        if cash == "65":
            assert result["full_budget"]["years"][0]["flexible_cut_reason"] == "resources"
            assert result["full_budget"]["lifestyle"]["full_budget_funded_all_years"] is False


def test_above_target_rules_can_spend_more_and_leave_less(app):
    with app.app_context():
        portfolio, _, _ = seed("100000", lower_multiplier_decimal=D("1.2"))
        result = compare(portfolio)
        assert result["difference"]["flexible_spending_difference_amount"] == D("-24")
        assert result["difference"]["ending_capital_difference_amount"] == D("-24")


def test_surplus_demo_funds_full_budget_to_exact_zero_but_rules_leave_unwanted_legacy(app):
    from retirement_demo import seed_demo
    with app.app_context():
        seed_demo("surplus")
        result = compare(db.session.scalar(select(Portfolio)))
        assert result["full_budget"]["lifestyle"]["full_budget_funded_all_years"] is True
        assert result["full_budget"]["final_value_amount"] == D("0")
        assert result["guardrails"]["final_value_amount"] == D("200000")
        assert result["guardrails"]["lifestyle"]["flexible_cut_years"] == 20


def test_full_budget_choice_needs_no_rule_numbers_and_still_limits_resources(app, client):
    with app.app_context():
        portfolio, account = _portfolio_account()
        _cash(account, "0")
        db.session.commit()
        portfolio_id = portfolio.id
    fields = {**dict.fromkeys(RULE_FIELDS), "spending_policy": "full_budget"}
    data = {**post_data(**fields), "save_projection": "1", "confirm_adoption": "y"}
    assert client.post("/retirement/budget", data=data).status_code == 302
    with app.app_context():
        plan = adopted_plan(portfolio_id)
        assert plan.spending_policy == "full_budget"
        assert all(getattr(plan, field) is None for field in RULE_FIELDS)
        save_income(portfolio_id, income_data())
        db.session.commit()
        result = compare(db.session.get(Portfolio, portfolio_id))
        assert result["guardrails"] is None
        assert result["difference"] is None
        row = result["full_budget"]["years"][0]
        assert row["core_paid_amount"] == D("60")
        assert row["flexible_paid_amount"] == D("20")
        assert row["full_lifestyle_withdrawal_rate"] is None
        assert row["flexible_cut_reason"] == "resources"
    html = client.get("/retirement/budget?path=guardrails").get_data(as_text=True)
    assert "No adjustment rules entered" in html
    assert "Full-budget path" in html


def test_disabling_rules_preserves_numbers_and_reenabling_restores_result(app):
    with app.app_context():
        portfolio, _, plan = seed()
        before = project(portfolio)["final_value_amount"]
        rules = {field: getattr(plan, field) for field in RULE_FIELDS}
        save_plan(portfolio.id, plan_data(spending_policy="full_budget", **dict.fromkeys(RULE_FIELDS)), confirmed=True)
        db.session.commit()
        assert {field: getattr(plan, field) for field in RULE_FIELDS} == rules
        assert project(portfolio)["final_value_amount"] == D("700")
        save_plan(portfolio.id, plan_data(**rules), confirmed=True)
        assert project(portfolio)["final_value_amount"] == before


@pytest.mark.parametrize("policy", [None, "unknown", [], "guardrails"])
def test_missing_or_partial_rule_configuration_cannot_be_saved(policy):
    with pytest.raises(PlanValidationError):
        validate_plan(plan_data(spending_policy=policy, lower_rate_decimal=None))


def test_comparison_routes_keep_path_selection_read_only_and_unsaved_preview_separate(app, client):
    with app.app_context():
        portfolio, _, plan = seed()
        portfolio_id = portfolio.id
    for path in ("full_budget", "guardrails"):
        html = client.get(f"/retirement/budget?path={path}").get_data(as_text=True)
        assert 'aria-current="true">Inspect the ' + ("full-budget" if path == "full_budget" else "adjustment-rule") in html
        assert "Can I fund my planned lifestyle?" in html
    data = {**post_data(spending_policy="full_budget", core_amount=D("999")), "preview_plan": "1"}
    html = client.post("/retirement/budget?path=guardrails", data=data).get_data(as_text=True)
    assert "both paths above use your saved assumptions" in html
    assert "keep my planned Flexible budget</strong>" in html
    with app.app_context():
        plan = adopted_plan(portfolio_id)
        assert plan.spending_policy == "guardrails"
        assert plan.core_amount == D("60")


def test_missing_income_fx_withholds_both_paths_and_legacy_fx_is_independent(app, client):
    with app.app_context():
        portfolio, _, _ = seed(terminal_legacy_target_amount=D("100"), terminal_legacy_target_currency_code="GBP")
        result = compare(portfolio)
        assert result["calculation_complete"]
        assert result["full_budget"]["legacy_target_gap_amount"] is None
        save_income(portfolio.id, income_data(currency_code="EUR"))
        db.session.commit()
        result = compare(portfolio)
        assert not result["calculation_complete"]
        assert result["difference"] is None
        assert result["full_budget"]["years"] == result["guardrails"]["years"] == ()
    html = client.get("/retirement/budget").get_data(as_text=True)
    assert "comparison needs complete" in html
    assert "Update income FX" in html


def test_v6_backup_preserves_rules_and_v7_roundtrip_preserves_full_budget(app):
    with app.app_context():
        portfolio, _, _ = seed()
        old = json.loads(export_backup())
        old["version"] = 6
        old["tables"].pop("decimal_conversions", None)
        old["tables"].pop("retirement_scenarios")
        old["tables"].pop("fx_reference_sets")
        for row in old["tables"]["retirement_plans"]:
            row.pop("spending_policy")
            for key in ("planning_mode", "annual_savings_amount", "legacy_value_basis"):
                row.pop(key)
        restore_backup(validate_backup(json.dumps(old).encode()))
        portfolio = db.session.scalar(select(Portfolio))
        assert adopted_plan(portfolio.id).spending_policy == "guardrails"
        assert project(portfolio)["final_value_amount"] == D("790")
        save_plan(portfolio.id, plan_data(spending_policy="full_budget"), confirmed=True)
        db.session.commit()
        restore_backup(validate_backup(export_backup()))
        portfolio = db.session.scalar(select(Portfolio))
        assert adopted_plan(portfolio.id).spending_policy == "full_budget"
        assert project(portfolio)["final_value_amount"] == D("700")
        old["tables"]["retirement_plans"][0]["lower_rate_decimal"] = None
        with pytest.raises(BackupValidationError):
            validate_backup(json.dumps(old).encode())


@pytest.mark.parametrize("fail_restore", [False, True])
def test_populated_migration_preserves_plan_income_and_foreign_keys(unmigrated_app, monkeypatch, fail_restore):
    runner = unmigrated_app.test_cli_runner()
    upgraded = runner.invoke(args=["db", "upgrade", "f3a7c9d1e5b8"])
    assert upgraded.exit_code == 0, upgraded.output
    from legacy_decimal_fixture import legacy_decimal_types, unscale_rows
    with unmigrated_app.app_context(), legacy_decimal_types():
        portfolio, account = _portfolio_account()
        _cash(account, "1000")
        db.session.commit()
        metadata = MetaData()
        table = Table("retirement_plans", metadata, autoload_with=db.engine)
        values = plan_data()
        values.pop("spending_policy")
        db.session.execute(table.insert().values(id=1, portfolio_id=portfolio.id, **values,
            created_at=datetime(2026, 8, 30), updated_at=datetime(2026, 8, 30)))
        income = Table("retirement_income", metadata, autoload_with=db.engine)
        db.session.execute(income.insert().values(id=1, plan_id=1, **income_data(),
            created_at=datetime(2026, 8, 30), updated_at=datetime(2026, 8, 30)))
        db.session.commit()
        before = dict(db.session.execute(text("SELECT * FROM retirement_plans")).mappings().one())
        incomes = db.session.execute(text("SELECT * FROM retirement_income")).mappings().all()
    if fail_restore:
        original_create = Table.create
        def fail_income_create(table, *args, **kwargs):
            if table.name == "retirement_income":
                raise RuntimeError("Injected income restore failure")
            return original_create(table, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(Table, "create", fail_income_create)
            failed = runner.invoke(args=["db", "upgrade"])
        assert failed.exit_code != 0
        with unmigrated_app.app_context():
            assert dict(db.session.execute(text("SELECT * FROM retirement_plans")).mappings().one()) == before
            assert db.session.execute(text("SELECT * FROM retirement_income")).mappings().all() == incomes
            assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "f3a7c9d1e5b8"
    upgraded = runner.invoke(args=["db", "upgrade"])
    assert upgraded.exit_code == 0, upgraded.output
    with unmigrated_app.app_context():
        after = dict(db.session.execute(text("SELECT * FROM retirement_plans")).mappings().one())
        assert after.pop("spending_policy") == "guardrails"
        assert after.pop("planning_mode") == "budget"
        assert after.pop("annual_savings_amount") == 0
        assert after.pop("legacy_value_basis") == "nominal"
        assert unscale_rows("retirement_plans", [after])[0] == unscale_rows("retirement_plans", [before], legacy=True)[0]
        assert unscale_rows("retirement_income", db.session.execute(text("SELECT * FROM retirement_income")).mappings().all()) == unscale_rows("retirement_income", incomes, legacy=True)
        assert db.session.execute(text("PRAGMA foreign_key_check")).all() == []
        assert db.session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        validate_backup(export_backup())
    drift = runner.invoke(args=["db", "check"])
    assert drift.exit_code == 0, drift.output


# --- presentation: the four owner questions -------------------


def test_inspected_path_is_identified_against_the_saved_approach(app, client):
    with app.app_context():
        seed("1000")
    html = client.get("/retirement/budget").get_data(as_text=True)
    assert "You are inspecting this path only" in html
    assert "applies your spending adjustment rules" in html
    assert "This is the spending approach saved in your plan" not in html
    html = client.get("/retirement/budget?path=guardrails").get_data(as_text=True)
    assert "This is the spending approach saved in your plan" in html
    assert "You are inspecting this path only" not in html


def test_annual_table_names_mixed_and_resource_cut_reasons(app, client):
    with app.app_context():
        seed("65")
    rules = client.get("/retirement/budget?path=guardrails").get_data(as_text=True)
    assert "— your rule and available money" in rules
    full = client.get("/retirement/budget").get_data(as_text=True)
    assert "— available money" in full


def test_annual_table_names_rule_only_cut_reason(app, client):
    with app.app_context():
        seed("1000")
    rule_only = client.get("/retirement/budget?path=guardrails").get_data(as_text=True)
    assert "— your rule" in rule_only
    assert "— your rule and available money" not in rule_only


def test_comparison_explains_why_rules_cut_with_concrete_amounts(app, client):
    with app.app_context():
        seed("1000")
    html = client.get("/retirement/budget").get_data(as_text=True)
    assert "When your rules reduce spending" in html
    assert "first at age 50" in html
    assert "10.00% of that year&#39;s starting portfolio" in html or "10.00% of that year's starting portfolio" in html
    assert "allow 25% of the Flexible budget" in html
    assert "10.00</span> of the" in html
    assert "40.00</span> target" in html


def test_resource_bound_states_explain_triggered_but_overridden_rules(app, client):
    """Rules triggered but resources bind equally: no contradiction, no blame."""
    with app.app_context():
        seed("65")
    html = client.get("/retirement/budget").get_data(as_text=True)
    assert "same total Flexible spending and ending balance" in html
    assert "both paths produce the same result" not in html
    assert "When your rules reduce spending" in html
    assert "Your rules never reduce" not in html


def test_equal_lifetime_totals_do_not_claim_identical_yearly_payments(app, client):
    with app.app_context():
        portfolio, _, _ = seed("200", core_amount=D("0"), flexible_amount=D("100"),
            final_age_years=52, lower_rate_decimal=D("0.6"), upper_rate_decimal=D("0.8"),
            lower_multiplier_decimal=D("1.5"), middle_multiplier_decimal=D("1"),
            upper_multiplier_decimal=D("0.5"))
        result = compare(portfolio)
        assert [y["flexible_paid_amount"] for y in result["full_budget"]["years"]] == [D("100"), D("100")]
        assert [y["flexible_paid_amount"] for y in result["guardrails"]["years"]] == [D("150"), D("50")]
        assert result["difference"] == {"flexible_spending_difference_amount": D("0"), "ending_capital_difference_amount": D("0")}
    html = client.get("/retirement/budget").get_data(as_text=True)
    assert "same total Flexible spending and ending balance" in html
    assert "Yearly payments may differ" in html
    assert "limited both paths to the same payments" not in html


def test_neutral_rules_are_stated_as_no_change(app, client):
    with app.app_context():
        seed("1000", lower_multiplier_decimal=D("1"), middle_multiplier_decimal=D("1"),
             upper_multiplier_decimal=D("1"))
    html = client.get("/retirement/budget").get_data(as_text=True)
    assert "both paths produce the same result" in html
    assert "Your rules never reduce the Flexible budget in any year" in html
    assert "more than the full budget" not in html


def test_above_budget_rules_are_stated_without_claiming_cuts(app, client):
    with app.app_context():
        seed("100000", lower_multiplier_decimal=D("1.2"))
    html = client.get("/retirement/budget").get_data(as_text=True)
    assert "more Flexible spending" in html
    assert "less money at the end" in html
    assert "they allow more than the full budget in at least one year" in html


def test_zero_legacy_gap_reads_exactly_rather_than_zero_more(app, client):
    with app.app_context():
        seed("1000", terminal_legacy_target_amount=D("700"),
             terminal_legacy_target_currency_code="USD")
    html = client.get("/retirement/budget").get_data(as_text=True)
    assert "Exactly the amount you want to leave" in html
    assert "90.00</span> more than you want to leave" in html
    assert '<span class="ccy">USD</span> 0.00</span> more' not in html
    assert "Above or equal target by" not in html


def test_unfunded_full_budget_names_the_first_affected_years(app, client):
    with app.app_context():
        seed("65")
    html = client.get("/retirement/budget").get_data(as_text=True)
    assert "not funded in every retirement year" in html
    assert "Core is first short at age 51" in html
    assert "The full Flexible budget is first unpaid at age 50" in html
    assert "35.00</span> of the" in html
    assert "40.00</span> target" in html


def test_review_card_states_paths_keep_saved_assumptions(app, client):
    with app.app_context():
        seed("1000")
    data = {**post_data(core_amount=D("70")), "preview_plan": "1"}
    html = client.post("/retirement/budget", data=data).get_data(as_text=True)
    assert "still use your saved assumptions" in html
    assert "they update only when you adopt this plan" in html


def test_year_summaries_drop_redundant_full_budget_share_and_name_cuts(app, client):
    with app.app_context():
        seed("1000")
    full = client.get("/retirement/budget").get_data(as_text=True)
    assert "keep the full Flexible budget" in full
    assert "allows 100% of target" not in full
    rules = client.get("/retirement/budget?path=guardrails").get_data(as_text=True)
    assert "allows 25% of target" in rules
    assert "Flexible cut" in rules
    assert "(your rule)" in rules
