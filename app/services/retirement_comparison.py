"""Two spending approaches on the same dated portfolio and annual engine."""

from app.services.retirement import build_retirement_projection
from app.services.retirement_plans import adopted_plan


def build_retirement_comparison(portfolio, summary, as_of_date, *, reporting_currency):
    plan = adopted_plan(portfolio.id)
    if plan is None:
        return None
    full = build_retirement_projection(portfolio, summary, as_of_date,
        reporting_currency=reporting_currency, spending_policy="full_budget")
    adjusted = None
    if plan.lower_rate_decimal is not None:
        adjusted = build_retirement_projection(portfolio, summary, as_of_date,
            reporting_currency=reporting_currency, spending_policy="guardrails")
    complete = full["calculation_complete"] and (adjusted is None or adjusted["calculation_complete"])
    difference = None
    if complete and adjusted is not None:
        difference = {
            # Positive means the full-budget path paid more Flexible / rules left more capital.
            "flexible_spending_difference_amount": full["lifestyle"]["total_flexible_base_year_amount"] - adjusted["lifestyle"]["total_flexible_base_year_amount"],
            "ending_capital_difference_amount": adjusted["final_value_amount"] - full["final_value_amount"],
        }
    return {"full_budget": full, "guardrails": adjusted, "difference": difference,
            "calculation_complete": complete, "saved_policy": plan.spending_policy,
            "currency": reporting_currency, "base_date": plan.base_date}
