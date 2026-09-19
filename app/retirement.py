"""Retirement entry point and explicit plan adoption, without ledger writes."""

from decimal import Decimal
from app.decimal_policy import money

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.extensions import db
from app.models import RetirementIncome
from app.planning import _as_of_date, _projection_chart
from app.retirement_forms import RetirementPlanForm, RetirementIncomeForm, RemoveIncomeForm
from app.services.portfolio_summary import build_portfolio_summary
from app.services.retirement import retirement_assumption
from app.services.retirement_comparison import build_retirement_comparison
from app.services.retirement_plans import adopted_plan, plan_details, plan_incomes, save_plan, save_income, PlanValidationError, validate_plan
from app.setup import _current_portfolio
from app.template_filters import money_amount


retirement_blueprint = Blueprint("retirement", __name__)


def _spending_chart(projection) -> dict[str, object] | None:
    """Presentation payload for the two-tier actual-spending chart.

    Maps the projection's annual server fields to display values; computes
    nothing. Stacks what was actually paid, never the Core target, so a
    shortfall year can never read as fully paid. The annual table beside the
    chart is its equivalent.
    """

    if not projection["calculation_complete"] or not projection["years"]:
        return None
    years = projection["years"]
    return {
        "labels": [str(year["age"]) for year in years],
        "desired_values": [
            float(money_amount(year["spending_required_amount"], currency=projection["reporting_currency"]).replace(",", "")) for year in years
        ],
        "core_values": [
            float(money_amount(year["core_paid_amount"], currency=projection["reporting_currency"]).replace(",", "")) for year in years
        ],
        "flexible_values": [
            float(money_amount(year["flexible_paid_amount"], currency=projection["reporting_currency"]).replace(",", "")) for year in years
        ],
        "currency": projection["reporting_currency"],
    }


def _scenario_chart(comparison) -> dict[str, object] | None:
    """Presentation payload for the two-path ending-capital chart.

    Maps the frozen annual rows to display values; computes nothing. The
    annual tables carry every plotted figure and remain the equivalent.
    """

    baseline, stress = comparison["baseline"], comparison["stress"]
    if not baseline["years"] or len(baseline["years"]) != len(stress["years"]):
        return None
    return {
        "labels": [str(year["age"]) for year in baseline["years"]],
        "baseline_values": [
            float(money_amount(year["ending_value_amount"], currency=baseline["reporting_currency"]).replace(",", "")) for year in baseline["years"]
        ],
        "stress_values": [
            float(money_amount(year["ending_value_amount"], currency=baseline["reporting_currency"]).replace(",", "")) for year in stress["years"]
        ],
        "currency": baseline["reporting_currency"],
    }


