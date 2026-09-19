"""Validation, dated spending and optional Flexible spending rules."""

from app.decimal_policy import currency_places, require_precision

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.conventions import normalize_currency_code, parse_decimal, parse_iso_date
from app.extensions import db
from app.models import Portfolio, RetirementIncome, RetirementPlan


ZERO = Decimal("0")
ONE = Decimal("1")
RULE_FIELDS = ("lower_rate_decimal", "upper_rate_decimal", "lower_multiplier_decimal",
               "middle_multiplier_decimal", "upper_multiplier_decimal")
SPENDING_POLICIES = {"full_budget": "Keep my planned Flexible budget",
                     "guardrails": "Apply my spending adjustment rules"}


class PlanValidationError(ValueError):
    def __init__(self, field, message):
        self.field, self.message = field, message
        super().__init__(message)


def adopted_plan(portfolio_id):
    return db.session.scalar(select(RetirementPlan).where(RetirementPlan.portfolio_id == portfolio_id))


def plan_incomes(plan_id):
    return tuple(db.session.scalars(select(RetirementIncome).where(
        RetirementIncome.plan_id == plan_id).order_by(RetirementIncome.start_date, RetirementIncome.id)))


def plan_details(plan):
    """Read-only presentation facts; no ORM row is passed to templates."""
    if plan is None:
        return None
    return {field: getattr(plan, field, {"planning_mode": "budget", "annual_savings_amount": ZERO, "legacy_value_basis": "nominal"}.get(field)) for field in (
        "planning_mode", "annual_savings_amount", "legacy_value_basis",
        "id", "base_date", "currency_code", "current_age_years",
        "withdrawal_start_age_years", "final_age_years", "core_amount",
        "flexible_amount", "core_inflation_decimal", "flexible_inflation_decimal",
        "equity_return_decimal", "income_return_decimal", "liquidity_return_decimal",
        "alternatives_return_decimal", "spending_policy", "lower_rate_decimal",
        "upper_rate_decimal", "lower_multiplier_decimal", "middle_multiplier_decimal",
        "upper_multiplier_decimal", "terminal_legacy_target_amount",
        "terminal_legacy_target_currency_code")}


def anniversary(value, years):
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def completed_years(start, end):
    years = end.year - start.year
    return years - (anniversary(start, years) > end)


def tier_amounts(plan, effective_date):
    years = completed_years(plan.base_date, effective_date)
    if years < 0:
        return None
    return {
        tier: getattr(plan, f"{tier}_amount") * (ONE + getattr(plan, f"{tier}_inflation_decimal")) ** years
        for tier in ("core", "flexible")
    }


def _number(data, field, minimum=ZERO, maximum=None, scale=8):
    try:
        value = parse_decimal(data.get(field), field_name=field.replace("_", " "))
    except (ValueError, TypeError) as error:
        raise PlanValidationError(field, str(error)) from error
    if value < minimum or (maximum is not None and value > maximum):
        raise PlanValidationError(field, f"Enter a value from {minimum} to {maximum}." if maximum is not None else "Enter a nonnegative amount.")
    if scale == 12:
        currency = data.get('terminal_legacy_target_currency_code') if field == 'terminal_legacy_target_amount' else data.get('currency_code')
        require_precision(value, currency_places(currency), PlanValidationError, field)
        require_precision(value, 3, PlanValidationError, field)
    if value.adjusted() >= (16 if scale == 12 else 2) or value != value.quantize(ONE.scaleb(-scale)):
        raise PlanValidationError(field, f"Use at most {scale} decimal places and a smaller value.")
    return value


def _date(data, field, optional=False):
    if optional and data.get(field) in (None, ""):
        return None
    try:
        return parse_iso_date(data.get(field), field_name=field.replace("_", " "))
    except (ValueError, TypeError) as error:
        raise PlanValidationError(field, str(error)) from error


def _currency(data, field):
    try:
        return normalize_currency_code(data.get(field), field_name=field.replace("_", " "))
    except (ValueError, TypeError) as error:
        raise PlanValidationError(field, str(error)) from error


