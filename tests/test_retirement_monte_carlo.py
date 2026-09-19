"""Exact Monte Carlo cash-flow, sampling, comparison and review-integrity checks."""
from copy import deepcopy
from datetime import date
from decimal import Decimal as D, localcontext
from html import unescape
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest

from app.extensions import db
from app.services.retirement import _consume, _pool_total, _rebalance, run_generated_projection, run_retirement_projection
from app.services.retirement_affordability import prepare_affordability, solve_affordability
from app.services.retirement_monte_carlo import (
    ROLES, CONTEXT, MODEL_VERSION, MonteCarloValidationError, allocation_weights, _log_volatilities,
    prepare_experiment, return_path, compare_allocations, comparison_steps, inspect_path, _percentile,
)
from test_retirement_affordability import inputs, form_data, review_token
from test_two_tier_retirement import seed, income_data
from test_m5_retirement import _position


def mix(*values):
    return dict(zip(ROLES, map(D, values)))


ZERO = mix('0', '0', '0', '0')
CASH = mix('0', '0', '1', '0')
EQUITY = mix('1', '0', '0', '0')


def pool(name, roles, access=50):
    return dict(source_key=name, source_name=name, source_kind='holding',
                bucket_code='growth', equity_exposed=bool(roles.get('equity')),
                available_from_age=access, access_date=None, roles=roles.copy())


def experiment_for(portfolio, flexible='0', **changes):
    basis, summary = prepare_affordability(portfolio, inputs(**changes))
    return prepare_experiment(basis, summary, D(flexible)), basis, summary


def test_proportional_withdrawal_mixed_sources_and_locked_capital():
    pools = [pool('mixed', mix('60','40','0','0')), pool('cash', mix('0','0','100','0')),
             pool('locked', mix('500','0','0','0'), 51)]
    paid, shortfall, applications = _consume(pools, D('50'), 50, proportional=True)
    assert (paid, shortfall) == (D('50'), D('0'))
    assert [row['amount'] for row in applications] == [D('25'), D('25')]
    assert pools[0]['roles'] == mix('45','30','0','0')
    assert _pool_total(pools[1]) == 75
    assert _pool_total(pools[2]) == 500
    paid, gap, _ = _consume(pools, D('200'), 50, proportional=True)
    assert (paid, gap) == (D('150'), D('50'))
    assert _pool_total(pools[0]) == _pool_total(pools[1]) == 0


def test_rebalance_exact_deficits_source_conservation_and_unlock():
    pools = [pool('locked', mix('70','0','0','0'), 51), pool('a', mix('0','0','10','0')),
             pool('b', mix('0','0','20','0'))]
    target = mix('.5','.25','0','.25')
    state = _rebalance(pools, 50, target)
    assert state['pre_return_role_values'] == mix('70','15','0','15')
    assert [_pool_total(p) for p in pools] == [D('70'), D('10'), D('20')]
    assert pools[1]['roles'] == mix('0','5','0','5')
    assert _rebalance(pools, 51, target)['pre_return_role_values'] == mix('50','25','0','25')
    assert [_pool_total(p) for p in pools] == [D('70'), D('10'), D('20')]
    assert _rebalance([], 50, target)['achieved_weights'] == dict.fromkeys(ROLES)


def test_fractional_rebalance_preserves_each_source_total():
    from random import Random
    rng = Random(7)
    for _ in range(200):
        pools = [pool(str(n), dict(equity=D(rng.randrange(1,999))/7, income=D(rng.randrange(1,999))/13,
                                  liquidity=D('0'), alternatives=D('0'))) for n in range(5)]
        before = [_pool_total(p) for p in pools]
        _rebalance(pools, 50, mix('.31','.29','.17','.23'))
        assert before == [_pool_total(p) for p in pools]
        assert all(v >= 0 for p in pools for v in p['roles'].values())
        full_amount = sum((_pool_total(p) for p in pools), D(0))
        paid, shortfall, _ = _consume(pools, full_amount, 50, proportional=True)
        assert paid == full_amount
        assert shortfall == 0
        assert all(_pool_total(p) == 0 for p in pools)


