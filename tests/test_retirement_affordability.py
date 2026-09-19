"""Owner-outcome checks for steady real lifestyle affordability."""
from decimal import Decimal as D
from datetime import date

from app.services.retirement_affordability import prepare_affordability, solve_affordability, _trial
from test_two_tier_retirement import seed, plan_data
from test_m5_retirement import AS_OF, _position
from app.extensions import db


def inputs(**changes):
    data = plan_data(spending_policy="full_budget")
    data.update(inflation_decimal=D("0"), annual_savings_amount=D("0"), terminal_legacy_target_amount=D("100"))
    data.update(changes)
    return data


def solve(portfolio, **changes):
    basis, summary = prepare_affordability(portfolio, inputs(**changes))
    return solve_affordability(basis), basis


def test_maximum_steady_allowance_and_cent_boundary(app):
    with app.app_context():
        portfolio, _, _ = seed()
        result, basis = solve(portfolio)
        assert result["status"] == "funded"
        assert result["flexible_amount"] == D("240")
        assert result["projection"]["final_value_amount"] == D("100")
        assert _trial(basis, D("240.01"))["final_value_amount"] < D("100")
        assert all(y["flexible_paid_today"] == D("240") for y in result["projection"]["years"])


def test_single_inflation_and_end_boundary_legacy(app):
    with app.app_context():
        portfolio, _, _ = seed()
        result, basis = solve(portfolio, final_age_years=52, core_amount=D("100"), inflation_decimal=D("0.1"))
        assert basis["legacy_target_reporting_amount"] == D("121")
        assert result["legacy_nominal_amount"] == D("121")
        assert result["flexible_amount"] == D("318.57")
        years = result["projection"]["years"]
        assert years[1]["flexible_paid_amount"] == D("350.427")
        assert years[1]["flexible_paid_today"] == D("318.57")
        assert years[-1]["ending_value_amount"] == D("121.003")
        assert result["final_today_amount"] >= D("100")


def test_zero_savings_and_end_of_working_year_contributions(app):
    with app.app_context():
        portfolio, _, _ = seed()
        zero, _ = solve(portfolio, withdrawal_start_age_years=51)
        saved, _ = solve(portfolio, withdrawal_start_age_years=51, annual_savings_amount=D("100"))
        assert zero["flexible_amount"] == D("390")
        assert saved["flexible_amount"] == D("440")
        assert [y["savings_added_amount"] for y in saved["projection"]["years"]] == [D("100"), D("0"), D("0")]
        assert saved["projection"]["years"][0]["spending_required_amount"] == 0


def test_savings_follow_starting_mix_with_no_same_year_return(app):
    with app.app_context():
        portfolio, account, _ = seed("0")
        _position(portfolio, account, name="Mixed", amount="1000", bucket="growth", roles={"equity":"0.5", "income":"0.5"})
        db.session.commit()
        result, basis = solve(portfolio, withdrawal_start_age_years=51, annual_savings_amount=D("100"), equity_return_decimal=D("0.1"))
        first = result["projection"]["years"][0]
        assert first["return_amount"] == D("50")
        assert first["ending_role_values"]["equity"] == D("600")
        assert first["ending_role_values"]["income"] == D("550")
        assert first["ending_value_amount"] == D("1150")


def test_core_failure_and_goal_failure_are_distinct(app):
    with app.app_context():
        portfolio, _, _ = seed("200")
        goal, _ = solve(portfolio)
        assert goal["status"] == "goal_shortfall" and goal["flexible_amount"] == 0
        assert goal["final_today_amount"] == D("20")
        failure, _ = solve(portfolio, core_amount=D("80"))
        assert failure["status"] == "core_shortfall" and failure["flexible_amount"] == 0
        assert failure["first_core_shortfall"]["age"] == 52
        assert failure["shortfall_cause"] == "resources"


def test_restricted_money_is_access_gap_and_projects_are_excluded(app):
    with app.app_context():
        portfolio, account, _ = seed()
        account.present_access_decimal = D("0")
        account.earliest_access_date = date(2027,8,30)
        _position(portfolio, account, name="Project", amount="9999", bucket="projects", roles={"equity":"1"})
        db.session.commit()
        result, basis = solve(portfolio)
        assert result["status"] == "core_shortfall"
        assert result["shortfall_cause"] == "access"
        assert basis["projects_included_amount"] == D("9999")


