"""Frozen comparisons, exact annual return ordering and the complete write flow."""

import json
from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import select, text

from app.extensions import db
from app.models import RetirementScenario, Portfolio, CashBalanceCheckpoint, ValuationObservation
from app.services.backup import export_backup, validate_backup, restore_backup, BackupValidationError
from app.services.retirement import run_retirement_projection
from app.services.retirement_plans import save_plan, save_income
from app.services.retirement_scenarios import (
    build_scenario, save_scenario, load_payload, encode_payload, parse_return_path,
    ScenarioValidationError, validate_scenario_record,
)
from test_two_tier_retirement import seed, plan_data, income_data, project
from test_m5_retirement import AS_OF, _position


def test_sequence_effects_and_constant_path_parity(app):
    with app.app_context():
        portfolio, _, _ = seed("1000", spending_policy="full_budget", final_age_years=52)
        down_first = build_scenario(portfolio, AS_OF, name="Down then up", return_path="0,0,-50,0\n0,0,100,0")
        up_first = build_scenario(portfolio, AS_OF, name="Up then down", return_path="0,0,100,0\n0,0,-50,0")
        # Same compounded asset-only growth; withdrawals expose ordering.
        assert down_first["stress"]["final_value_amount"] == D("700")
        assert up_first["stress"]["final_value_amount"] == D("850")
        assert down_first["baseline"]["final_value_amount"] == D("800")
        assert down_first["stress"]["years"][0]["ending_value_amount"] == D("450")
        equal = build_scenario(portfolio, AS_OF, name="Parity", return_path="0,0,0,0")
        assert equal["baseline"] == equal["stress"] == project(portfolio)
        frozen_before = encode_payload(down_first["basis"])
        assert run_retirement_projection(down_first["basis"], down_first["annual_returns"]) == down_first["stress"]
        assert encode_payload(down_first["basis"]) == frozen_before


def test_loss_after_spending_exact_exhaustion_and_resumption(app):
    with app.app_context():
        portfolio, _, _ = seed("1000", spending_policy="full_budget", liquidity_return_decimal=D("0.1"))
        payload = build_scenario(portfolio, AS_OF, name="First year only", return_path="0,0,-50,0")
        years = payload["stress"]["years"]
        assert [row["ending_value_amount"] for row in years] == [D("450"), D("385"), D("313.5")]
        assert years[1]["role_returns"]["liquidity"] == D("0.1")
        loss = build_scenario(portfolio, AS_OF, name="Total loss", return_path="0,0,-100,0")
        assert loss["stress"]["years"][0]["core_paid_amount"] == D("60")
        assert loss["stress"]["lifestyle"]["first_core_shortfall"]["age"] == 51
        save_plan(portfolio.id, plan_data(spending_policy="full_budget", final_age_years=51, core_amount=D("1000"), flexible_amount=D("0")), confirmed=True)
        exact = build_scenario(portfolio, AS_OF, name="Exact final payment", return_path="0,0,-100,0")
        assert exact["stress"]["final_value_amount"] == 0
        assert exact["stress"]["lifestyle"]["core_funded_all_years"] is True


def test_frozen_run_survives_live_changes_and_backup(app):
    with app.app_context():
        portfolio, account, plan = seed("1000", spending_policy="full_budget")
        save_income(portfolio.id, income_data())
        row = save_scenario(portfolio, AS_OF, name="Frozen", return_path="0,0,-20,0")
        db.session.commit()
        saved_id, saved_payload = row.id, row.payload_json
        payload = load_payload(saved_payload)
        assert encode_payload(payload) == saved_payload
        db.session.scalar(select(CashBalanceCheckpoint)).confirmed_balance_amount = D("99999")
        account.present_access_decimal = D("0")
        plan.core_amount = D("999")
        portfolio.reporting_currency_code = "EUR"
        db.session.commit()
        assert db.session.get(RetirementScenario, saved_id).payload_json == saved_payload
        assert encode_payload(run_retirement_projection(payload["basis"], payload["annual_returns"])) == encode_payload(payload["stress"])
        restored = validate_backup(export_backup())
        assert restored.preview.table_counts["retirement_scenarios"] == 1
        restore_backup(restored)
        assert db.session.get(RetirementScenario, saved_id).payload_json == saved_payload
        corrupted = json.loads(export_backup())
        changed = load_payload(saved_payload)
        changed["stress"]["final_value_amount"] += 1
        corrupted["tables"]["retirement_scenarios"][0]["payload_json"] = encode_payload(changed)
        with pytest.raises(BackupValidationError, match="Saved result"):
            validate_backup(json.dumps(corrupted).encode())
        assert db.session.get(RetirementScenario, saved_id).payload_json == saved_payload