def test_fd_mapping_maturity_access_and_projects_do_not_mutate_sources(app):
    with app.app_context():
        portfolio, account, _ = seed('0')
        account.present_access_decimal = D('.5')
        account.earliest_access_date = date(2028, 8, 30)
        _position(portfolio, account, name='FD in Income', amount='1000', bucket='bridge', roles={'income':'1'}, maturity=date(2027, 1, 1))
        _position(portfolio, account, name='Project', amount='9000', bucket='projects', roles={'equity':'1'})
        db.session.commit()
        experiment, basis, summary = experiment_for(portfolio, income_return_decimal=D('.5'), liquidity_return_decimal=D('.02'))
        assert experiment['recorded_percentages']['income'] == 100
        assert experiment['current_percentages']['liquidity'] == 100
        assert [p['available_from_age'] for p in experiment['basis']['_inputs']['pools']] == [51,52]
        assert experiment['starting_assets'] == 1000
        assert experiment['locked_amount'] == 1000
        assert all(p['roles'].get('income') == 500 for p in basis['_inputs']['pools'])
        projection = run_generated_projection(experiment['basis'], [mix('0','.5','.02','0')]*3, CASH)
        first = projection['years'][0]
        assert first['ending_value_amount'] == D('1020')
        assert first['core_shortfall_amount'] == 60
        assert first['rebalance']['locked_amount'] == 1000
        assert basis['projects_included_amount'] == 9000
        assert summary['holdings'][0]['role_weights'][0]['code'] == 'income'


def test_generated_upper_tail_manual_bound_and_input_immutability(app):
    with app.app_context():
        portfolio, _, _ = seed('100')
        experiment, _, _ = experiment_for(portfolio, core_amount=D('0'), final_age_years=51)
        original = deepcopy(experiment)
        result = run_generated_projection(experiment['basis'], [mix('2','0','0','0')], EQUITY)
        assert result['final_value_amount'] == 300
        with pytest.raises(ValueError):
            run_retirement_projection(experiment['basis'], [mix('2','0','0','0')])
        for invalid in (mix('-1.01','0','0','0'), mix('NaN','0','0','0')):
            with pytest.raises(ValueError):
                run_generated_projection(experiment['basis'], [invalid], EQUITY)
        assert experiment == original


def test_sequence_order_with_known_cash_flows_and_full_compact_agreement(app):
    with app.app_context():
        portfolio, _, _ = seed('100')
        experiment, _, _ = experiment_for(portfolio, core_amount=D('20'), final_age_years=52, terminal_legacy_target_amount=D('0'))
        up = mix('.5','0','0','0'); down = mix('-.5','0','0','0')
        early = run_generated_projection(experiment['basis'], [up,down], EQUITY)
        late = run_generated_projection(experiment['basis'], [down,up], EQUITY)
        assert early['final_value_amount'] == D('50')
        assert late['final_value_amount'] == D('30')
        compact = run_generated_projection(experiment['basis'], [down,up], EQUITY, compact=True)
        for full, small in zip(late['years'], compact['years']):
            assert all(full[key] == value for key, value in small.items())


def test_savings_income_and_obligation_timing(app):
    from app.services.retirement_plans import save_income
    with app.app_context():
        portfolio, account, _ = seed('100')
        experiment, _, _ = experiment_for(portfolio, withdrawal_start_age_years=51,
                                           final_age_years=52, annual_savings_amount=D('10'), core_amount=D('20'))
        run = run_generated_projection(experiment['basis'], [mix('.1','0','0','0')]*2, EQUITY)
        assert [r['ending_value_amount'] for r in run['years']] == [D('120'),D('110')]
        assert [r['savings_added_amount'] for r in run['years']] == [D('10'), D('0')]
        assert run['years'][0]['source_balances'][-1]['available_from_age'] == 51
        # Accessible income settles debt first; surplus is invested before returns.
        save_income(portfolio.id, income_data(annual_amount=D('200')))
        db.session.commit()
        experiment, _, _ = experiment_for(portfolio, final_age_years=51, core_amount=D('20'))
        experiment['basis']['opening_negative_cash_amount'] = D('150')
        run = run_generated_projection(experiment['basis'], [mix('.1','0','0','0')], EQUITY)
        row = run['years'][0]
        assert row['income_to_obligation_amount'] == 50
        assert row['income_to_core_amount'] == 20
        assert row['income_saved_amount'] == 130
        assert row['ending_value_amount'] == 143