def form_data(**changes):
    values = inputs(**changes)
    result = {}
    for key in ['base_date','currency_code','current_age_years','withdrawal_start_age_years','final_age_years','core_amount','annual_savings_amount','terminal_legacy_target_amount','inflation_decimal','equity_return_decimal','income_return_decimal','liquidity_return_decimal','alternatives_return_decimal']:
        value = values[key]
        if key.endswith('_decimal'):
            key = key.removesuffix('_decimal') + '_percent'
            value *= 100
        result[key] = value.isoformat() if isinstance(value,date) else str(value)
    return result


def review_token(response):
    import re
    return re.search(rb'name="review" value="([^"]+)"', response.data)[1].decode()


def test_projection_review_save_and_print_without_javascript(app, client):
    from app.services.retirement_plans import adopted_plan
    from app.services.spending import build_spending_value
    with app.app_context():
        portfolio, _, _ = seed()
        portfolio_id = portfolio.id
    response = client.post('/retirement', data=form_data())
    assert response.status_code == 200
    assert b'240.00/year Flexible' in response.data
    assert b'<table' not in response.data
    token = review_token(response)
    with app.app_context():
        assert adopted_plan(portfolio_id).planning_mode == 'budget'
        assert adopted_plan(portfolio_id).flexible_amount == D('40')
    report = client.get('/retirement/details', query_string={'review':token})
    assert report.status_code == 200
    assert b'Chart data' in report.data
    assert b'100.00' in report.data
    saved = client.post('/retirement', data={'review':token,'save_plan':'1'})
    assert saved.status_code == 302
    with app.app_context():
        plan = adopted_plan(portfolio_id)
        assert plan.planning_mode == 'affordability'
        assert plan.flexible_amount == D('240')
        assert plan.legacy_value_basis == 'today'
    app.config['WTF_CSRF_ENABLED'] = True
    refreshed = client.get(saved.location)
    assert refreshed.status_code == 200
    assert refreshed.data.count(b'data-chart="affordability"') == 2
    assert b'There is a problem' not in refreshed.data


def test_nominal_goal_requires_explicit_today_amount_without_transition_copy(app, client):
    import re
    from app.services.retirement_plans import adopted_plan
    with app.app_context():
        portfolio, _, _ = seed(terminal_legacy_target_amount=D('100'), terminal_legacy_target_currency_code='USD')
        pid = portfolio.id
    response = client.get('/retirement')
    goal_input = re.search(rb'<input[^>]*name="terminal_legacy_target_amount"[^>]*>', response.data)[0]
    assert b'value=""' in goal_input
    assert b'previous plan' not in response.data.lower()
    assert b'Previous budget tools' not in response.data
    assert b'data-chart="affordability"' not in response.data
    with app.app_context():
        plan = adopted_plan(pid)
        assert plan.terminal_legacy_target_amount == D('100')
        assert plan.legacy_value_basis == 'nominal'


def test_invalid_savings_and_signed_review(app,client):
    with app.app_context(): seed()
    for value in ['','-1','NaN','Infinity']:
        data=form_data(); data['annual_savings_amount']=value
        response=client.post('/retirement',data=data)
        assert b'aria-invalid="true"' in response.data
        assert b'data-chart="affordability"' not in response.data
    assert client.post('/retirement',data={'review':'fake','save_plan':'1'}).status_code==400
    assert client.get('/retirement/details?review=fake').status_code==400
    app.config['WTF_CSRF_ENABLED']=True
    assert client.post('/retirement',data=form_data()).status_code==400


def test_changed_portfolio_invalidates_review_and_details(app,client):
    from app.models import RetirementPlan
    from sqlalchemy import select
    with app.app_context(): seed()
    token=review_token(client.post('/retirement',data=form_data()))
    with app.app_context():
        plan=db.session.scalar(select(RetirementPlan))
        from app.services.retirement_plans import save_income
        from test_two_tier_retirement import income_data
        save_income(plan.portfolio_id,income_data())
        db.session.commit()
    assert client.get('/retirement/details',query_string={'review':token}).status_code==409
    response=client.post('/retirement',data={'review':token,'save_plan':'1'})
    assert b'changed since this review' in response.data
    with app.app_context(): assert db.session.scalar(select(RetirementPlan)).planning_mode=='budget'


