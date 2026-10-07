"""Real route failure paths for explicit Flexible targets on disposable data."""

import json
import re
from datetime import date
from decimal import Decimal
from html import unescape

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import CashBalanceCheckpoint, Portfolio, RetirementScenario
from app.services.backup import export_backup, restore_backup, validate_backup
from app.services.funding import build_funding_summary
from app.services.portfolio_summary import build_portfolio_summary
from app.services.retirement_affordability import validate_affordability
from app.services.retirement_plans import adopted_plan, save_plan
from app.services.retirement_scenarios import load_payload, save_scenario
from test_m5_retirement import AS_OF
from test_retirement_affordability import form_data, inputs, review_token
from test_two_tier_retirement import seed


def seed_target(**changes):
    portfolio, account, _ = seed()
    values = validate_affordability(inputs(**changes))
    values['flexible_amount'] = Decimal('40')
    save_plan(portfolio.id, values, confirmed=True)
    db.session.commit()
    return portfolio, account


def choose(client, token, amount, **changes):
    return client.post('/retirement', data={
        'review': token, 'flexible_amount': amount, 'review_lifestyle': '1', **changes,
    })


def chart(response, name):
    body = response.get_data(as_text=True)
    return json.loads(unescape(re.search(r'data-' + name + r'="([^"]+)"', body)[1]))