def test_zero_volatility_and_identical_mixes_are_exact(app):
    with app.app_context():
        portfolio, _, _ = seed()
        experiment, _, _ = experiment_for(portfolio, flexible='100', core_amount=D('60'))
        returns = return_path(experiment['basis']['role_returns'], 3, 1, volatilities=ZERO)
        assert returns == (ZERO, ZERO, ZERO)
        projection = run_generated_projection(experiment['basis'], returns, CASH)
        assert [r['ending_value_amount'] for r in projection['years']] == [D('840'),D('680'),D('520')]
        result = compare_allocations(experiment, mix('0','0','100','0'), path_count=12)
        assert result['allocations'][0]['rows'] == result['allocations'][1]['rows']
        assert result['allocations'][0]['outcomes'] == result['allocations'][1]['outcomes']
        assert result['allocations'][0]['rows'][-1]['real']['median'] == 520


def test_lifestyle_core_and_legacy_outcomes_remain_distinct(app):
    with app.app_context():
        portfolio, _, _ = seed('100')
        experiment, _, _ = experiment_for(portfolio, flexible='60', core_amount=D('20'), final_age_years=52, terminal_legacy_target_amount=D('50'))
        result = compare_allocations(experiment, mix('0','0','100','0'), path_count=4)
        assert {k:v['count'] for k,v in result['allocations'][0]['outcomes'].items()} == dict(lifestyle=0,core_shortfall=0,lifestyle_and_goal=0)
        experiment, _, _ = experiment_for(portfolio, core_amount=D('20'), final_age_years=52, terminal_legacy_target_amount=D('90'))
        result = compare_allocations(experiment, mix('0','0','100','0'), path_count=4)
        assert {k:v['count'] for k,v in result['allocations'][0]['outcomes'].items()} == dict(lifestyle=4,core_shortfall=0,lifestyle_and_goal=0)


def test_fixed_inflation_uses_payment_and_closing_boundaries(app):
    with app.app_context():
        portfolio, _, _ = seed('1000')
        experiment, _, _ = experiment_for(portfolio, flexible='100', core_amount=D('100'),
                                          inflation_decimal=D('.1'), final_age_years=52)
        result = compare_allocations(experiment, mix('0','0','100','0'), path_count=4)
        rows = result['allocations'][0]['rows']
        assert rows[0]['nominal']['median'] == 800
        assert rows[1]['nominal']['median'] == 580
        assert rows[1]['real']['median'] == D('580') / D('1.21')
        assert result['legacy_real'] == 100
        assert result['legacy_nominal'] == 121


def test_run_failure_withholds_frequencies_and_does_not_cache_partial_result(app, client, monkeypatch):
    with app.app_context():
        seed()
    _, _, fields = _entry(client)
    response = client.post('/retirement/monte-carlo', data=fields)
    def fail(*args, **kwargs):
        raise ArithmeticError('Synthetic failure')
    monkeypatch.setattr('app.monte_carlo.comparison_steps', fail)
    result = client.get(response.location)
    assert result.status_code == 422
    assert b'No outcome frequencies are available' in result.data
    assert not app.extensions['monte_carlo_summaries'][0]


def test_generator_is_reproducible_prefix_stable_and_context_independent():
    growth = mix('.05','.02','.01','.02')
    first = return_path(growth, 5, 27)
    assert first == return_path(growth, 5, 27)
    assert first == return_path(growth, 9, 27)[:5]
    assert first != return_path(growth, 5, 28)
    assert all(row['liquidity'] == D('.01') for row in first)
    with localcontext() as context:
        context.prec = 8
        assert first == return_path(growth, 5, 27)
    # Pin the actual generator, not merely two invocations of the same code.
    assert first[0]['equity'] == D('0.037330988875378063050301825')
    assert first[0]['income'] == D('-0.0223703244791602359534416376')
    assert first[0]['alternatives'] == D('-0.0691823833405466456330363101')


@pytest.mark.parametrize('equity_growth', ['-.99', '-.2', '0', '.062', '.2', '1'])
def test_equity_calibration_matches_annual_variance_at_owner_growth(equity_growth):
    growth = mix(equity_growth, '.02', '.01', '.02')
    before = growth.copy()
    with localcontext() as context:
        context.prec = 60
        sigma = _log_volatilities(growth)['equity']
        mu = (1 + growth['equity']).ln()
        # Independent lognormal moment equations at higher precision.
        first_moment = (mu + sigma*sigma/2).exp()
        second_moment = (2*mu + 2*sigma*sigma).exp()
        assert abs(second_moment - first_moment**2 - D('.163')**2) < D('1e-25')
    assert growth == before