def test_new_plan_backup_and_frozen_savings_path_roundtrip(app):
    from app.services.backup import export_backup, validate_backup, restore_backup
    from app.services.retirement_plans import save_plan, adopted_plan
    from app.services.retirement_affordability import validate_affordability
    from app.services.retirement_scenarios import save_scenario
    from app.models import Portfolio, RetirementScenario
    from sqlalchemy import select
    with app.app_context():
        portfolio, _, _ = seed()
        data=inputs(withdrawal_start_age_years=51,annual_savings_amount=D('100'))
        basis,_=prepare_affordability(portfolio,data)
        result=solve_affordability(basis)
        save_plan(portfolio.id,{**validate_affordability(data),'flexible_amount':result['flexible_amount']},confirmed=True)
        save_scenario(portfolio,AS_OF,name='Savings path',return_path='0,0,0,0')
        db.session.commit()
        restore_backup(validate_backup(export_backup()))
        portfolio=db.session.scalar(select(Portfolio))
        assert adopted_plan(portfolio.id).annual_savings_amount==D('100')
        assert adopted_plan(portfolio.id).legacy_value_basis=='today'
        assert db.session.scalar(select(RetirementScenario)).name=='Savings path'
        assert solve(portfolio,withdrawal_start_age_years=51,annual_savings_amount=D('100'))[0]['flexible_amount']==result['flexible_amount']


def test_v8_backup_retains_nominal_goal(app):
    import json
    from app.services.backup import export_backup, validate_backup, restore_backup
    from app.services.retirement_plans import adopted_plan
    with app.app_context():
        portfolio,_,_=seed(terminal_legacy_target_amount=D('123'),terminal_legacy_target_currency_code='USD')
        pid=portfolio.id
        data=json.loads(export_backup());data['version']=8
        data['tables'].pop('decimal_conversions', None)
        data['tables'].pop('fx_reference_sets')
        for row in data['tables']['retirement_plans']:
            for key in ('planning_mode','legacy_value_basis','annual_savings_amount'): row.pop(key)
        restore_backup(validate_backup(json.dumps(data).encode()))
        plan=adopted_plan(pid)
        assert plan.legacy_value_basis=='nominal'
        assert plan.terminal_legacy_target_amount==D('123')
        assert plan.annual_savings_amount==0


def test_savings_follow_initial_buckets_and_do_not_rebalance(app):
    with app.app_context():
        portfolio,account,_=seed('500')
        _position(portfolio,account,name='Equity',amount='500',bucket='bridge',roles={'equity':'1'})
        db.session.commit()
        result,_=solve(portfolio,withdrawal_start_age_years=52,final_age_years=53,
                     annual_savings_amount=D('100'),equity_return_decimal=D('.1'))
        first,second=result['projection']['years'][:2]
        assert first['ending_bucket_values']['cash']==D('550')
        assert first['ending_bucket_values']['bridge']==D('600')
        assert second['ending_bucket_values']['cash']==D('600')
        assert second['ending_bucket_values']['bridge']==D('710')
        assert first['savings_added_amount']==second['savings_added_amount']==D('100')


def test_review_one_year_later_rebases_money_and_age_without_adopting(app,client):
    from app.services.retirement_plans import save_plan, adopted_plan
    from app.services.retirement_affordability import validate_affordability
    with app.app_context():
        portfolio,_,_=seed()
        pid=portfolio.id
        data=inputs(inflation_decimal=D('.025'),final_age_years=90)
        values=validate_affordability(data);values['flexible_amount']=D('10')
        save_plan(pid,values,confirmed=True);db.session.commit()
    response=client.get('/retirement?as_of=2027-08-30')
    assert b'data-chart="affordability"' in response.data
    assert b'Age now 51' in response.data
    assert b'USD 61.50' in response.data
    assert b'USD 102.50' in response.data
    with app.app_context():
        plan=adopted_plan(pid)
        assert plan.base_date==AS_OF
        assert plan.core_amount==D('60')
        assert plan.flexible_amount==D('10')