def validate_plan(data):
    values = {"base_date": _date(data, "base_date"), "currency_code": _currency(data, "currency_code")}
    policy = data.get("spending_policy")
    if not isinstance(policy, str) or policy not in SPENDING_POLICIES:
        raise PlanValidationError("spending_policy", "Choose how to model Flexible spending.")
    values["spending_policy"] = policy
    for field, maximum in (("current_age_years", 120), ("withdrawal_start_age_years", 120), ("final_age_years", 130)):
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise PlanValidationError(field, f"Enter a whole-number age from 0 to {maximum}.")
        values[field] = value
    if values["withdrawal_start_age_years"] < values["current_age_years"]:
        raise PlanValidationError("withdrawal_start_age_years", "Retirement-start age must be at least current age.")
    if values["final_age_years"] <= values["withdrawal_start_age_years"]:
        raise PlanValidationError("final_age_years", "Final age must be greater than retirement-start age.")
    if values["base_date"].year + values["final_age_years"] - values["current_age_years"] > 9999:
        raise PlanValidationError("base_date", "Choose a base date that keeps the plan horizon within supported dates.")
    for tier in ("core", "flexible"):
        values[f"{tier}_amount"] = _number(data, f"{tier}_amount", scale=12)
        values[f"{tier}_inflation_decimal"] = _number(data, f"{tier}_inflation_decimal", maximum=ONE)
    if values["core_amount"] + values["flexible_amount"] <= 0:
        raise PlanValidationError("core_amount", "Core plus Flexible spending must be greater than zero.")
    for role in ("equity", "income", "liquidity", "alternatives"):
        field = f"{role}_return_decimal"
        values[field] = _number(data, field, minimum=-ONE, maximum=ONE)
    if policy == "full_budget" and all(data.get(field) in (None, "") for field in RULE_FIELDS):
        values.update(dict.fromkeys(RULE_FIELDS))
    else:
        for band in ("lower", "upper"):
            field = f"{band}_rate_decimal"
            values[field] = _number(data, field, maximum=ONE)
        if not ZERO < values["lower_rate_decimal"] < values["upper_rate_decimal"]:
            raise PlanValidationError("upper_rate_decimal", "Enter a positive first trigger and a larger second trigger.")
        for band in ("lower", "middle", "upper"):
            field = f"{band}_multiplier_decimal"
            values[field] = _number(data, field, maximum=Decimal("2"))
        if not values["lower_multiplier_decimal"] >= values["middle_multiplier_decimal"] >= values["upper_multiplier_decimal"]:
            raise PlanValidationError("upper_multiplier_decimal", "Allowed Flexible percentages must not increase as the withdrawal rate rises.")
    legacy = data.get("terminal_legacy_target_amount")
    values["terminal_legacy_target_amount"] = None if legacy in (None, "") else _number(data, "terminal_legacy_target_amount", scale=12)
    values["terminal_legacy_target_currency_code"] = (
        _currency(data, "terminal_legacy_target_currency_code") if values["terminal_legacy_target_amount"] is not None else None)
    if values["terminal_legacy_target_amount"] is None and data.get("terminal_legacy_target_currency_code"):
        raise PlanValidationError("terminal_legacy_target_amount", "Enter the legacy amount or clear its currency.")
    values["planning_mode"] = data.get("planning_mode", "budget")
    values["legacy_value_basis"] = data.get("legacy_value_basis", "nominal")
    values["annual_savings_amount"] = _number({"annual_savings_amount": data.get("annual_savings_amount", ZERO), "currency_code": values["currency_code"]}, "annual_savings_amount", scale=12)
    if values["planning_mode"] not in ("budget", "affordability"):
        raise PlanValidationError("planning_mode", "Unknown retirement planning mode.")
    if values["legacy_value_basis"] not in ("nominal", "today"):
        raise PlanValidationError("terminal_legacy_target_amount", "Unknown money-left value basis.")
    if values["planning_mode"] == "affordability" and (policy != "full_budget" or values["legacy_value_basis"] != "today" or values["core_inflation_decimal"] != values["flexible_inflation_decimal"]):
        raise PlanValidationError("core_inflation_decimal", "Affordability uses one inflation rate, full spending and a today-money goal.")
    if values["planning_mode"] == "affordability" and (values["terminal_legacy_target_amount"] is None or values["terminal_legacy_target_currency_code"] != values["currency_code"]):
        raise PlanValidationError("terminal_legacy_target_amount", "Affordability requires a today-money goal in the planning currency; zero is valid.")
    return values


def save_plan(portfolio_id, data, *, confirmed):
    if confirmed is not True:
        raise PlanValidationError("confirm_adoption", "Confirm that this plan supplies Retirement and Overview spending.")
    if db.session.get(Portfolio, portfolio_id) is None:
        raise PlanValidationError("portfolio_id", "Portfolio was not found.")
    values = validate_plan(data)
    plan = adopted_plan(portfolio_id)
    if plan is not None and values["spending_policy"] == "full_budget" and values["lower_rate_decimal"] is None:
        # Switching rules off does not erase the owner's existing rule set.
        values.update({field: getattr(plan, field) for field in RULE_FIELDS})
    if plan is None:
        plan = RetirementPlan(portfolio_id=portfolio_id)
        db.session.add(plan)
    for field, value in values.items():
        setattr(plan, field, value)
    db.session.flush()
    return plan


def validate_income(data):
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 160:
        raise PlanValidationError("name", "Enter an income name up to 160 characters.")
    values = {"name": name.strip(), "annual_amount": _number(data, "annual_amount", scale=12),
              "currency_code": _currency(data, "currency_code"), "start_date": _date(data, "start_date"),
              "end_date": _date(data, "end_date", optional=True),
              "inflation_decimal": _number(data, "inflation_decimal", maximum=ONE)}
    if values["end_date"] is not None and values["end_date"] < values["start_date"]:
        raise PlanValidationError("end_date", "End date must be on or after start date.")
    return values


def save_income(portfolio_id, data, income_id=None):
    plan = adopted_plan(portfolio_id)
    if plan is None:
        raise PlanValidationError("name", "Adopt a retirement plan first.")
    values = validate_income(data)
    row = db.session.get(RetirementIncome, income_id) if income_id is not None else None
    if income_id is not None and (row is None or row.plan_id != plan.id):
        raise PlanValidationError("name", "Income was not found in this plan.")
    if row is None:
        row = RetirementIncome(plan_id=plan.id)
        db.session.add(row)
    for field, value in values.items():
        setattr(row, field, value)
    db.session.flush()
    return row


def flexible_policy(plan, *, capital, core_need, target, income_left):
    """Three owner-entered bands, evaluated before spending adjustment."""
    desired_need = core_need + max(ZERO, target - income_left)
    if plan.spending_policy == "full_budget":
        return {"rate": desired_need / capital if capital > 0 else None,
                "allowed": target, "band": "full_budget", "multiplier": ONE}
    if capital <= 0:
        return {"rate": None, "allowed": min(target, income_left), "band": "income_only", "multiplier": None}
    rate = desired_need / capital
    band = "lower" if rate < plan.lower_rate_decimal else "middle" if rate < plan.upper_rate_decimal else "upper"
    multiplier = getattr(plan, f"{band}_multiplier_decimal")
    return {"rate": rate, "allowed": target * multiplier, "band": band, "multiplier": multiplier}
