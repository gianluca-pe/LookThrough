"""Read-only exact allocation feedback exercised through its real endpoint."""

import json

import pytest

from app.services.backup import export_backup


def allocation_fields(values):
    return dict(zip(('equity_percent', 'income_percent', 'liquidity_percent', 'alternatives_percent'), values))


@pytest.mark.parametrize('values,state,total,difference', [
    (('50', '30', '10', '8'), 'under', '98', '2'),
    (('50', '30', '10', '13'), 'over', '103', '3'),
    (('50.1', '30.2', '10.3', '9.4'), 'complete', '100', '0'),
    (('33.333333', '33.333333', '33.333334', '0'), 'complete', '100', '0'),
    (('33.333333', '33.333333', '33.333333', '0'), 'under', '99.999999', '0.000001'),
    (('33.333334', '33.333334', '33.333333', '0'), 'over', '100.000001', '0.000001'),
    (('0', '0', '0', '0'), 'under', '0', '100'),
])
def test_exact_preview_without_portfolio_or_simulation(app, client, values, state, total, difference):
    with app.app_context():
        before = json.loads(export_backup())['tables']
    response = client.post('/retirement/monte-carlo/allocation-check', data=allocation_fields(values))
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    result = response.get_json()
    assert (result['state'], result['total'], result['difference']) == (state, total, difference)
    assert total + '%' in result['message']
    with app.app_context():
        assert json.loads(export_backup())['tables'] == before
        assert 'monte_carlo_summaries' not in app.extensions


@pytest.mark.parametrize('invalid', ['', '-1', 'NaN', 'Infinity', '101', '33.3333333', '1e999'])
def test_invalid_or_incomplete_fields_have_no_invented_total(client, invalid):
    response = client.post('/retirement/monte-carlo/allocation-check', data=allocation_fields((invalid, '30', '20', '0')))
    assert response.status_code == 200
    result = response.get_json()
    assert result['state'] == 'invalid'
    assert result['total'] is None
    assert 'equity_percent' in result['errors']


def test_feedback_does_not_exempt_csrf(app, client):
    app.config['WTF_CSRF_ENABLED'] = True
    response = client.post('/retirement/monte-carlo/allocation-check', data=allocation_fields(('50', '30', '20', '0')))
    assert response.status_code == 400