def test_mixed_sales_fd_access_projects_and_obligations_survive_freezing(app):
    with app.app_context():
        portfolio, account, _ = seed("-10", spending_policy="full_budget")
        _position(portfolio, account, name="Mixed", amount="1000", bucket="now", roles={"equity": "0.5", "income": "0.5"})
        _position(portfolio, account, name="FD", amount="500", bucket="bridge", roles={"income": "1"}, maturity=date(2027, 8, 30))
        _position(portfolio, account, name="Project", amount="200", bucket="projects", roles={"equity": "1"})
        db.session.commit()
        payload = build_scenario(portfolio, AS_OF, name="Sources", return_path="-50,0,0,0")
        year = payload["stress"]["years"][0]
        assert year["opening_obligation_applied_amount"] == D("10")
        assert year["applications"][1]["role_amounts"] == {"equity": D("30"), "income": D("30")}
        assert year["applications"][2]["role_amounts"] == {"equity": D("20"), "income": D("20")}
        assert year["return_amount"] == D("-222.5")
        assert year["ending_value_amount"] == D("1167.5")
        assert payload["baseline"]["projects_included_amount"] == D("200")
        assert all(app["source_name"] != "Project" for app in year["applications"])
        encoded = encode_payload(payload)
        validate_scenario_record({"name": "Sources", "as_of_date": AS_OF, "portfolio_id": portfolio.id, "payload_json": encoded})
        account.present_access_decimal = D("0")
        account.earliest_access_date = date(2027, 8, 30)
        db.session.commit()
        restricted = build_scenario(portfolio, AS_OF, name="Restricted", return_path="-50,0,0,0")
        assert restricted["stress"]["years"][0]["core_shortfall_amount"] == D("60")
        assert restricted["stress"]["years"][0]["ending_value_amount"] > 0
        assert restricted["stress"]["years"][1]["core_shortfall_amount"] == 0


@pytest.mark.parametrize("path", ["", "0,0,0", "0,0,0,0,0", "0,0,NaN,0", "0,0,Infinity,0", "0,0,-100.1,0", "0,0,101,0", "0,0,0.0000001,0", "0,0,0,0\n\n0,0,0,0", "0,0,0,0\n0,0,0,0\n0,0,0,0\n0,0,0,0"])
def test_return_validation(path):
    with pytest.raises(ScenarioValidationError):
        parse_return_path(path, 3)