def fields(response):
    return dict(re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', unescape(response.get_data(as_text=True))))


def test_target_capacity_chart_report_save_and_reopen_agree(app, client):
    with app.app_context():
        portfolio, _ = seed_target()
        portfolio_id = portfolio.id
        before = json.loads(export_backup())['tables']
    page = client.get('/retirement?as_of=2026-08-30')
    assert fields(page)['flexible_amount'] == '40'
    assert chart(page, 'flexible') == [40, 40, 40]
    selected = choose(client, review_token(page), '120.01')
    assert selected.status_code == 200
    assert b'240.00/year' in selected.data
    assert chart(selected, 'flexible') == [120.01, 120.01, 120.01]
    assert chart(selected, 'capital')[-1] == 459.97
    token = review_token(selected)
    report = client.get('/retirement/details', query_string={'review': token})
    assert b'Planned Flexible' in report.data
    assert b'120.01' in report.data and b'459.97' in report.data
    with app.app_context():
        assert json.loads(export_backup())['tables'] == before
    saved = client.post('/retirement', data={'review': token, 'save_plan': '1'})
    assert saved.status_code == 302
    reopened = client.get(saved.location)
    assert fields(reopened)['flexible_amount'] == '120.01'
    assert chart(reopened, 'flexible') == [120.01, 120.01, 120.01]
    with app.app_context():
        assert adopted_plan(portfolio_id).flexible_amount == Decimal('120.01')


@pytest.mark.parametrize('amount,status', [('300', 'flexible_shortfall'), ('420', 'core_shortfall'), ('250', 'goal_shortfall')])
def test_unsupported_objective_requires_acknowledgement(app, client, amount, status):
    with app.app_context():
        portfolio, _ = seed_target()
        portfolio_id = portfolio.id
    selected = choose(client, review_token(client.get('/retirement?as_of=2026-08-30')), amount)
    assert ('data-lifestyle-status="' + status + '"').encode() in selected.data
    token = review_token(selected)
    failed = client.post('/retirement', data={'review': token, 'save_plan': '1'})
    assert b'href="#acknowledge_failure"' in failed.data
    with app.app_context():
        assert adopted_plan(portfolio_id).flexible_amount == Decimal('40')
    saved = client.post('/retirement', data={'review': review_token(failed), 'save_plan': '1', 'acknowledge_failure': 'y'})
    assert saved.status_code == 302
    reopened = client.get(saved.location)
    assert fields(reopened)['flexible_amount'] == amount
    assert ('data-lifestyle-status="' + status + '"').encode() in reopened.data


@pytest.mark.parametrize('amount', ['', '-1', 'NaN', 'Infinity', '120.001', '10000000000000000'])
def test_invalid_target_preserves_plan_and_links_error(app, client, amount):
    with app.app_context():
        seed_target()
        before = export_backup()
    page = client.get('/retirement?as_of=2026-08-30')
    result = choose(client, review_token(page), amount)
    assert b'href="#flexible_amount"' in result.data
    assert b'id="flexible_amount-error"' in result.data
    with app.app_context():
        assert json.loads(export_backup())['tables'] == json.loads(before)['tables']


def test_zero_target_and_exact_maximum_are_explicit_drafts(app, client):
    with app.app_context():
        portfolio, _ = seed_target()
        portfolio_id = portfolio.id
    page = client.get('/retirement?as_of=2026-08-30')
    zero = choose(client, review_token(page), '0')
    assert chart(zero, 'flexible') == [0, 0, 0]
    maximum = client.post('/retirement', data={'review': review_token(zero), 'use_maximum': '1'})
    assert fields(maximum)['flexible_amount'] == '240'
    assert chart(maximum, 'capital')[-1] == 100
    with app.app_context():
        assert adopted_plan(portfolio_id).flexible_amount == Decimal('40')


def test_capacity_drop_and_missing_sources_never_replace_saved_target(app, client):
    with app.app_context():
        portfolio, _ = seed_target()
        portfolio_id = portfolio.id
        db.session.scalar(select(CashBalanceCheckpoint)).confirmed_balance_amount = Decimal('200')
        db.session.commit()
    page = client.get('/retirement?as_of=2026-08-30')
    assert fields(page)['flexible_amount'] == '40'
    assert b'data-lifestyle-status="core_shortfall"' in page.data
    with app.app_context():
        db.session.delete(db.session.scalar(select(CashBalanceCheckpoint)))
        db.session.commit()
    missing = client.get('/retirement?as_of=2026-08-30')
    assert fields(missing)['flexible_amount'] == '40'
    assert b'data-lifestyle-status="missing"' in missing.data
    assert b'name="save_plan"' not in missing.data
    with app.app_context():
        assert adopted_plan(portfolio_id).flexible_amount == Decimal('40')


def test_assumption_changes_keep_target_and_currency_requires_explicit_choice(app, client):
    with app.app_context():
        seed_target()
    page = client.get('/retirement?as_of=2026-08-30')
    changed = client.post('/retirement', data={**form_data(core_amount=Decimal('80')), 'review': review_token(page), 'project': '1'})
    assert fields(changed)['flexible_amount'] == '40'
    currency = client.post('/retirement', data={**form_data(currency_code='EUR'), 'review': review_token(changed), 'project': '1'})
    assert fields(currency)['flexible_amount'] == ''
    assert b'currency changed' in currency.data
    assert b'name="save_plan"' not in currency.data


def test_later_date_rebases_saved_target_without_writing(app, client):
    with app.app_context():
        portfolio, _ = seed_target(inflation_decimal=Decimal('.025'), final_age_years=90)
        portfolio_id = portfolio.id
    page = client.get('/retirement?as_of=2027-08-30')
    assert fields(page)['flexible_amount'] == '41'
    with app.app_context():
        plan = adopted_plan(portfolio_id)
        assert plan.base_date == AS_OF and plan.flexible_amount == Decimal('40')


def test_stale_tabs_and_changed_sources_cannot_overwrite_newer_plan(app, client):
    with app.app_context():
        portfolio, _ = seed_target()
        portfolio_id = portfolio.id
    page = client.get('/retirement?as_of=2026-08-30')
    first = choose(client, review_token(page), '100')
    second = choose(client, review_token(page), '120')
    assert client.post('/retirement', data={'review': review_token(first), 'save_plan': '1'}).status_code == 302
    stale = client.post('/retirement', data={'review': review_token(second), 'save_plan': '1'})
    assert b'changed since this review' in stale.data
    with app.app_context():
        assert adopted_plan(portfolio_id).flexible_amount == Decimal('100')
        db.session.scalar(select(CashBalanceCheckpoint)).confirmed_balance_amount += Decimal('1')
        db.session.commit()
    changed = client.post('/retirement', data={'review': review_token(stale), 'save_plan': '1'})
    assert b'changed since this review' in changed.data
    with app.app_context():
        assert adopted_plan(portfolio_id).flexible_amount == Decimal('100')


def test_old_review_can_be_viewed_but_needs_fresh_review_to_save(app, client):
    from app.retirement import _affordability_review
    with app.app_context():
        seed_target()
    page = client.get('/retirement?as_of=2026-08-30')
    with app.app_context():
        payload = _affordability_review().loads(review_token(page))
        old = _affordability_review().dumps({key: payload[key] for key in ('fields', 'digest', 'portfolio_id')})
    viewed = client.get('/retirement', query_string={'review': old})
    assert viewed.status_code == 200
    rejected = client.post('/retirement', data={'review': old, 'save_plan': '1'})
    assert b'Review this lifestyle again' in rejected.data
    with app.app_context():
        assert adopted_plan(db.session.scalar(select(Portfolio)).id).flexible_amount == Decimal('40')


def test_monte_carlo_uses_draft_allows_above_capacity_and_never_adopts(app, client):
    with app.app_context():
        seed_target()
        before = json.loads(export_backup())['tables']
    selected = choose(client, review_token(client.get('/retirement?as_of=2026-08-30')), '120.01')
    token = review_token(selected)
    experiment = client.get('/retirement/monte-carlo', query_string={'review': token})
    assert fields(experiment)['flexible_amount'] == '120.01'
    run = client.post('/retirement/monte-carlo', data={**fields(experiment), 'flexible_amount': '300'})
    assert run.status_code == 302
    result = client.get(run.location)
    assert b'Annual Flexible tested:' in result.data and b'300.00' in result.data
    assert chart(client.get('/retirement', query_string={'review': token}), 'flexible') == [120.01, 120.01, 120.01]
    with app.app_context():
        assert json.loads(export_backup())['tables'] == before


def test_target_adoption_preserves_core_reserves_backup_and_frozen_comparison(app, client):
    with app.app_context():
        portfolio, _ = seed_target()
        portfolio_id = portfolio.id
        summary = build_portfolio_summary(portfolio, AS_OF, reporting_currency='USD')
        funding_before = build_funding_summary(portfolio, summary, AS_OF, reporting_currency='USD')
        save_scenario(portfolio, AS_OF, name='Original target', return_path='0,0,0,0')
        db.session.commit()
    selected = choose(client, review_token(client.get('/retirement?as_of=2026-08-30')), '120.01')
    assert client.post('/retirement', data={'review': review_token(selected), 'save_plan': '1'}).status_code == 302
    with app.app_context():
        portfolio = db.session.get(Portfolio, portfolio_id)
        summary = build_portfolio_summary(portfolio, AS_OF, reporting_currency='USD')
        after = build_funding_summary(portfolio, summary, AS_OF, reporting_currency='USD')
        assert after['assessment'] == funding_before['assessment']
        frozen = load_payload(db.session.scalar(select(RetirementScenario)).payload_json)
        assert frozen['basis']['plan']['flexible_amount'] == Decimal('40')
        backup = export_backup()
        restore_backup(validate_backup(backup))
        assert adopted_plan(portfolio_id).flexible_amount == Decimal('120.01')


def test_failed_save_rolls_back_and_csrf_is_required(app, client, monkeypatch):
    from app.services.retirement_plans import PlanValidationError
    with app.app_context():
        portfolio, _ = seed_target()
        portfolio_id = portfolio.id
    selected = choose(client, review_token(client.get('/retirement?as_of=2026-08-30')), '120')
    token = review_token(selected)
    original = db.session.commit
    def fail_commit():
        raise PlanValidationError('save_plan', 'Synthetic failed commit; nothing saved.')
    monkeypatch.setattr(db.session, 'commit', fail_commit)
    response = client.post('/retirement', data={'review': token, 'save_plan': '1'})
    assert b'Synthetic failed commit' in response.data
    monkeypatch.setattr(db.session, 'commit', original)
    with app.app_context():
        assert adopted_plan(portfolio_id).flexible_amount == Decimal('40')
    app.config['WTF_CSRF_ENABLED'] = True
    assert choose(client, token, '120').status_code == 400
    assert client.post('/retirement', data={'review': token, 'save_plan': '1'}).status_code == 400