def test_calibration_preserves_draws_other_assets_and_growth_centre():
    growth = mix('.062', '.02', '.01', '.02')
    old_vol = mix('.17', '.06', '0', '.16')
    previous = return_path(growth, 50, 27, volatilities=old_vol)
    calibrated = return_path(growth, 50, 27)
    log_vol = _log_volatilities(growth)['equity']
    centre = (1 + growth['equity']).ln()
    for old, new in zip(previous, calibrated):
        assert all(old[role] == new[role] for role in ('income', 'liquidity', 'alternatives'))
        old_shock = ((1 + old['equity']).ln() - centre) / D('.17')
        new_shock = ((1 + new['equity']).ln() - centre) / log_vol
        assert abs(old_shock - new_shock) < D('1e-24')


def test_progress_reports_completed_pairs_and_same_exact_result(app):
    with app.app_context():
        portfolio, _, _ = seed('100')
        experiment, _, _ = experiment_for(portfolio, final_age_years=52)
        alternative = mix('50','30','10','10')
        updates = list(comparison_steps(experiment, alternative, path_count=51))
        assert [u['completed'] for u in updates[:-1]] == [0, 25, 50, 51]
        assert all(u['total'] == 51 for u in updates[:-1])
        assert updates[-1]['result'] == compare_allocations(experiment, alternative, path_count=51)


def test_stream_delivers_progress_before_completion_then_cached_link(app, client, monkeypatch):
    with app.app_context():
        seed()
    _, _, fields = _entry(client)
    def short_run(experiment, percentages):
        yield from comparison_steps(experiment, percentages, path_count=26)
    monkeypatch.setattr('app.monte_carlo.comparison_steps', short_run)
    response = client.post('/retirement/monte-carlo', data=fields,
                           headers={'X-Monte-Carlo-Progress': '1'}, buffered=False)
    assert response.mimetype == 'application/x-ndjson'
    assert response.headers['Cache-Control'] == 'no-store'
    chunks = iter(response.response)
    assert json.loads(next(chunks)) == {'completed': 0, 'total': 26}
    assert not app.extensions['monte_carlo_summaries'][0]
    updates = [json.loads(chunk) for chunk in chunks]
    assert [u['completed'] for u in updates[:-1]] == [25, 26]
    def fail(*args, **kwargs):
        raise AssertionError('Completed result must come from cache')
    monkeypatch.setattr('app.monte_carlo.comparison_steps', fail)
    assert client.get(updates[-1]['url']).status_code == 200


def test_stream_failure_and_validation_retain_recovery(app, client, monkeypatch):
    with app.app_context():
        seed()
    _, _, fields = _entry(client)
    headers = {'X-Monte-Carlo-Progress': '1'}
    assert client.post('/retirement/monte-carlo', data={**fields, 'equity_percent': '50'}, headers=headers).status_code == 422
    def fail(*args, **kwargs):
        yield {'completed': 0, 'total': 1000}
        raise ArithmeticError('Synthetic failure')
    monkeypatch.setattr('app.monte_carlo.comparison_steps', fail)
    response = client.post('/retirement/monte-carlo', data=fields, headers=headers)
    events = [json.loads(line) for line in response.data.splitlines()]
    assert 'No new results' in events[-1]['error']
    assert not app.extensions['monte_carlo_summaries'][0]
    app.config['WTF_CSRF_ENABLED'] = True
    assert client.post('/retirement/monte-carlo', data=fields, headers=headers).status_code == 400


def test_percentiles_include_failed_paths_and_interpolate_in_decimal():
    assert _percentile([D('0'),D('0'),D('100'),D('200')], D('.5')) == 50
    assert _percentile([D('0'),D('0'),D('100'),D('200')], D('.9')) == 170


def test_generated_sample_matches_log_centres_risk_scale_and_correlation():
    # Fixed-draw statistical guard against a broken transform or covariance mapping.
    growth = mix('.05','.02','.01','.02')
    observations = [return_path(growth, 1, n)[0] for n in range(1,4097)]
    logs = {role: [(1+row[role]).ln() for row in observations] for role in ('equity','income','alternatives')}
    means = {role: sum(values, D(0))/len(values) for role,values in logs.items()}
    centred = {role: [value-means[role] for value in values] for role,values in logs.items()}
    variances = {role: sum((v*v for v in values),D(0))/len(values) for role,values in centred.items()}
    for role, volatility in _log_volatilities(growth).items():
        if role == 'liquidity':
            continue
        assert abs(means[role]-(1+growth[role]).ln()) < D('.007')
        assert abs(variances[role].sqrt()-volatility) < D('.007')
    equity_mean = sum((row['equity'] for row in observations), D(0)) / len(observations)
    equity_variance = sum(((row['equity']-equity_mean)**2 for row in observations), D(0)) / len(observations)
    assert abs(equity_variance.sqrt() - D('.163')) < D('.007')
    def correlation(a,b):
        return sum((x*y for x,y in zip(centred[a],centred[b])),D(0))/len(observations)/(variances[a]*variances[b]).sqrt()
    assert D('.30') < correlation('equity','income') < D('.40')
    assert abs(correlation('equity','alternatives')) < D('.05')
    assert abs(correlation('income','alternatives')) < D('.05')