def test_no_js_create_recovery_read_only_views_and_missing_inputs(app, client):
    with app.app_context():
        portfolio, _, plan = seed("1000", spending_policy="full_budget")
        initial = export_backup()
    response = client.get("/retirement/scenarios")
    assert response.status_code == 200
    assert b'for="return_path"' in response.data
    fields = {"name": "Early downturn", "as_of_date": AS_OF.isoformat(), "return_path": "0,0,-20,0"}
    failed = client.post("/retirement/scenarios", data={**fields, "return_path": "0,0,NaN,0"})
    assert failed.status_code == 200
    assert b'Row 1:' in failed.data and b'autofocus' in failed.data
    assert b'Early downturn' in failed.data and b'0,0,NaN,0' in failed.data
    with app.app_context():
        assert db.session.scalar(select(RetirementScenario)) is None
    saved = client.post("/retirement/scenarios", data=fields)
    assert saved.status_code == 302
    html = client.get(saved.location)
    assert html.status_code == 200
    assert b'Baseline annual path' in html.data and b'Entered annual return path' in html.data
    assert b'Your adopted plan and Overview are unchanged' in html.data
    assert b'Age 50' in html.data and b'-20.00%' in html.data
    assert client.post(saved.location, data=fields).status_code == 405
    assert client.get("/retirement/scenarios/9999").status_code == 404
    with app.app_context():
        before, after = json.loads(initial)["tables"], json.loads(export_backup())["tables"]
        before.pop("retirement_scenarios")
        after.pop("retirement_scenarios")
        assert before == after
        db.session.delete(db.session.scalar(select(CashBalanceCheckpoint)))
        db.session.commit()
    missing = client.post("/retirement/scenarios", data=fields)
    assert b'incomplete for this date' in missing.data
    assert client.get(saved.location).status_code == 200
    with app.app_context():
        assert len(db.session.scalars(select(RetirementScenario)).all()) == 1


def test_csrf_and_bad_dates(app, client):
    with app.app_context():
        seed()
    data = {"name": "Bad date", "as_of_date": "nonsense", "return_path": "0,0,0,0"}
    assert b'Not a valid date' in client.post("/retirement/scenarios", data=data).data
    app.config["WTF_CSRF_ENABLED"] = True
    assert client.post("/retirement/scenarios", data=data).status_code == 400


def test_v7_migration_and_backup_compatibility(unmigrated_app):
    runner = unmigrated_app.test_cli_runner()
    result = runner.invoke(args=["db", "upgrade", "a4b8d0e2f6c9"])
    assert result.exit_code == 0, result.output
    from legacy_decimal_fixture import legacy_decimal_types, unscale_rows
    with unmigrated_app.app_context(), legacy_decimal_types():
        from sqlalchemy import Table, MetaData
        from datetime import datetime
        from test_m5_retirement import _portfolio_account, _cash
        from test_two_tier_retirement import plan_data
        portfolio, account = _portfolio_account()
        _cash(account, '1000')
        table = Table('retirement_plans', MetaData(), autoload_with=db.engine)
        db.session.execute(table.insert().values(id=1, portfolio_id=portfolio.id, **plan_data(),
            created_at=datetime(2026,8,30), updated_at=datetime(2026,8,30)))
        db.session.commit()
        prior = dict(db.session.execute(text("SELECT * FROM retirement_plans")).mappings().one())
    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output
    with unmigrated_app.app_context():
        after = dict(db.session.execute(text("SELECT * FROM retirement_plans")).mappings().one())
        for key, expected in {"planning_mode":"budget", "annual_savings_amount":0, "legacy_value_basis":"nominal"}.items():
            assert after.pop(key) == expected
        assert unscale_rows("retirement_plans", [after])[0] == unscale_rows("retirement_plans", [prior], legacy=True)[0]
        old = json.loads(export_backup())
        old["version"] = 7
        old["tables"].pop("decimal_conversions", None)
        for row in old['tables']['retirement_plans']:
            for key in ('planning_mode','annual_savings_amount','legacy_value_basis'):
                row.pop(key)
        old["tables"].pop("retirement_scenarios")
        old["tables"].pop("fx_reference_sets")
        validated = validate_backup(json.dumps(old).encode())
        assert validated.decoded_tables["retirement_scenarios"] == []
        restore_backup(validated)
        assert project(db.session.scalar(select(Portfolio)))["final_value_amount"] == D("790")
    drift = runner.invoke(args=["db", "check"])
    assert drift.exit_code == 0, drift.output


