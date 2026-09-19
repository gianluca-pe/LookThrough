"""Solve a steady real Flexible allowance using the shared annual engine."""

from copy import copy
from decimal import Decimal
from types import SimpleNamespace

from app.decimal_policy import currency_places, MAX_UNITS
from app.services.portfolio_summary import build_portfolio_summary
from app.services.retirement import prepare_retirement_projection, run_retirement_projection
from app.services.retirement_plans import adopted_plan, validate_plan, PlanValidationError, plan_details, RULE_FIELDS

ZERO = Decimal("0")


def validate_affordability(data):
    """Compact input contract; the allowance is a result, never an input."""
    inflation = data.get("inflation_decimal")
    values = {**data, "flexible_amount": Decimal(1).scaleb(-currency_places(data.get("currency_code"))),
              "core_inflation_decimal": inflation, "flexible_inflation_decimal": inflation,
              "planning_mode": "affordability", "spending_policy": "full_budget",
              "legacy_value_basis": "today", **dict.fromkeys(RULE_FIELDS),
              "terminal_legacy_target_currency_code": data.get("currency_code")}
    if data.get("annual_savings_amount") is None:
        raise PlanValidationError("annual_savings_amount", "Enter yearly net savings; enter 0 for no savings.")
    if data.get("terminal_legacy_target_amount") is None:
        raise PlanValidationError("terminal_legacy_target_amount", "Enter the money you want left in today's purchasing power; 0 is valid.")
    values = validate_plan(values)
    values["flexible_amount"] = ZERO
    return values


def prepare_affordability(portfolio, data):
    values = validate_affordability(data)
    saved = adopted_plan(portfolio.id)
    plan = SimpleNamespace(**values, id=saved.id if saved else None, portfolio_id=portfolio.id)
    summary = build_portfolio_summary(portfolio, values["base_date"], reporting_currency=values["currency_code"])
    basis = prepare_retirement_projection(portfolio, summary, values["base_date"],
                                          reporting_currency=values["currency_code"], plan_override=plan)
    return basis, summary


def _trial(basis, flexible):
    candidate = dict(basis)
    plan = copy(basis["_inputs"]["plan"])
    plan.flexible_amount = flexible
    candidate["_inputs"] = {**basis["_inputs"], "plan": plan}
    candidate["assumption"] = copy(basis["assumption"])
    candidate["assumption"].flexible_amount = flexible
    candidate["plan"] = plan_details(plan)
    candidate["spending"] = {**basis["spending"], "native_amount": plan.core_amount + flexible,
                             "reporting_amount": (plan.core_amount + flexible) * basis["spending"]["fx_rate"]}
    return run_retirement_projection(candidate)


def _fits(projection):
    return (projection["lifestyle"]["full_budget_funded_all_years"]
            and projection["final_value_amount"] >= projection["legacy_target_reporting_amount"]
            and projection["years"][-1]["opening_obligation_remaining_amount"] == 0)


def solve_affordability(basis):
    """Largest feasible annual base-date allowance to the currency minor unit, rounded down.

    Nonnegative annual return factors, identical contributions/income and the
    fixed source withdrawal order make feasibility monotone in the allowance.
    Every candidate must pay every year's targets, not just meet ending capital.
    """
    if not basis["calculation_complete"]:
        return {"status": "missing", "projection": run_retirement_projection(basis), "flexible_amount": None}
    core = _trial(basis, ZERO)
    if not _fits(core):
        status = "core_shortfall" if core["lifestyle"]["first_core_shortfall"] else "goal_shortfall"
        return _present(core, ZERO, status)
    places = currency_places(basis['_inputs']['plan'].currency_code)
    quantum = Decimal(1).scaleb(-places)
    maximum_units = MAX_UNITS // 10 ** (3 - places)
    low, high = 0, 10000  # Search bounds only; never a product assumption.
    runs = 1
    while True:
        candidate = _trial(basis, Decimal(high) * quantum)
        runs += 1
        if not _fits(candidate):
            break
        low = high
        if high == maximum_units:
            return {**_present(core, ZERO, "limit"), "flexible_amount": None}
        high = min(high * 2, maximum_units)
    while high - low > 1:
        middle = (low + high) // 2
        runs += 1
        if _fits(_trial(basis, Decimal(middle) * quantum)):
            low = middle
        else:
            high = middle
    allowance = Decimal(low) * quantum
    result = _present(_trial(basis, allowance), allowance, "funded" if allowance else "core_only")
    result["projection_runs"] = runs + 1
    return result


def _present(projection, allowance, status):
    plan = projection["plan"]
    inflation = plan["core_inflation_decimal"]
    rows = []
    for index, row in enumerate(projection["years"]):
        start_factor = (1 + inflation) ** index
        end_factor = start_factor * (1 + inflation)
        rows.append({**row, "ending_age": row["age"] + 1,
                     "core_paid_today": row["core_paid_amount"] / start_factor,
                     "flexible_paid_today": row["flexible_paid_amount"] / start_factor,
                     "total_paid_amount": row["core_paid_amount"] + row["flexible_paid_amount"],
                     "total_paid_today": (row["core_paid_amount"] + row["flexible_paid_amount"]) / start_factor,
                     "ending_value_today": row["ending_value_amount"] / end_factor,
                     "savings_added_amount": row.get("savings_added_amount", ZERO)})
    projection = {**projection, "years": tuple(rows)}
    first = next((row for row in rows if row["core_shortfall_amount"] > 0), None)
    # A positive, locked portfolio is a payment-access gap, not capital depletion.
    shortfall_cause = ("access" if first and first["starting_value_amount"] > first["withdrawn_amount"] else "resources") if first else None
    total = plan["core_amount"] + allowance
    return {"status": status, "projection": projection, "flexible_amount": allowance,
            "core_amount": plan["core_amount"], "total_amount": total,
            "core_monthly": plan["core_amount"] / 12, "flexible_monthly": allowance / 12,
            "total_monthly": total / 12,
            "includes_planned_income": any(row["income_to_core_amount"] > 0 or row["income_to_flexible_amount"] > 0 for row in rows),
            "final_today_amount": rows[-1]["ending_value_today"],
            "legacy_today_amount": plan["terminal_legacy_target_amount"],
            "legacy_nominal_amount": projection["legacy_target_reporting_amount"],
            "legacy_gap_today": rows[-1]["ending_value_today"] - plan["terminal_legacy_target_amount"],
            "first_core_shortfall": first, "shortfall_cause": shortfall_cause}
