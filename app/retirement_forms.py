"""No-JavaScript forms for the explicitly adopted retirement baseline."""

from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import BooleanField, DateField, SelectField, StringField, SubmitField, TextAreaField, IntegerField, HiddenField
from wtforms.validators import InputRequired, Length, NumberRange, Optional

from app.form_fields import DecimalField
from app.planning_forms import RetirementProjectionForm
from app.services.retirement_plans import PlanValidationError, validate_plan, validate_income, RULE_FIELDS, SPENDING_POLICIES


def number(label, maximum=None):
    return DecimalField(label, places=None, validators=[InputRequired(), NumberRange(min=0, max=maximum)])


def reject_nonfinite(form):
    for field in form:
        if isinstance(field.data, Decimal) and not field.data.is_finite():
            field.process_errors = list(field.process_errors) + ["Enter a finite number."]
            field.data = None


class RetirementPlanForm(RetirementProjectionForm):
    base_date = DateField("Plan base date", validators=[InputRequired()])
    currency_code = StringField("Plan currency", validators=[InputRequired(), Length(min=3, max=3)])
    core_amount = number("Annual Core spending")
    flexible_amount = number("Annual target Flexible spending")
    core_inflation_percent = number("Core inflation (%)", 100)
    flexible_inflation_percent = number("Flexible inflation (%)", 100)
    spending_policy = SelectField("How should Flexible spending change?",
        choices=[("", "Choose a spending approach")] + list(SPENDING_POLICIES.items()), validators=[InputRequired()])
    lower_rate_percent = number("First withdrawal trigger (%)", 100)
    upper_rate_percent = number("Second withdrawal trigger (%)", 100)
    lower_multiplier_percent = number("Below the first trigger: spend this share of my Flexible budget (%)", 200)
    middle_multiplier_percent = number("From the first trigger up to the second: spend this share (%)", 200)
    upper_multiplier_percent = number("At or above the second trigger: spend this share (%)", 200)
    terminal_legacy_target_currency_code = StringField("Legacy target currency", validators=[Optional(), Length(min=3, max=3)])
    confirm_adoption = BooleanField("Use these assumptions and Core + Flexible spending for Retirement and Overview")
    preview_plan = SubmitField("Review plan")
    save_projection = SubmitField("Adopt plan and project")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_age_years.label.text = "Age on the plan base date"
        self.withdrawal_start_age_years.label.text = "Age when retirement spending begins"
        self.final_age_years.label.text = "Age when this plan ends"
        self.terminal_legacy_target_amount.label.text = "Money I want remaining when the plan ends (future money)"

    def values(self):
        result = {}
        for field in self:
            if field.name.endswith("_percent"):
                result[field.name.removesuffix("_percent") + "_decimal"] = field.data / Decimal("100") if field.data is not None else None
            else:
                result[field.name] = field.data
        return result

    def validate(self, extra_validators=None):
        reject_nonfinite(self)
        if self.spending_policy.data == "full_budget":
            for name in RULE_FIELDS:
                field = self[name.removesuffix("_decimal") + "_percent"]
                field.validators = [Optional(), *field.validators[1:]]
        valid = FlaskForm.validate(self, extra_validators=extra_validators)
        if valid:
            try:
                validate_plan(self.values())
            except PlanValidationError as error:
                self.add_service_error(error)
                valid = False
        return valid

    def add_service_error(self, error):
        field = error.field.removesuffix("_decimal") + "_percent" if error.field.endswith("_decimal") else error.field
        getattr(self, field, self.core_amount).errors.append(error.message)


class RetirementIncomeForm(FlaskForm):
    name = StringField("Income name", validators=[InputRequired(), Length(max=160)])
    annual_amount = number("Annual amount available after tax")
    currency_code = StringField("Income currency", validators=[InputRequired(), Length(min=3, max=3)])
    start_date = DateField("First eligible payment date", validators=[InputRequired()])
    end_date = DateField("Last eligible payment date", validators=[Optional()])
    inflation_percent = number("Income inflation (%)", 100)
    save_income = SubmitField("Save income")

    def values(self):
        return {"name": self.name.data, "annual_amount": self.annual_amount.data,
                "currency_code": self.currency_code.data, "start_date": self.start_date.data,
                "end_date": self.end_date.data,
                "inflation_decimal": self.inflation_percent.data / Decimal("100") if self.inflation_percent.data is not None else None}

    def validate(self, extra_validators=None):
        reject_nonfinite(self)
        valid = super().validate(extra_validators=extra_validators)
        if valid:
            try:
                validate_income(self.values())
            except PlanValidationError as error:
                field = "inflation_percent" if error.field == "inflation_decimal" else error.field
                getattr(self, field, self.name).errors.append(error.message)
                valid = False
        return valid