@retirement_blueprint.route("/retirement/budget", methods=["GET", "POST"])
def budget():
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    plan = adopted_plan(portfolio.id)
    if plan and plan.planning_mode == "affordability":
        flash("Your plan now uses the affordability simulator. Saved return comparisons remain available.", "success")
        return redirect(url_for("retirement.index"))
    as_of_date, as_of_error = _as_of_date(portfolio)
    form = RetirementPlanForm()
    if not form.is_submitted():
        existing = plan or retirement_assumption(portfolio.id)
        form.base_date.data = plan.base_date if plan else as_of_date
        form.currency_code.data = plan.currency_code if plan else portfolio.annual_spending_currency_code
        if existing:
            for field in form:
                source = field.name.removesuffix("_percent") + "_decimal" if field.name.endswith("_percent") else field.name
                if hasattr(existing, source):
                    value = getattr(existing, source)
                    field.data = value * Decimal("100") if field.name.endswith("_percent") and value is not None else value
    preview = None
    if form.validate_on_submit():
        if as_of_error:
            form.base_date.errors.append(as_of_error)
        else:
            values = validate_plan(form.values())
            preview = {"total_amount": values["core_amount"] + values["flexible_amount"], "currency": values["currency_code"], "base_date": values["base_date"], "spending_policy": values["spending_policy"]}
            if form.save_projection.data and not form.confirm_adoption.data:
                form.confirm_adoption.errors.append(
                    "Confirm that Retirement and Overview should use this plan before adopting it."
                )
            if form.confirm_adoption.data and form.save_projection.data:
                try:
                    save_plan(portfolio.id, form.values(), confirmed=True)
                    db.session.commit()
                except PlanValidationError as error:
                    db.session.rollback()
                    form.add_service_error(error)
                else:
                    flash("Retirement plan adopted. Overview now uses its Core + target Flexible spending.", "success")
                    return redirect(url_for("retirement.budget", as_of=max(as_of_date, values["base_date"]).isoformat()))
    currency = plan.currency_code if plan else portfolio.reporting_currency_code
    summary = build_portfolio_summary(portfolio, as_of_date, reporting_currency=currency)
    comparison = build_retirement_comparison(portfolio, summary, as_of_date, reporting_currency=currency) if plan else None
    selected_path = request.args.get("path", "full_budget")
    if selected_path not in {"full_budget", "guardrails"} or not comparison or comparison.get(selected_path) is None:
        selected_path = "full_budget"
    projection = comparison[selected_path] if comparison else None
    chart = _projection_chart(projection) if projection else None
    spending_chart = _spending_chart(projection) if projection else None
    income_rows = tuple({field: getattr(row, field) for field in (
        "id", "name", "annual_amount", "currency_code", "start_date", "end_date", "inflation_decimal")}
        for row in plan_incomes(plan.id)) if plan else ()
    return render_template("retirement/budget.html", form=form, plan=plan_details(plan), preview=preview,
                           portfolio=portfolio, portfolio_name=portfolio.name, as_of_date=as_of_date,
                           as_of_error=as_of_error, projection=projection, projection_chart=chart,
                           spending_chart=spending_chart, incomes=income_rows,
                           comparison=comparison, selected_path=selected_path)


@retirement_blueprint.route("/retirement/income/new", methods=["GET", "POST"])
@retirement_blueprint.route("/retirement/income/<int:income_id>", methods=["GET", "POST"])
def income(income_id=None):
    portfolio = _current_portfolio()
    plan = adopted_plan(portfolio.id) if portfolio else None
    if plan is None:
        return redirect(url_for("retirement.index"))
    row = db.session.get(RetirementIncome, income_id) if income_id is not None else None
    if income_id is not None and (row is None or row.plan_id != plan.id):
        abort(404)
    form = RetirementIncomeForm()
    remove_form = RemoveIncomeForm()
    if not form.is_submitted() and row:
        for field in form:
            if hasattr(row, field.name):
                field.data = getattr(row, field.name)
        form.inflation_percent.data = row.inflation_decimal * Decimal("100")
    if request.method == "POST" and "remove_income" in request.form:
        if row and remove_form.validate_on_submit():
            db.session.delete(row)
            db.session.commit()
            flash("Planned income removed.", "success")
            return redirect(url_for("retirement.index"))
    elif form.validate_on_submit():
        try:
            save_income(portfolio.id, form.values(), income_id=income_id)
            db.session.commit()
        except PlanValidationError as error:
            db.session.rollback()
            getattr(form, error.field, form.name).errors.append(error.message)
        else:
            flash("Planned income saved.", "success")
            return redirect(url_for("retirement.index"))
    return render_template("retirement/income.html", form=form, remove_form=remove_form,
                           income={"id": row.id} if row else None, portfolio_name=portfolio.name)


@retirement_blueprint.route("/retirement/scenarios", methods=["GET", "POST"])
def scenarios():
    from app.retirement_forms import RetirementScenarioForm
    from app.services.retirement_scenarios import save_scenario, scenarios_for_portfolio, ScenarioValidationError

    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    plan = adopted_plan(portfolio.id)
    form = RetirementScenarioForm()
    if not form.is_submitted():
        form.as_of_date.data, date_error = _as_of_date(portfolio)
        if date_error:
            form.as_of_date.errors = [date_error]
    if form.validate_on_submit():
        try:
            row = save_scenario(portfolio, form.as_of_date.data, name=form.name.data,
                                return_path=form.return_path.data)
            db.session.commit()
        except ScenarioValidationError as error:
            db.session.rollback()
            getattr(form, error.field).errors.append(error.message)
        else:
            flash("Comparison saved with its starting inputs. Your adopted plan and Overview are unchanged.", "success")
            return redirect(url_for("retirement.scenario", scenario_id=row.id))
    rows = [{"id": row.id, "name": row.name, "as_of_date": row.as_of_date,
             "created_at": row.created_at} for row in scenarios_for_portfolio(portfolio.id)]
    return render_template("retirement/scenarios.html", form=form, scenarios=rows,
                           has_plan=plan is not None, plan=plan_details(plan),
                           portfolio_name=portfolio.name)