@pytest.mark.parametrize('values', [mix('50','25','25','1'), mix('NaN','0','0','0'), mix('101','-1','0','0')])
def test_invalid_allocation_rejected(values):
    with pytest.raises(MonteCarloValidationError):
        allocation_weights(values)


def _entry(client, **changes):
    response = client.post('/retirement', data=form_data(**changes))
    token = review_token(response)
    page = client.get('/retirement/monte-carlo', query_string={'review': token})
    fields = dict((name, unescape(value)) for name, value in re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', page.get_data(as_text=True)))
    return token, page, fields


def test_recurring_allocation_defaults_can_be_submitted_unchanged(app, client):
    with app.app_context():
        portfolio, account, _ = seed('1000')
        _position(portfolio, account, name='Shares', amount='1000', bucket='growth', roles={'equity': '1'})
        _position(portfolio, account, name='Bonds', amount='1000', bucket='bridge', roles={'income': '1'})
        db.session.commit()
    _, page, fields = _entry(client)
    assert page.status_code == 200
    values = [D(fields[role + '_percent']) for role in ROLES]
    assert sorted(values) == [D('0'), D('33.333333'), D('33.333333'), D('33.333334')]
    assert sum(values) == D('100')
    assert client.post('/retirement/monte-carlo', data=fields).status_code == 302
    invalid = client.post('/retirement/monte-carlo', data={**fields, 'equity_percent': '0'})
    assert invalid.status_code == 422
    assert b'id="equity_percent-error"' in invalid.data


def test_routes_unsaved_review_prg_chart_report_and_no_database_writes(app, client):
    from app.services.backup import export_backup
    with app.app_context():
        seed()
        before = json.loads(export_backup())['tables']
    token, page, fields = _entry(client)
    assert page.status_code == 200
    assert b'Current model' in page.data
    assert b'data-path-count="1000"' in page.data
    assert b'16.30%' in page.data
    assert b'illustrative broad-market preset' in page.data
    response = client.post('/retirement/monte-carlo', data=fields)
    assert response.status_code == 302
    result = client.get(response.location)
    assert result.status_code == 200
    html = result.get_data(as_text=True)
    chart = json.loads(unescape(re.search(r'data-values="([^"]+)"', html)[1]))
    assert chart['allocations'][0]['median'] == chart['allocations'][1]['median']
    assert b'1,000 paths' in result.data
    run_token = parse_qs(urlparse(response.location).query)['run'][0]
    report = client.get('/retirement/monte-carlo/details', query_string={'run': run_token, 'path': 1000})
    assert report.status_code == 200
    assert b'Path 1000' in report.data
    assert b'Chart-equivalent annual capital ranges' in report.data
    assert b'FD maturity' in report.data
    assert b'16.30%' in report.data
    assert b'All compound-growth rates remain your inputs.' in report.data
    assert client.get('/retirement/monte-carlo/details', query_string={'run': run_token,'path':0}).status_code == 422
    assert client.get('/retirement/monte-carlo/details', query_string={'run': run_token,'path':1001}).status_code == 422
    assert client.get('/retirement/monte-carlo/details', query_string={'run':run_token,'path':'NaN'}).status_code == 422
    assert client.get('/retirement/monte-carlo', query_string={'run':run_token+'bad'}).status_code == 400
    resumed = client.get('/retirement', query_string={'review': token})
    assert b'240.00/year Flexible' in resumed.data
    with app.app_context():
        assert json.loads(export_backup())['tables'] == before
        from app.models import CashBalanceCheckpoint
        from sqlalchemy import select
        db.session.scalar(select(CashBalanceCheckpoint)).confirmed_balance_amount += D('1')
        db.session.commit()
    assert client.get(response.location).status_code == 409
    assert client.get('/retirement/monte-carlo/details',query_string={'run':run_token}).status_code == 409


@pytest.mark.parametrize('old_count', [None, 5000])
def test_changed_path_count_requires_new_run_without_reinterpreting_link(app, client, monkeypatch, old_count):
    from app.monte_carlo import _serializer
    with app.app_context():
        seed()
    _, _, fields = _entry(client)
    response = client.post('/retirement/monte-carlo', data=fields)
    token = parse_qs(urlparse(response.location).query)['run'][0]
    with app.app_context():
        payload = _serializer().loads(token)
        assert payload.pop('path_count') == 1000
        if old_count is not None:
            payload['path_count'] = old_count
        old_token = _serializer().dumps(payload)
    def fail(*args, **kwargs):
        raise AssertionError('An old run must not be recalculated with a different path count')
    monkeypatch.setattr('app.monte_carlo._run', fail)
    for route in ('/retirement/monte-carlo', '/retirement/monte-carlo/details'):
        result = client.get(route, query_string={'run': old_token})
        assert result.status_code == 400
        assert b'path count changed' in result.data


def test_previous_risk_model_link_requires_fresh_comparison(app, client, monkeypatch):
    from app.monte_carlo import _serializer
    with app.app_context():
        seed()
    _, _, fields = _entry(client)
    response = client.post('/retirement/monte-carlo', data=fields)
    token = parse_qs(urlparse(response.location).query)['run'][0]
    with app.app_context():
        payload = _serializer().loads(token)
        payload['version'] = 'usd-lognormal-v1-decimal28-polar-mt19937'
        old_token = _serializer().dumps(payload)
    def fail(*args, **kwargs):
        raise AssertionError('An old risk profile must not silently use the new calibration')
    monkeypatch.setattr('app.monte_carlo._run', fail)
    for route in ('/retirement/monte-carlo', '/retirement/monte-carlo/details'):
        result = client.get(route, query_string={'run': old_token})
        assert result.status_code == 400
        assert b'comparison model changed' in result.data


def test_invalid_inputs_and_changed_sources_never_return_cached_results(app, client):
    from app.models import CashBalanceCheckpoint
    from sqlalchemy import select
    with app.app_context():
        seed()
    token, _, fields = _entry(client)
    response = client.post('/retirement/monte-carlo', data={**fields,'equity_percent':'50'})
    assert response.status_code == 422
    assert b'href="#equity_percent"' in response.data
    assert b'aria-describedby="equity_percent-error"' in response.data
    for value in ('241','-1','NaN','Infinity'):
        assert client.post('/retirement/monte-carlo', data={**fields,'flexible_amount':value}).status_code == 422
    with app.app_context():
        cash = db.session.scalar(select(CashBalanceCheckpoint))
        cash.confirmed_balance_amount += D('1')
        db.session.commit()
    assert client.post('/retirement/monte-carlo', data=fields).status_code == 409
    assert client.get('/retirement/monte-carlo', query_string={'review':token}).status_code == 409


def test_csrf_and_invalid_reviews(app, client):
    with app.app_context():
        seed()
    _, _, fields = _entry(client)
    app.config['WTF_CSRF_ENABLED'] = True
    assert client.post('/retirement/monte-carlo', data=fields).status_code == 400
    assert client.get('/retirement/monte-carlo?review=broken').status_code == 400
    assert client.get('/retirement/monte-carlo/details').status_code == 400


def test_missing_zero_capital_currency_and_log_endpoint_explain_unavailability(app, client):
    with app.app_context():
        portfolio, _, _ = seed('0')
        basis, summary = prepare_affordability(portfolio, inputs())
        with pytest.raises(MonteCarloValidationError, match='positive'):
            prepare_experiment(basis, summary, D('0'))
        basis['reporting_currency'] = 'EUR'
        with pytest.raises(MonteCarloValidationError, match='USD'):
            prepare_experiment(basis, summary, D('0'))
        basis['reporting_currency'] = 'USD'
        basis['role_returns']['equity'] = D('-1')
        with pytest.raises(MonteCarloValidationError, match='greater than -100'):
            prepare_experiment(basis, summary, D('0'))
        basis['calculation_complete'] = False
        with pytest.raises(MonteCarloValidationError, match='missing'):
            prepare_experiment(basis, summary, D('0'))
    _, page, _ = _entry(client)
    assert page.status_code == 422
    assert b'positive non-Project' in page.data