class RemoveIncomeForm(FlaskForm):
    confirm_remove = BooleanField("Remove this planned income stream", validators=[InputRequired()])
    remove_income = SubmitField("Remove income")


class RetirementScenarioForm(FlaskForm):
    name = StringField("Comparison name", validators=[InputRequired(), Length(max=160)])
    as_of_date = DateField("Starting portfolio date", validators=[InputRequired()])
    return_path = TextAreaField("Annual return percentages: Equity, Income, Liquidity, Alternatives",
                               validators=[InputRequired(), Length(max=12000)])
    save_comparison = SubmitField("Save comparison")


class RetirementAffordabilityForm(FlaskForm):
    base_date = DateField("Portfolio and purchasing-power date", validators=[InputRequired()])
    currency_code = StringField("Planning currency", validators=[InputRequired(), Length(min=3, max=3)])
    current_age_years = IntegerField("Your age at this date", validators=[InputRequired(), NumberRange(min=0, max=120)])
    withdrawal_start_age_years = IntegerField("Retirement age", validators=[InputRequired(), NumberRange(min=0, max=120)])
    final_age_years = IntegerField("Plan through age", validators=[InputRequired(), NumberRange(min=1, max=130)])
    core_amount = number("Annual Core lifestyle — today's money")
    annual_savings_amount = number("Annual net savings while working — fixed yearly amount")
    terminal_legacy_target_amount = number("Money left at the end — today's money")
    inflation_percent = number("Inflation (%)", 100)
    equity_return_percent = DecimalField("Equity return (%)", places=None, validators=[InputRequired(), NumberRange(min=-100, max=100)])
    income_return_percent = DecimalField("Income return (%)", places=None, validators=[InputRequired(), NumberRange(min=-100, max=100)])
    liquidity_return_percent = DecimalField("Liquidity return (%)", places=None, validators=[InputRequired(), NumberRange(min=-100, max=100)])
    alternatives_return_percent = DecimalField("Alternatives return (%)", places=None, validators=[InputRequired(), NumberRange(min=-100, max=100)])
    project = SubmitField("Update projection")
    save_plan = SubmitField("Save as my plan")

    def values(self):
        values = {}
        for field in self:
            if field.name.endswith("_percent"):
                values[field.name.removesuffix("_percent") + "_decimal"] = field.data / Decimal("100") if field.data is not None else None
            elif field.name not in {"csrf_token", "project", "save_plan"}:
                values[field.name] = field.data
        return values

    def add_service_error(self, error):
        field = error.field
        if field in {"core_inflation_decimal", "flexible_inflation_decimal"}:
            field = "inflation_percent"
        elif field.endswith("_decimal"):
            field = field.removesuffix("_decimal") + "_percent"
        getattr(self, field, self.core_amount).errors.append(error.message)

    def validate(self, extra_validators=None):
        from app.services.retirement_affordability import validate_affordability
        reject_nonfinite(self)
        valid = super().validate(extra_validators=extra_validators)
        if valid:
            try:
                validate_affordability(self.values())
            except PlanValidationError as error:
                self.add_service_error(error)
                valid = False
        return valid


class MonteCarloForm(FlaskForm):
    review = HiddenField(validators=[InputRequired()])
    source_digest = HiddenField(validators=[InputRequired()])
    flexible_amount = number("Annual Flexible to test — today's USD")
    equity_percent = number("Equity (%)", 100)
    income_percent = number("Income (%)", 100)
    liquidity_percent = number("Liquidity (%)", 100)
    alternatives_percent = number("Alternatives (%)", 100)
    run_comparison = SubmitField("Run comparison")

    def percentages(self):
        return {role: self[role + '_percent'].data for role in ('equity', 'income', 'liquidity', 'alternatives')}

    def validate(self, extra_validators=None):
        from app.services.retirement_monte_carlo import allocation_weights, MonteCarloValidationError
        reject_nonfinite(self)
        valid = super().validate(extra_validators=extra_validators)
        if valid:
            try:
                allocation_weights(self.percentages())
            except MonteCarloValidationError as error:
                self.equity_percent.errors.append(str(error))
                valid = False
        return valid