@retirement_blueprint.get("/retirement/scenarios/<int:scenario_id>")
def scenario(scenario_id):
    from app.models import RetirementScenario
    from app.services.retirement_scenarios import load_payload

    portfolio = _current_portfolio()
    row = db.session.get(RetirementScenario, scenario_id)
    if portfolio is None or row is None or row.portfolio_id != portfolio.id:
        abort(404)
    payload = load_payload(row.payload_json)
    return render_template("retirement/scenario.html", comparison=payload,
                           scenario_chart=_scenario_chart(payload),
                           saved_at=row.created_at, portfolio_name=portfolio.name)


def _affordability_review():
    from flask import current_app
    from itsdangerous import URLSafeSerializer
    return URLSafeSerializer(current_app.secret_key, salt="retirement-affordability")


def _basis_digest(basis):
    from hashlib import sha256
    from app.services.retirement_scenarios import encode_payload
    return sha256(encode_payload(basis).encode()).hexdigest()


def _affordability_chart(result):
    if result['status'] in {'missing', 'limit'}:
        return None
    rows = result['projection']['years']
    currency = result['projection']['plan']['currency_code']
    def numbers(key):
        return [float(money_amount(row[key], currency=currency).replace(',', '')) for row in rows]
    return dict(ages=[row['age'] for row in rows], ending_ages=[row['ending_age'] for row in rows],
                core=numbers('core_paid_amount'), flexible=numbers('flexible_paid_amount'),
                total=numbers('total_paid_amount'), today=numbers('total_paid_today'),
                initial_index=next((index for index, row in enumerate(rows) if row['phase'] == 'withdrawal'), 0),
                capital=numbers('ending_value_today'), capital_nominal=numbers('ending_value_amount'),
                goal=[float(money_amount(result['legacy_today_amount'], currency=currency).replace(',', ''))] * len(rows))


def _prefill_affordability(form, plan, portfolio, as_of):
    from app.services.retirement_plans import completed_years
    form.base_date.data = as_of
    form.currency_code.data = plan.currency_code if plan else portfolio.reporting_currency_code
    form.annual_savings_amount.data = plan.annual_savings_amount if plan else Decimal('0')
    if not plan:
        return
    elapsed = max(0, completed_years(plan.base_date, as_of))
    for name in ('current_age_years', 'withdrawal_start_age_years', 'final_age_years', 'core_amount'):
        form[name].data = getattr(plan, name)
    form.current_age_years.data += elapsed
    form.withdrawal_start_age_years.data = max(form.current_age_years.data, plan.withdrawal_start_age_years)
    form.core_amount.data = money(form.core_amount.data * (1 + plan.core_inflation_decimal) ** elapsed, plan.currency_code)
    # Never silently reinterpret an old nominal legacy or merge two inflation rates.
    if plan.core_inflation_decimal == plan.flexible_inflation_decimal:
        form.inflation_percent.data = plan.core_inflation_decimal * 100
    if plan.legacy_value_basis == 'today':
        form.terminal_legacy_target_amount.data = money(plan.terminal_legacy_target_amount * (1 + plan.core_inflation_decimal) ** elapsed, plan.currency_code)
    for role in ('equity', 'income', 'liquidity', 'alternatives'):
        form[role + '_return_percent'].data = getattr(plan, role + '_return_decimal') * 100