def test_long_mixed_role_path_replays_exactly_after_serialization(app):
    with app.app_context():
        portfolio, account, _ = seed("12345.67", final_age_years=93,
            equity_return_decimal=D("0.05321"), income_return_decimal=D("0.02789"),
            liquidity_return_decimal=D("0.0123"), alternatives_return_decimal=D("0.0367"),
            core_amount=D("4567.89"), flexible_amount=D("2345.67"),
            core_inflation_decimal=D("0.022"), flexible_inflation_decimal=D("0.031"))
        _position(portfolio, account, name="Four roles", amount="765432.12", bucket="growth",
                  roles={"equity": "0.4", "income": "0.3", "liquidity": "0.2", "alternatives": "0.1"})
        db.session.commit()
        save_scenario(portfolio, AS_OF, name="Long path", return_path="-30.123456,-5.4321,1,-12\n15,4,2,8")
        db.session.commit()
        validated = validate_backup(export_backup())
        restore_backup(validated)
        row = db.session.scalar(select(RetirementScenario))
        payload = load_payload(row.payload_json)
        assert encode_payload(run_retirement_projection(payload["basis"], payload["annual_returns"])) == encode_payload(payload["stress"])


def test_later_start_date_stale_inputs_and_missing_legacy_fx(app, client):
    with app.app_context():
        portfolio, _, _ = seed("1000", spending_policy="full_budget", terminal_legacy_target_amount=D("100"),
                                terminal_legacy_target_currency_code="GBP")
        later = date(2027, 8, 30)
        row = save_scenario(portfolio, later, name="Already retired", return_path="0,0,0,0")
        db.session.commit()
        payload = load_payload(row.payload_json)
        assert len(payload["stress"]["years"]) == 2
        assert payload["stress"]["legacy_target_gap_amount"] is None
        validate_backup(export_backup())
        url = f"/retirement/scenarios/{row.id}"
    assert b'Missing target FX' in client.get(url).data


def test_missing_income_fx_blocks_save_and_stale_fx_is_frozen(app, client):
    from app.models import FxRate
    with app.app_context():
        portfolio, _, _ = seed()
        save_income(portfolio.id, income_data(currency_code="GBP"))
        db.session.commit()
        with pytest.raises(ScenarioValidationError, match="incomplete"):
            save_scenario(portfolio, AS_OF, name="Missing income FX", return_path="0,0,0,0")
        db.session.add(FxRate(base_currency_code="GBP", quote_currency_code="USD",
            effective_date=date(2026, 8, 1), quote_per_base_amount=D("1.25")))
        db.session.commit()
        row = save_scenario(portfolio, AS_OF, name="Stale FX", return_path="0,0,0,0")
        db.session.commit()
        payload = load_payload(row.payload_json)
        assert payload["baseline"]["status"] == "stale"
        assert payload["stress"]["years"][0]["income_sources"][0]["amount"] == D("100")
        validate_backup(export_backup())
        url = f"/retirement/scenarios/{row.id}"
    assert b'Stale inputs when saved' in client.get(url).data


def test_result_page_lead_when_both_paths_fund_core(app, client):
    with app.app_context():
        portfolio, _, _ = seed("1000", spending_policy="full_budget")
        row = save_scenario(portfolio, AS_OF, name="Funded", return_path="0,0,50,0")
        db.session.commit()
        url = f"/retirement/scenarios/{row.id}"
    html = client.get(url).get_data(as_text=True)
    assert "Core is funded in every retirement year under both paths, under these assumptions." in html


def test_scenario_chart_rounds_decimal_money_before_float_conversion():
    from app.retirement import _scenario_chart
    from app.template_filters import money_amount

    values = [D("1705413.575"), D("2.675"), D("-2.675"), D("2.685")]
    years = [{"age": 50 + i, "ending_value_amount": value} for i, value in enumerate(values)]
    chart = _scenario_chart({"baseline": {"years": years, "reporting_currency": "USD"},
                             "stress": {"years": years}})
    assert chart["stress_values"] == [1705413.58, 2.68, -2.68, 2.68]
    assert chart["baseline_values"] == [float(money_amount(value).replace(",", "")) for value in values]