def test_no_starting_mix_requires_sources_only_for_positive_savings(app):
    with app.app_context():
        portfolio,_,_=seed('0')
        assert solve(portfolio,withdrawal_start_age_years=51,annual_savings_amount=D('100'))[0]['status']=='missing'
        assert solve(portfolio,withdrawal_start_age_years=51,annual_savings_amount=D('0'))[0]['status']=='core_shortfall'



def test_spending_chart_is_nominal_while_allowance_and_capital_stay_real(app, client):
    import json
    import re
    from html import unescape
    from app.retirement import _affordability_chart
    with app.app_context():
        portfolio, _, _ = seed()
        result, _ = solve(portfolio, final_age_years=52, core_amount=D('100'), inflation_decimal=D('.1'))
        chart = _affordability_chart(result)
        assert result['flexible_amount'] == D('318.57')
        assert chart['core'] == [100.0, 110.0]
        assert chart['flexible'] == [318.57, 350.43]
        assert chart['capital'][-1] == 100.0
        assert chart['capital_nominal'] == [581.43, 121.0]
        assert chart['total'] == [418.57, 460.43]
        assert chart['today'] == [418.57, 418.57]
    response = client.post('/retirement', data=form_data(final_age_years=52, core_amount=D('100'), inflation_decimal=D('.1')))
    html = response.get_data(as_text=True)
    assert json.loads(unescape(re.search(r'data-core="([^"]+)"', html)[1])) == chart['core']
    assert json.loads(unescape(re.search(r'data-flexible="([^"]+)"', html)[1])) == chart['flexible']
    assert 'nominal, including inflation' in html
    assert json.loads(unescape(re.search(r'data-today="([^"]+)"', html)[1])) == chart['today']
    assert 'Equivalent in today' in html
    assert json.loads(unescape(re.search(r'data-nominal="([^"]+)"', html)[1])) == chart['capital_nominal']
    assert 'Your goal at age 52: <strong>USD 100.00 in today' in html
    assert 'USD 121.00 in that year' in html
    assert 'using your 10.00% inflation assumption' in html
    assert 'id="capital-year-label">Capital at age 52' in html
    report = client.get('/retirement/details', query_string={'review': review_token(response)}).get_data(as_text=True)
    assert 'Core paid (nominal)' in report
    assert 'Ending capital (today’s money)' in report
    assert 'Ending capital (nominal)' in report
    assert 'Money left at age 52, nominal USD</dt><dd>121.00</dd>' in report
    assert '<td class="num">350.43</td>' in report



def test_allowance_copy_distinguishes_withdrawals_from_planned_income(app, client):
    from app.services.retirement_plans import save_income
    from test_two_tier_retirement import income_data
    with app.app_context():
        portfolio, _, _ = seed()
        pid = portfolio.id
    response = client.post('/retirement', data=form_data())
    assert b'you could withdraw' in response.data
    assert b'Before any taxes on portfolio withdrawals' in response.data
    assert b'you could spend' not in response.data
    with app.app_context():
        save_income(pid, income_data())
        db.session.commit()
    response = client.post('/retirement', data=form_data())
    assert b'you could draw from your portfolio and planned income' in response.data
    assert b'Before any taxes on portfolio withdrawals' in response.data



def test_reference_line_uses_paid_amounts_in_shortfall_and_zero_while_working(app):
    from app.retirement import _affordability_chart
    with app.app_context():
        portfolio, _, _ = seed('100')
        result, _ = solve(portfolio, core_amount=D('60'), inflation_decimal=D('.1'))
        assert result['status'] == 'core_shortfall'
        rows = result['projection']['years']
        assert rows[1]['total_paid_amount'] == D('40')
        assert rows[1]['total_paid_today'] == D('40') / D('1.1')
        assert _affordability_chart(result)['today'] == [60.0, 36.36, 0.0]
        working, _ = solve(portfolio, withdrawal_start_age_years=51)
        assert _affordability_chart(working)['initial_index'] == 1
        assert working['projection']['years'][0]['total_paid_today'] == 0