@retirement_blueprint.route('/retirement', methods=['GET', 'POST'])
def index():
    from itsdangerous import BadSignature
    from werkzeug.datastructures import MultiDict
    from app.retirement_forms import RetirementAffordabilityForm
    from app.services.retirement_affordability import prepare_affordability, solve_affordability, validate_affordability
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for('setup.show'))
    plan = adopted_plan(portfolio.id)
    as_of, date_error = _as_of_date(portfolio)
    review = None
    if request.method == 'POST' and 'save_plan' in request.form:
        try:
            review = _affordability_review().loads(request.form.get('review', ''))
        except BadSignature:
            abort(400, 'This projection review is invalid. Update the projection and try again.')
        if review.get('portfolio_id') != portfolio.id:
            abort(400)
        form = RetirementAffordabilityForm(MultiDict({**review['fields'], 'csrf_token': request.form.get('csrf_token', '')}))
    else:
        form = RetirementAffordabilityForm()
    editing_review = request.args.get('review') if request.method == 'GET' else None
    if editing_review:
        try:
            previous = _affordability_review().loads(editing_review)
        except BadSignature:
            abort(400, 'This projection review is invalid. Update the projection.')
        if previous.get('portfolio_id') != portfolio.id:
            abort(404)
        fields = dict(previous['fields'])
        if hasattr(form, 'csrf_token'):
            fields['csrf_token'] = form.csrf_token.current_token
        form = RetirementAffordabilityForm(MultiDict(fields))
    elif request.method == 'GET':
        _prefill_affordability(form, plan, portfolio, as_of)
        if plan and plan.planning_mode == 'affordability':
            fields = {field.name: field._value() for field in form if field.name not in {'project', 'save_plan', 'csrf_token'}}
            if hasattr(form, 'csrf_token'):
                fields['csrf_token'] = form.csrf_token.current_token
            form = RetirementAffordabilityForm(MultiDict(fields))
    result = basis = None
    # A saved affordability plan can project immediately. Older goals require a real-money choice.
    should_project = request.method == 'POST' or editing_review or (plan and plan.planning_mode == 'affordability')
    if should_project and form.validate():
        basis, _ = prepare_affordability(portfolio, form.values())
        result = solve_affordability(basis)
        if review is not None:
            if review['digest'] != _basis_digest(basis):
                flash('Portfolio or income inputs changed since this review. Review the updated result before saving.', 'warning')
            elif result['flexible_amount'] is not None:
                try:
                    values = validate_affordability(form.values())
                    values['flexible_amount'] = result['flexible_amount']
                    save_plan(portfolio.id, values, confirmed=True)
                    db.session.commit()
                except PlanValidationError as error:
                    db.session.rollback()
                    form.add_service_error(error)
                else:
                    flash('Plan saved. Overview now uses this Core plus Flexible allowance.', 'success')
                    return redirect(url_for('retirement.index', as_of=values['base_date'].isoformat()))
    if date_error:
        form.base_date.errors = list(form.base_date.errors) + [date_error]
        result = None
    token = None
    if result:
        fields = {field.name: field._value() for field in form if field.name not in {'project', 'save_plan', 'csrf_token'}}
        token = _affordability_review().dumps(dict(fields=fields, digest=_basis_digest(basis), portfolio_id=portfolio.id))
    incomes = [dict(id=row.id, name=row.name, annual_amount=row.annual_amount, currency_code=row.currency_code) for row in plan_incomes(plan.id)] if plan else []
    return render_template('retirement/index.html', form=form, plan=plan_details(plan), result=result,
                           chart=_affordability_chart(result) if result else None, review_token=token,
                           incomes=incomes, starting_mix=basis.get("_inputs", {}).get("savings_weights", {}) if basis else {}, portfolio_name=portfolio.name)


@retirement_blueprint.get('/retirement/details')
def details():
    from itsdangerous import BadSignature
    from werkzeug.datastructures import MultiDict
    from app.retirement_forms import RetirementAffordabilityForm
    from app.services.retirement_affordability import prepare_affordability, solve_affordability
    portfolio = _current_portfolio()
    try:
        review = _affordability_review().loads(request.args.get('review', ''))
    except BadSignature:
        abort(400, 'Open detailed assumptions from a current retirement projection.')
    if portfolio is None or review.get('portfolio_id') != portfolio.id:
        abort(404)
    form = RetirementAffordabilityForm(MultiDict(review['fields']), meta={'csrf': False})
    if not form.validate():
        abort(400)
    basis, summary = prepare_affordability(portfolio, form.values())
    if review['digest'] != _basis_digest(basis):
        return render_template('retirement/changed.html', portfolio_name=portfolio.name), 409
    result = solve_affordability(basis)
    return render_template('retirement/details.html', result=result, projection=result['projection'],
                           inputs=form.values(), basis=basis, summary=summary, portfolio_name=portfolio.name)
