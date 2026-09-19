"""Read-only Monte Carlo workspace entered from a signed retirement review."""
from decimal import Decimal, ROUND_HALF_UP
from collections import OrderedDict
from threading import Lock
import json

from flask import Blueprint, Response, abort, current_app, redirect, render_template, request, stream_with_context, url_for
from itsdangerous import BadData, URLSafeSerializer
from werkzeug.datastructures import MultiDict

from app.retirement import _affordability_review, _basis_digest
from app.retirement_forms import MonteCarloForm, RetirementAffordabilityForm
from app.services.retirement_affordability import prepare_affordability, solve_affordability
from app.services.retirement_plans import plan_incomes
from app.services.retirement_monte_carlo import (
    MODEL_VERSION, PATH_COUNT, ROLES, MonteCarloValidationError,
    comparison_steps, inspect_path, prepare_experiment,
)
from app.setup import _current_portfolio
from app.template_filters import money_amount

monte_carlo_blueprint = Blueprint('monte_carlo', __name__, url_prefix='/retirement/monte-carlo')


def _serializer():
    return URLSafeSerializer(current_app.secret_key, salt='retirement-monte-carlo')


def _load_review(token, portfolio):
    try:
        review = _affordability_review().loads(token)
    except BadData:
        abort(400, 'Open this experiment from a reviewed retirement projection.')
    if not isinstance(review, dict) or not isinstance(review.get('fields'), dict):
        abort(400)
    if review.get('portfolio_id') != portfolio.id:
        abort(404)
    form = RetirementAffordabilityForm(MultiDict(review['fields']), meta={'csrf': False})
    if not form.validate():
        abort(400)
    basis, summary = prepare_affordability(portfolio, form.values())
    if review.get('digest') != _basis_digest(basis):
        return None
    # Source metadata also matters for FD risk mapping and inspectable evidence.
    fields = ('registration_id', 'instrument_name', 'account_name', 'source_mode', 'native_amount',
              'native_currency', 'value_date', 'fx_date', 'fx_rate', 'status', 'fixed_deposit',
              'checkpoint_date', 'latest_cash_effect_date', 'classification_date', 'access_note')
    evidence = {kind: [{key: row.get(key) for key in fields} for row in summary[kind]]
                for kind in ('holdings', 'cash_balances')}
    evidence['income_assumptions'] = [
        {key: getattr(row, key) for key in ('name','annual_amount','currency_code','start_date','end_date','inflation_decimal')}
        for row in plan_incomes(basis['plan']['id'])
    ]
    digest = _basis_digest({'basis': basis, 'sources': evidence})
    solved = solve_affordability(basis)
    if solved['flexible_amount'] is None:
        raise MonteCarloValidationError('Review a complete retirement projection with a calculated Flexible allowance first.')
    return basis, summary, solved['flexible_amount'], digest, evidence['income_assumptions']


def _chart(result):
    def display(value):
        return money_amount(value).replace(',', '')
    charts = []
    for allocation in result['allocations']:
        rows = allocation['rows']
        charts.append({'label': allocation['label'], 'ages': [r['ending_age'] for r in rows],
                       **{q: [display(r['real'][q]) for r in rows] for q in ('p10', 'median', 'p90')},
                       'readouts': [{basis: {q: 'USD ' + money_amount(row[basis][q]) for q in ('p10', 'median', 'p90')}
                                    for basis in ('real', 'nominal')} for row in rows]})
    values = [Decimal(value) for chart in charts for key in ('p10', 'p90') for value in chart[key]]
    values += [result['legacy_real'], Decimal('0')]
    return {'allocations': charts, 'goal': display(result['legacy_real']),
            'minimum': display(min(values)), 'maximum': display(max(values) if max(values) > min(values) else min(values) + 1)}


def _run(experiment, percentages):
    for update in _run_steps(experiment, percentages):
        if 'result' in update:
            return update['result']


def _run_steps(experiment, percentages):
    # Keep at most four compact run summaries in this process so report/path
    # selection can reuse results without rerunning the full simulation.
    # Every route checks the reviewed source digest before consulting this cache.
    cache, lock = current_app.extensions.setdefault('monte_carlo_summaries', (OrderedDict(), Lock()))
    key = _basis_digest({'version': MODEL_VERSION, 'path_count': PATH_COUNT,
                         'basis': experiment['basis'], 'percentages': percentages})
    with lock:
        cached = cache.get(key)
        if cached is not None:
            cache.move_to_end(key)
    if cached is not None:
        yield {'result': cached}
        return
    for update in comparison_steps(experiment, percentages):
        if 'result' not in update:
            yield update
        else:
            result = update['result']
    with lock:
        cache[key] = result
        cache.move_to_end(key)
        while len(cache) > 4:
            cache.popitem(last=False)
    yield {'result': result}


def _stream_run(experiment, percentages, result_url):
    # The request itself runs the engine. No background worker or stored job.
    @stream_with_context
    def updates():
        try:
            for update in _run_steps(experiment, percentages):
                event = {'url': result_url} if 'result' in update else update
                yield json.dumps(event) + '\n'
        except (ArithmeticError, ValueError) as error:
            current_app.logger.error('Monte Carlo run failed: %s', type(error).__name__)
            yield json.dumps({'error': 'This comparison could not be calculated. No new results are available. Review the assumptions and try again.'}) + '\n'
    return Response(updates(), mimetype='application/x-ndjson', headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})


def _workspace(report=False):
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for('setup.show'))
    run_token = request.args.get('run', '')
    payload = None
    if run_token:
        try:
            payload = _serializer().loads(run_token)
        except BadData:
            abort(400, 'This comparison link is invalid. Run the comparison again.')
        if not isinstance(payload, dict) or payload.get('version') != MODEL_VERSION:
            abort(400, 'The comparison model changed. Run a new comparison.')
        if payload.get('path_count') != PATH_COUNT:
            abort(400, 'The comparison path count changed. Run a new comparison from Retirement.')
    if report and payload is None:
        abort(400, 'Open the report from a completed comparison.')
    review_token = request.form.get('review', '') if request.method == 'POST' else payload.get('review', '') if payload else request.args.get('review', '')
    base_context = dict(portfolio_name=portfolio.name, review_token=review_token, roles=ROLES, path_count=PATH_COUNT)
    try:
        loaded = _load_review(review_token, portfolio)
        if loaded is None:
            return render_template('retirement/changed.html', portfolio_name=portfolio.name), 409
        basis, summary, allowance, digest, income_assumptions = loaded
        experiment = prepare_experiment(basis, summary, allowance)
    except MonteCarloValidationError as error:
        return render_template('retirement/monte_carlo_unavailable.html', error=str(error), **base_context), 422
    if payload and payload.get('source_digest') != digest:
        return render_template('retirement/changed.html', portfolio_name=portfolio.name), 409
    # Editable alternatives obey the input precision contract. Balance the
    # rounding residual in the largest role; keep current model weights exact.
    defaults = {role: value.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)
                for role, value in experiment['current_percentages'].items()}
    largest = max(defaults, key=defaults.get)
    defaults[largest] += Decimal('100') - sum(defaults.values(), Decimal('0'))
    initial = {'review': review_token, 'source_digest': digest, 'flexible_amount': str(allowance),
               **{role + '_percent': str(value) for role, value in defaults.items()}}
    if payload:
        if not isinstance(payload.get('fields'), dict):
            abort(400)
        initial.update(payload['fields'])
    # GET includes a fresh CSRF token for the next POST; signed GET inputs are
    # validated separately with CSRF disabled because GET performs no write.
    form = MonteCarloForm() if request.method == 'POST' else MonteCarloForm(MultiDict(initial))
    if request.method == 'POST' or payload:
        valid = form.validate() if request.method == 'POST' else MonteCarloForm(MultiDict(initial), meta={'csrf': False}).validate()
        if valid and form.flexible_amount.data > allowance:
            form.flexible_amount.errors.append('Choose the calculated allowance or a lower amount, including zero.')
            valid = False
        if form.source_digest.data != digest:
            return render_template('retirement/changed.html', portfolio_name=portfolio.name), 409
        if payload and not valid:
            abort(400, 'The comparison inputs are invalid. Run a new comparison.')
        if valid and request.method == 'POST':
            fields = {name: str(form[name].data) for name in ('flexible_amount', *(role + '_percent' for role in ROLES))}
            token = _serializer().dumps({'version': MODEL_VERSION, 'path_count': PATH_COUNT, 'review': review_token,
                                         'source_digest': digest, 'fields': fields})
            result_url = url_for('monte_carlo.index', run=token)
            if request.headers.get('X-Monte-Carlo-Progress') == '1':
                experiment = prepare_experiment(basis, summary, form.flexible_amount.data)
                return _stream_run(experiment, form.percentages(), result_url)
            return redirect(result_url)
    result = None
    paths = None
    path_number = 1
    path_error = None
    if payload:
        experiment = prepare_experiment(basis, summary, form.flexible_amount.data)
        try:
            result = _run(experiment, form.percentages())
            if report:
                try:
                    path_number = int(request.args.get('path', '1'))
                    if not 1 <= path_number <= PATH_COUNT:
                        raise ValueError
                except ValueError:
                    path_error = f'Choose a whole path number from 1 to {PATH_COUNT}.'
                    path_number = 1
                if not path_error:
                    paths = inspect_path(experiment, form.percentages(), path_number)
        except (ArithmeticError, ValueError) as error:
            current_app.logger.error('Monte Carlo run failed: %s', type(error).__name__)
            return render_template('retirement/monte_carlo_unavailable.html',
                                   error='This comparison could not be calculated. No outcome frequencies are available. Review the assumptions and try again.', **base_context), 422
    return render_template('retirement/monte_carlo_details.html' if report else 'retirement/monte_carlo.html',
                           form=form, experiment=experiment, basis=basis, summary=summary,
                           allowance=allowance, result=result, chart=_chart(result) if result else None,
                           income_assumptions=income_assumptions,
                           run_token=run_token, paths=paths, path_number=path_number, path_error=path_error,
                           **base_context), 422 if form.errors or path_error else 200


@monte_carlo_blueprint.route('', methods=['GET', 'POST'])
def index():
    return _workspace()


@monte_carlo_blueprint.get('/details')
def details():
    return _workspace(report=True)
