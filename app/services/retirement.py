"""Deterministic, owner-assumption retirement projection."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import Mapping
from types import SimpleNamespace

from sqlalchemy import select

from app.conventions import normalize_currency_code, parse_decimal
from app.extensions import db
from app.models import ASSET_ROLE_CODES, Portfolio, RetirementAssumption
from app.services.spending import build_spending_value
from app.services.fx import resolve_fx
from app.services.retirement_plans import (
    adopted_plan, anniversary, completed_years, flexible_policy, plan_details, plan_incomes, tier_amounts,
)


RETIREMENT_BUCKETS = ("now", "bridge", "growth")
BUCKET_ORDER = {"now": 1, "bridge": 2, "growth": 3}
ROLE_RETURN_FIELDS = {
    "equity": "equity_return_decimal",
    "income": "income_return_decimal",
    "liquidity": "liquidity_return_decimal",
    "alternatives": "alternatives_return_decimal",
}


class RetirementValidationError(ValueError):
    """An owner-entered projection assumption is invalid."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def retirement_assumption(portfolio_id: int) -> RetirementAssumption | None:
    return db.session.scalar(
        select(RetirementAssumption).where(
            RetirementAssumption.portfolio_id == portfolio_id
        )
    )


def _age(value: object, *, field: str, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RetirementValidationError(field, f"{label} must be a whole number.")
    if value < 0 or value > maximum:
        raise RetirementValidationError(
            field, f"{label} must be between 0 and {maximum}."
        )
    return value


def _rate(value: object, *, field: str, label: str) -> Decimal:
    try:
        parsed = parse_decimal(value, field_name=label)
    except (TypeError, ValueError) as error:
        raise RetirementValidationError(field, str(error)) from error
    if parsed < Decimal("-1") or parsed > Decimal("1"):
        raise RetirementValidationError(
            field, f"{label} must be between -100% and 100%."
        )
    return parsed


def save_retirement_assumption(
    portfolio_id: int,
    *,
    current_age_years: int,
    withdrawal_start_age_years: int,
    final_age_years: int,
    role_returns: Mapping[str, Decimal],
    terminal_legacy_target_amount: Decimal | None = None,
) -> RetirementAssumption:
    """Create or replace the portfolio's single explicit base-case assumptions."""

    portfolio = db.session.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise RetirementValidationError("portfolio_id", "Portfolio was not found.")
    current_age = _age(
        current_age_years,
        field="current_age_years",
        label="Current age",
        maximum=120,
    )
    withdrawal_age = _age(
        withdrawal_start_age_years,
        field="withdrawal_start_age_years",
        label="Withdrawal-start age",
        maximum=120,
    )
    final_age = _age(
        final_age_years,
        field="final_age_years",
        label="Final age",
        maximum=130,
    )
    if withdrawal_age <= current_age:
        raise RetirementValidationError(
            "withdrawal_start_age_years",
            "Withdrawal-start age must be greater than current age.",
        )
    if final_age <= withdrawal_age:
        raise RetirementValidationError(
            "final_age_years",
            "Final age must be greater than withdrawal-start age.",
        )
    if set(role_returns) != set(ASSET_ROLE_CODES):
        raise RetirementValidationError(
            "role_returns", "Enter a nominal return for all four asset roles."
        )
    parsed_returns = {
        role: _rate(
            role_returns[role],
            field=f"{role}_return_percent",
            label=f"{role.capitalize()} return",
        )
        for role in ASSET_ROLE_CODES
    }
    legacy_target = None
    if terminal_legacy_target_amount is not None:
        try:
            legacy_target = parse_decimal(
                terminal_legacy_target_amount,
                field_name="Terminal legacy target",
            )
        except (TypeError, ValueError) as error:
            raise RetirementValidationError(
                "terminal_legacy_target_amount", str(error)
            ) from error
        if legacy_target < 0:
            raise RetirementValidationError(
                "terminal_legacy_target_amount",
                "Terminal legacy target must not be negative.",
            )

    row = retirement_assumption(portfolio_id)
    if row is None:
        row = RetirementAssumption(portfolio_id=portfolio_id)
        db.session.add(row)
    row.current_age_years = current_age
    row.withdrawal_start_age_years = withdrawal_age
    row.final_age_years = final_age
    for role, field in ROLE_RETURN_FIELDS.items():
        setattr(row, field, parsed_returns[role])
    row.terminal_legacy_target_amount = legacy_target
    row.terminal_legacy_target_currency_code = (
        portfolio.reporting_currency_code if legacy_target is not None else None
    )
    db.session.flush()
    return row


def _add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def _availability_age(
    as_of_date: date,
    maturity_date: date | None,
    current_age: int,
    final_age: int,
) -> int:
    if maturity_date is None or maturity_date <= as_of_date:
        return current_age
    for offset in range(1, final_age - current_age + 1):
        if _add_years(as_of_date, offset) >= maturity_date:
            return current_age + offset
    return final_age + 1


def _cash_source_state(summary: dict[str, object]) -> dict[str, object]:
    expected = [
        account
        for account in summary["account_summaries"]
        if account["cash_tracking_mode"] == "separate_cash"
        and account["cash_settlement_account_id"] is None
        and account["portfolio_share_decimal"] > 0
    ]
    unsourced = [
        account for account in expected if account["cash_balance_count"] == 0
    ]
    expected_ids = {account["account_id"] for account in expected}
    relevant_rows = [
        row for row in summary["cash_balances"] if row["account_id"] in expected_ids
    ]
    missing_rows = [row for row in relevant_rows if row["reporting_amount"] is None]
    known_count = sum(row["reporting_amount"] is not None for row in relevant_rows)
    stale_count = sum(row["status"] == "stale" for row in relevant_rows)
    unknown = bool(missing_rows or unsourced)
    if unknown and not known_count:
        status = "missing"
    elif unknown:
        status = "partial"
    elif stale_count:
        status = "stale"
    else:
        status = "current"
    return {
        "status": status,
        "unsourced_account_ids": tuple(row["account_id"] for row in unsourced),
    }


def _projection_pools(
    summary: dict[str, object],
    assumption: RetirementAssumption,
    as_of_date: date,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    pools: list[dict[str, object]] = []
    unknown_sources: list[dict[str, object]] = []
    projects_included = Decimal("0")
    projects_retirement_access = Decimal("0")
    projects_missing_count = 0
    restricted = summary["restricted_reporting_amount"] or Decimal("0")
    non_project_restricted = Decimal("0")
    future_access_amount = Decimal("0")
    future_access_sources: list[dict[str, object]] = []
    negative_cash = Decimal("0")
    stale_source_count = 0

    for cash in summary["cash_balances"]:
        if cash["portfolio_share_decimal"] == 0:
            continue
        included = cash["included_reporting_amount"]
        accessible = cash["accessible_reporting_amount"]
        restricted_amount = cash["restricted_reporting_amount"]
        access_date = cash["earliest_access_date"]
        has_future_access = access_date is not None and access_date > as_of_date
        if included is None or accessible is None or restricted_amount is None:
            unknown_sources.append(
                {
                    "source_kind": "cash",
                    "account_id": cash["account_id"],
                    "reason": "missing_value",
                }
            )
            continue
        if cash["status"] == "stale":
            stale_source_count += 1
        if accessible < 0:
            negative_cash += -accessible
        elif accessible > 0:
            pools.append(
                {
                    "source_key": (
                        f"cash:{cash['account_id']}:{cash['native_currency']}"
                    ),
                    "source_kind": "cash",
                    "source_name": (
                        f"{cash['account_name']} · {cash['native_currency']}"
                    ),
                    "bucket_code": None,
                    "equity_exposed": False,
                    "available_from_age": assumption.current_age_years,
                    "access_date": None,
                    "roles": {"liquidity": accessible},
                }
            )
        if restricted_amount < 0:
            negative_cash += -restricted_amount
        elif restricted_amount > 0:
            non_project_restricted += restricted_amount
            access_age = (
                _availability_age(
                    as_of_date,
                    access_date,
                    assumption.current_age_years,
                    assumption.final_age_years,
                )
                if has_future_access
                else assumption.final_age_years + 1
            )
            pools.append(
                {
                    "source_key": (
                        f"cash:{cash['account_id']}:{cash['native_currency']}"
                        ":restricted"
                    ),
                    "source_kind": "cash",
                    "source_name": (
                        f"{cash['account_name']} · {cash['native_currency']}"
                    ),
                    "bucket_code": None,
                    "equity_exposed": False,
                    "available_from_age": access_age,
                    "access_date": access_date,
                    "roles": {"liquidity": restricted_amount},
                }
            )
            if has_future_access:
                future_access_amount += restricted_amount
                future_access_sources.append(
                    {
                        "source_kind": "cash",
                        "source_name": (
                            f"{cash['account_name']} · "
                            f"{cash['native_currency']}"
                        ),
                        "access_date": access_date,
                        "access_age": access_age,
                        "current_amount": restricted_amount,
                    }
                )

    for holding in summary["holdings"]:
        included = holding["included_reporting_amount"]
        access_adjusted = None
        if holding["accessible_reporting_amount"] is not None:
            access_adjusted = (
                holding["accessible_reporting_amount"]
                + holding["term_liquidity_reporting_amount"]
            )
        bucket = holding["fire_bucket_code"]
        if bucket == "projects":
            if included is not None:
                projects_included += included
            else:
                projects_missing_count += 1
            if access_adjusted is not None:
                projects_retirement_access += access_adjusted
            continue
        if holding["portfolio_share_decimal"] == 0:
            continue
        restricted_amount = holding["restricted_reporting_amount"]
        access_date = holding["earliest_access_date"]
        has_future_access = access_date is not None and access_date > as_of_date
        if (
            included is None
            or access_adjusted is None
            or restricted_amount is None
        ):
            unknown_sources.append(
                {
                    "source_kind": "holding",
                    "registration_id": holding["registration_id"],
                    "reason": "missing_value",
                }
            )
            continue
        if access_adjusted == 0 and restricted_amount <= 0:
            continue
        if bucket not in RETIREMENT_BUCKETS:
            unknown_sources.append(
                {
                    "source_kind": "holding",
                    "registration_id": holding["registration_id"],
                    "reason": "missing_bucket",
                }
            )
            continue
        role_weights = holding["role_weights"]
        if not role_weights:
            unknown_sources.append(
                {
                    "source_kind": "holding",
                    "registration_id": holding["registration_id"],
                    "reason": "missing_role",
                }
            )
            continue
        fixed_deposit = holding["fixed_deposit"]
        maturity_date = None
        if fixed_deposit is not None:
            maturity_date = fixed_deposit["maturity_date"]
            if maturity_date is None:
                unknown_sources.append(
                    {
                        "source_kind": "holding",
                        "registration_id": holding["registration_id"],
                        "reason": "missing_maturity",
                    }
                )
                continue
        roles = {
            role["code"]: access_adjusted * role["weight_decimal"]
            for role in role_weights
        }
        if holding["status"] == "stale":
            stale_source_count += 1
        maturity_age = _availability_age(
            as_of_date,
            maturity_date,
            assumption.current_age_years,
            assumption.final_age_years,
        )
        if access_adjusted > 0:
            pools.append(
                {
                    "source_key": f"holding:{holding['registration_id']}",
                    "source_kind": "holding",
                    "source_name": holding["instrument_name"],
                    "bucket_code": bucket,
                    "equity_exposed": roles.get("equity", Decimal("0")) > 0,
                    "available_from_age": maturity_age,
                    "access_date": None,
                    "roles": roles,
                }
            )
        if restricted_amount > 0:
            non_project_restricted += restricted_amount
            access_age = (
                _availability_age(
                    as_of_date,
                    access_date,
                    assumption.current_age_years,
                    assumption.final_age_years,
                )
                if has_future_access
                else assumption.final_age_years + 1
            )
            future_roles = {
                role["code"]: restricted_amount * role["weight_decimal"]
                for role in role_weights
            }
            pools.append(
                {
                    "source_key": (
                        f"holding:{holding['registration_id']}:restricted"
                    ),
                    "source_kind": "holding",
                    "source_name": holding["instrument_name"],
                    "bucket_code": bucket,
                    "equity_exposed": (
                        future_roles.get("equity", Decimal("0")) > 0
                    ),
                    "available_from_age": max(maturity_age, access_age),
                    "access_date": access_date,
                    "roles": future_roles,
                }
            )
            if has_future_access:
                future_access_amount += restricted_amount
                future_access_sources.append(
                    {
                        "source_kind": "holding",
                        "source_name": holding["instrument_name"],
                        "access_date": access_date,
                        "access_age": access_age,
                        "current_amount": restricted_amount,
                    }
                )

    return pools, {
        "unknown_sources": tuple(unknown_sources),
        "negative_cash_amount": negative_cash,
        "projects_included_amount": projects_included,
        "projects_retirement_access_amount": projects_retirement_access,
        "projects_missing_count": projects_missing_count,
        "restricted_amount": restricted,
        "non_project_restricted_amount": non_project_restricted,
        "future_access_amount": future_access_amount,
        "future_access_sources": tuple(future_access_sources),
        "stale_source_count": stale_source_count,
    }


def _pool_total(pool: dict[str, object]) -> Decimal:
    return sum(pool["roles"].values(), Decimal("0"))


def _pool_order(pool: dict[str, object]) -> tuple[int, int, str]:
    return (
        0 if pool["source_kind"] == "cash" else BUCKET_ORDER[pool["bucket_code"]],
        1 if pool["equity_exposed"] else 0,
        pool["source_key"],
    )


def _remove_proportionally(
    pool: dict[str, object], amount: Decimal
) -> dict[str, Decimal]:
    total = _pool_total(pool)
    take = min(total, amount)
    if take <= 0:
        return {}
    positive_roles = [
        role for role in ASSET_ROLE_CODES if pool["roles"].get(role, 0) > 0
    ]
    if take == total:
        # Closing a source removes its exact components. Re-proportioning a full
        # sale can leave Decimal division residue that looks like spendable capital.
        removed = {role: pool["roles"][role] for role in positive_roles}
        for role in positive_roles:
            pool["roles"][role] = Decimal("0")
        return removed
    removed: dict[str, Decimal] = {}
    remaining = take
    for index, role in enumerate(positive_roles):
        role_amount = pool["roles"][role]
        role_take = (
            remaining
            if index == len(positive_roles) - 1
            else take * role_amount / total
        )
        pool["roles"][role] -= role_take
        removed[role] = role_take
        remaining -= role_take
    return removed


def _distribute(amount, weights):
    """Allocate a Decimal total with a stable residual and no negative components."""
    positive = [(key, value) for key, value in weights.items() if value > 0]
    total = sum((value for _, value in positive), Decimal("0"))
    if amount == total:
        return dict(weights)
    remaining = amount
    allocated = Decimal("0")
    result = dict.fromkeys(weights, Decimal("0"))
    for index, (key, value) in enumerate(positive):
        part = remaining if index == len(positive) - 1 else min(remaining, amount * value / total)
        result[key] = part
        # Same ordered Decimal sum; do not rescan all sources for every source.
        allocated += part
        remaining = amount - allocated
    return result


def _rebalance(pools, age, weights, *, evidence=True):
    """Fill total-portfolio deficits using only available capital."""
    locked = _role_values([pool for pool in pools if pool["available_from_age"] > age])
    available = [pool for pool in pools if pool["available_from_age"] <= age and _pool_total(pool) > 0]
    amount = sum((_pool_total(pool) for pool in available), Decimal("0"))
    total = amount + sum(locked.values(), Decimal("0"))
    deficits = {role: max(Decimal("0"), weights[role] * total - locked[role]) for role in weights}
    role_amounts = _distribute(amount, deficits)
    for pool in available:
        # Give each source the achievable mix while conserving that source's
        # own total. Subtracting previous source allocations from a final source
        # would transfer accumulated Decimal residuals between identities.
        pool["roles"] = _distribute(_pool_total(pool), role_amounts)
        pool["equity_exposed"] = pool["roles"].get("equity", Decimal("0")) > 0
    if not evidence:
        return None
    achieved = _role_values(pools)
    return {"locked_role_values": locked, "locked_amount": sum(locked.values(), Decimal("0")),
            "available_amount": amount, "target_weights": weights,
            "achieved_weights": {role: value / total if total else None for role, value in achieved.items()},
            "pre_return_role_values": achieved}


def _consume(
    pools: list[dict[str, object]], amount: Decimal, age: int, *, proportional=False, evidence=True
) -> tuple[Decimal, Decimal, tuple[dict[str, object], ...]]:
    if amount <= 0:
        return Decimal("0"), Decimal("0"), ()
    remaining = amount
    applications: list[dict[str, object]] = []
    proportional_taken = Decimal("0")
    eligible = [pool for pool in pools if pool["available_from_age"] <= age and _pool_total(pool) > 0]
    if proportional:
        totals = {index: _pool_total(pool) for index, pool in enumerate(eligible)}
        allocations = _distribute(min(amount, sum(totals.values(), Decimal("0"))), totals)
    else:
        eligible.sort(key=_pool_order)
    for index, pool in enumerate(eligible):
        if pool["available_from_age"] > age or _pool_total(pool) <= 0:
            continue
        applied = allocations[index] if proportional else min(_pool_total(pool), remaining)
        roles = _remove_proportionally(pool, applied)
        if proportional:
            proportional_taken += applied
            remaining = amount - proportional_taken
        else:
            remaining -= applied
        if evidence:
            applications.append(
                {
                    "source_key": pool["source_key"],
                    "source_kind": pool["source_kind"],
                    "source_name": pool["source_name"],
                    "bucket_code": pool["bucket_code"],
                    "equity_exposed": pool["equity_exposed"],
                    "amount": applied,
                    "role_amounts": roles,
                }
            )
        if remaining == 0:
            break
    return amount - remaining, remaining, tuple(applications)


def _role_values(pools: list[dict[str, object]]) -> dict[str, Decimal]:
    values = {role: Decimal("0") for role in ASSET_ROLE_CODES}
    for pool in pools:
        for role, amount in pool["roles"].items():
            values[role] += amount
    return values


def _bucket_values(pools: list[dict[str, object]]) -> dict[str, Decimal]:
    values = {
        "cash": Decimal("0"),
        "now": Decimal("0"),
        "bridge": Decimal("0"),
        "growth": Decimal("0"),
    }
    for pool in pools:
        key = "cash" if pool["source_kind"] == "cash" else pool["bucket_code"]
        values[key] += _pool_total(pool)
    return values


def _apply_returns(
    pools: list[dict[str, object]], returns: Mapping[str, Decimal]
) -> tuple[Decimal, dict[str, Decimal]]:
    by_role = {role: Decimal("0") for role in ASSET_ROLE_CODES}
    for pool in pools:
        for role, amount in tuple(pool["roles"].items()):
            change = amount * returns[role]
            pool["roles"][role] = amount + change
            by_role[role] += change
    return sum(by_role.values(), Decimal("0")), by_role


def _income_schedule(plan, assumption, as_of_date, currency, stale_days):
    """Only payments active at a withdrawal-year boundary enter the plan."""
    schedule, reasons = {}, []
    stale = False
    incomes = plan_incomes(plan.id)
    for age in range(assumption.current_age_years, assumption.final_age_years):
        boundary = anniversary(as_of_date, age - assumption.current_age_years)
        entries = []
        if age >= assumption.withdrawal_start_age_years:
            for income in incomes:
                if income.annual_amount == 0 or boundary < income.start_date or (income.end_date and boundary > income.end_date):
                    continue
                fx = resolve_fx(income.currency_code, currency, as_of_date, stale_days=stale_days)
                if fx.rate is None:
                    reason = {"kind": "missing_income_fx", "income_name": income.name,
                              "base_currency": income.currency_code, "quote_currency": currency}
                    if reason not in reasons:
                        reasons.append(reason)
                    continue
                stale = stale or fx.status == "stale"
                amount = income.annual_amount * (Decimal("1") + income.inflation_decimal) ** completed_years(income.start_date, boundary)
                entries.append({"name": income.name, "native_amount": amount, "currency": income.currency_code,
                                "amount": amount * fx.rate, "fx_rate": fx.rate, "fx_date": fx.effective_date,
                                "fx_path": fx.path, "status": fx.status})
        schedule[age] = tuple(entries)
    return schedule, reasons, stale


def _spend_year(pools, *, age, capital, core, flexible, income, obligation, plan,
                proportional=False, evidence=True):
    """One annual spending step, shared by the legacy and two-tier projections."""
    zero = Decimal("0")
    income_to_obligation = min(income, obligation)
    obligation -= income_to_obligation
    income_left = income - income_to_obligation
    income_to_core = min(income_left, core)
    income_left -= income_to_core
    core_need = core - income_to_core
    policy = flexible_policy(plan, capital=capital, core_need=core_need, target=flexible, income_left=income_left) if plan else {
        "allowed": flexible, "rate": (core + flexible) / capital if capital > 0 else None,
        "band": "fixed", "multiplier": Decimal("1")}
    core_withdrawn, core_shortfall, core_apps = _consume(pools, core_need, age, proportional=proportional, evidence=evidence)
    available_for_flexible = income_left + sum(
        (max(zero, _pool_total(pool)) for pool in pools if pool["available_from_age"] <= age), zero
    )
    target_resource_shortfall = max(zero, flexible - available_for_flexible)
    income_to_flexible = min(income_left, policy["allowed"])
    income_left -= income_to_flexible
    flex_withdrawn, flex_shortfall, flex_apps = _consume(pools, policy["allowed"] - income_to_flexible, age, proportional=proportional, evidence=evidence)
    if income_left > 0:
        cash_pool = next((pool for pool in pools if pool["source_key"] == "retirement:income-cash"), None)
        if cash_pool is None:
            cash_pool = {"source_key": "retirement:income-cash", "source_kind": "cash",
                         "source_name": "Saved retirement income", "bucket_code": None,
                         "equity_exposed": False, "available_from_age": age, "access_date": None,
                         "roles": {"liquidity": zero}}
            pools.append(cash_pool)
        cash_pool["roles"]["liquidity"] += income_left
    paid_core, paid_flexible = income_to_core + core_withdrawn, income_to_flexible + flex_withdrawn
    if not evidence:
        return {"core_shortfall_amount": core_shortfall, "flexible_resource_shortfall_amount": flex_shortfall,
                "remaining_obligation": obligation, "withdrawn_amount": core_withdrawn + flex_withdrawn}
    policy_cut = max(zero, flexible - policy["allowed"])
    cut = max(zero, flexible - paid_flexible)
    cut_reason = (
        "none" if cut == 0 else "resources" if policy_cut == 0
        else "rule" if target_resource_shortfall == 0 else "rule_and_resources"
    )
    return {
        "core_target_amount": core, "core_paid_amount": paid_core, "core_shortfall_amount": core_shortfall,
        "core_coverage_ratio": paid_core / core if core else None,
        "core_withdrawal_rate": core_need / capital if capital > 0 else None,
        "flexible_target_amount": flexible, "flexible_allowed_amount": policy["allowed"],
        "flexible_paid_amount": paid_flexible, "flexible_policy_cut_amount": policy_cut,
        "flexible_cut_reason": cut_reason,
        "flexible_full_target_payable": core_shortfall == 0 and target_resource_shortfall == 0,
        "flexible_target_resource_shortfall_amount": target_resource_shortfall,
        "flexible_resource_shortfall_amount": flex_shortfall,
        "flexible_cut_amount": cut,
        "full_lifestyle_withdrawal_rate": policy["rate"], "flexible_band": policy["band"],
        "flexible_multiplier": policy["multiplier"], "external_income_amount": income,
        "income_to_obligation_amount": income_to_obligation, "income_to_core_amount": income_to_core,
        "income_to_flexible_amount": income_to_flexible, "income_saved_amount": income_left,
        "withdrawn_amount": core_withdrawn + flex_withdrawn,
        "actual_withdrawal_rate": (core_withdrawn + flex_withdrawn) / capital if capital > 0 else None,
        "spending_applications": tuple({**row, "purpose": purpose} for purpose, rows in (("core", core_apps), ("flexible", flex_apps)) for row in rows),
        "remaining_obligation": obligation,
    }


def _lifestyle_metrics(years):
    rows = [row for row in years if row["phase"] == "withdrawal"]
    flexible_rows = [row for row in rows if row["flexible_target_amount"] > 0]
    cuts = [row for row in flexible_rows if row["flexible_cut_amount"] > 0]
    shortfalls = [row for row in rows if row["core_shortfall_amount"] > 0]
    streak = longest = 0
    for row in rows:
        streak = streak + 1 if row["flexible_cut_amount"] > 0 else 0
        longest = max(longest, streak)
    real_flexible = [row["flexible_base_year_amount"] for row in rows]
    coverage = [row["core_coverage_ratio"] for row in rows if row["core_coverage_ratio"] is not None]
    return {"full_budget_funded_all_years": not shortfalls and not cuts,
            "core_funded_all_years": not shortfalls if coverage else None,
            "core_shortfall_years": len(shortfalls), "minimum_core_coverage_ratio": min(coverage) if coverage else None,
            "first_core_shortfall": shortfalls[0] if shortfalls else None,
            "flexible_target_met_years": len(flexible_rows) - len(cuts), "flexible_target_years": len(flexible_rows),
            "flexible_target_met_percent": Decimal(len(flexible_rows) - len(cuts)) / Decimal(len(flexible_rows)) * 100 if flexible_rows else None,
            "flexible_cut_years": len(cuts), "longest_flexible_cut_streak": longest,
            "first_flexible_cut": cuts[0] if cuts else None,
            "minimum_flexible_base_year_amount": min(real_flexible) if real_flexible else None,
            "average_flexible_base_year_amount": sum(real_flexible, Decimal("0")) / len(real_flexible) if real_flexible else None,
            "maximum_flexible_base_year_amount": max(real_flexible) if real_flexible else None,
            "total_flexible_base_year_amount": sum(real_flexible, Decimal("0")),
            "initial_core_withdrawal_rate": rows[0]["core_withdrawal_rate"] if rows else None,
            "initial_full_lifestyle_withdrawal_rate": rows[0]["full_lifestyle_withdrawal_rate"] if rows else None}


def prepare_retirement_projection(
    portfolio: Portfolio,
    summary: dict[str, object],
    as_of_date: date,
    *,
    reporting_currency: str,
    spending_policy: str | None = None,
    plan_override=None,
) -> dict[str, object]:
    """Project one reproducible annual base case without hidden assumptions."""

    selected_currency = normalize_currency_code(
        reporting_currency, field_name="Reporting currency"
    )
    if summary["reporting_currency"] != selected_currency:
        raise ValueError("Projection and portfolio summary currencies must match.")
    plan = plan_override if plan_override is not None else adopted_plan(portfolio.id)
    if spending_policy not in (None, "full_budget", "guardrails"):
        raise ValueError("Unknown spending policy.")
    if spending_policy is not None and plan is None:
        raise ValueError("A two-tier plan is required for a policy comparison.")
    if plan:
        # Request-only overrides must never dirty the adopted ORM plan.
        plan = deepcopy(plan) if isinstance(plan, SimpleNamespace) else SimpleNamespace(**{column.name: getattr(plan, column.name) for column in plan.__table__.columns})
        if spending_policy is not None:
            plan.spending_policy = spending_policy
        if plan.spending_policy == "guardrails" and plan.lower_rate_decimal is None:
            raise ValueError("Enter spending adjustment rules before projecting them.")
    assumption = plan or retirement_assumption(portfolio.id)
    if assumption is not None and plan is None:
        assumption = SimpleNamespace(**{c.name: getattr(assumption, c.name) for c in assumption.__table__.columns})
    if plan:
        offset = completed_years(plan.base_date, as_of_date)
        assumption = SimpleNamespace(**vars(plan))
        assumption.current_age_years += max(0, offset)
    if plan_override is not None:
        fx = resolve_fx(plan.currency_code, selected_currency, as_of_date, stale_days=portfolio.fx_stale_days)
        amounts = tier_amounts(plan, as_of_date)
        native = sum(amounts.values()) if amounts is not None else None
        spending = {"reporting_amount": native * fx.rate if native is not None and fx.rate is not None else None,
                    "native_amount": native, "native_currency": plan.currency_code,
                    "reporting_currency": selected_currency, "fx_rate": fx.rate,
                    "fx_date": fx.effective_date, "status": fx.status,
                    "missing_reason": fx.missing_reason}
    else:
        spending = build_spending_value(portfolio, as_of_date, selected_currency)
    legacy_fx = None
    legacy_target_reporting = None
    if (
        assumption is not None
        and assumption.terminal_legacy_target_amount is not None
    ):
        legacy_fx = resolve_fx(
            assumption.terminal_legacy_target_currency_code,
            selected_currency,
            as_of_date,
            stale_days=portfolio.fx_stale_days,
        )
        if legacy_fx.rate is not None:
            legacy_target_reporting = (
                assumption.terminal_legacy_target_amount * legacy_fx.rate
            )
    if plan and getattr(plan, "legacy_value_basis", "nominal") == "today" and legacy_target_reporting is not None:
        end = anniversary(as_of_date, assumption.final_age_years - assumption.current_age_years)
        legacy_target_reporting *= (Decimal("1") + plan.core_inflation_decimal) ** completed_years(plan.base_date, end)
    cash_state = _cash_source_state(summary)
    reasons: list[dict[str, object]] = []
    if assumption is None:
        reasons.append({"kind": "missing_assumptions"})
    if portfolio.annual_inflation_decimal is None and plan is None:
        reasons.append({"kind": "missing_inflation"})
    if spending["reporting_amount"] is None:
        reasons.append(
            {
                "kind": "missing_spending_fx",
                "detail": spending["missing_reason"],
            }
        )
    if cash_state["status"] in {"partial", "missing"}:
        reasons.append(
            {
                "kind": "incomplete_cash",
                "account_ids": cash_state["unsourced_account_ids"],
            }
        )

    if assumption is None:
        return {
            "as_of_date": as_of_date,
            "reporting_currency": selected_currency,
            "status": "missing",
            "calculation_complete": False,
            "assumption": None,
            "spending": spending,
            "reasons": tuple(reasons),
            "years": (),
        }

    if plan and (as_of_date < plan.base_date or assumption.current_age_years >= assumption.final_age_years):
        reasons.append({"kind": "outside_plan_dates"})
    income_schedule, income_reasons, income_stale = _income_schedule(plan, assumption, as_of_date, selected_currency, portfolio.fx_stale_days) if plan else ({}, [], False)
    reasons.extend(income_reasons)
    pools, source_facts = _projection_pools(summary, assumption, as_of_date)
    if source_facts["unknown_sources"]:
        reasons.append(
            {
                "kind": "incomplete_sources",
                "sources": source_facts["unknown_sources"],
            }
        )
    incomplete = bool(reasons)
    stale = (
        source_facts["stale_source_count"] > 0
        or cash_state["status"] == "stale"
        or spending["status"] == "stale"
        or income_stale
    )
    returns = {
        role: getattr(assumption, field)
        for role, field in ROLE_RETURN_FIELDS.items()
    }
    base = {
        "as_of_date": as_of_date,
        "reporting_currency": selected_currency,
        "status": "partial" if incomplete else "stale" if stale else "current",
        "calculation_complete": not incomplete,
        "assumption": assumption,
        "plan": plan_details(plan),
        "spending_policy": plan.spending_policy if plan else "legacy",
        "role_returns": returns,
        "annual_inflation_decimal": portfolio.annual_inflation_decimal,
        "spending": spending,
        "reasons": tuple(reasons),
        "projects_included_amount": source_facts["projects_included_amount"],
        "projects_retirement_access_amount": source_facts[
            "projects_retirement_access_amount"
        ],
        "projects_missing_count": source_facts["projects_missing_count"],
        "restricted_amount": source_facts["restricted_amount"],
        "non_project_restricted_amount": source_facts[
            "non_project_restricted_amount"
        ],
        "future_access_amount": source_facts["future_access_amount"],
        "future_access_sources": source_facts["future_access_sources"],
        "opening_negative_cash_amount": source_facts["negative_cash_amount"],
        "years": (),
        "depletion_age": None,
        "first_shortfall_age": None,
        "first_drawdown_age": None,
        "first_drawdown_calendar_year": None,
        "initial_withdrawal_rate": None,
        "final_value_amount": None,
        "legacy_target_state": None,
        "legacy_target_gap_amount": None,
        "legacy_target_reporting_amount": legacy_target_reporting,
        "legacy_target_status": legacy_fx.status if legacy_fx is not None else None,
        "legacy_target_fx_date": (
            legacy_fx.effective_date if legacy_fx is not None else None
        ),
    }
    if incomplete:
        return base

    base["_inputs"] = {
        "pools": pools,
        "income_schedule": {str(age): rows for age, rows in income_schedule.items()},
        "annual_spending": spending["reporting_amount"],
        "inflation": portfolio.annual_inflation_decimal,
        "plan": plan,
    }
    if plan and getattr(plan, "planning_mode", "budget") == "affordability":
        base["_inputs"]["annual_savings_amount"] = plan.annual_savings_amount * spending["fx_rate"]
        positive_total = sum((_pool_total(pool) for pool in pools), Decimal("0"))
        base["_inputs"]["savings_weights"] = {
            role: amount / positive_total for role, amount in _role_values(pools).items()
        } if positive_total > 0 else {}
        # New contributions mirror the initial source mix, including its buckets.
        # Separate simulated pools keep original records/access restrictions intact.
        base["_inputs"]["savings_allocation"] = tuple({
            "source_key": "retirement:savings:" + pool["source_key"],
            "source_name": "Working-year savings — " + pool["source_name"],
            "source_kind": pool["source_kind"], "bucket_code": pool["bucket_code"],
            "equity_exposed": pool["equity_exposed"],
            "weights": {role: amount / positive_total for role, amount in pool["roles"].items()}
        } for pool in pools if _pool_total(pool) > 0) if positive_total > 0 else ()
        if plan.annual_savings_amount > 0 and assumption.current_age_years < assumption.withdrawal_start_age_years and not positive_total:
            base["calculation_complete"] = False
            base["reasons"] += ({"kind": "missing_savings_mix"},)
            base["status"] = "missing"
    return base


def build_retirement_projection(portfolio, summary, as_of_date, *, reporting_currency,
                                spending_policy=None):
    return run_retirement_projection(prepare_retirement_projection(
        portfolio, summary, as_of_date, reporting_currency=reporting_currency,
        spending_policy=spending_policy))


def run_retirement_projection(basis, annual_returns=()):
    return _run_retirement_projection(basis, annual_returns)


def run_generated_projection(basis, annual_returns, weights, *, compact=False):
    """Generated-path contract: four finite Decimal returns >= -100%, no upper cap.

    The caller supplies a detached, explicitly mapped Monte Carlo basis. This
    entry point never changes validation or replay for historical manual paths.
    """
    if set(weights) != set(ROLE_RETURN_FIELDS) or any(
        not isinstance(w, Decimal) or not w.is_finite() or w < 0 or w > 1 for w in weights.values()
    ) or sum(weights.values(), Decimal("0")) != 1:
        raise ValueError("Allocation weights must be finite Decimals totalling 100%.")
    if basis.get("_inputs", {}).get("plan") is None or basis["_inputs"]["plan"].spending_policy != "full_budget":
        raise ValueError("Generated comparisons require an explicit full-lifestyle plan.")
    if len(annual_returns) != basis["assumption"].final_age_years - basis["assumption"].current_age_years:
        raise ValueError("Generated returns must cover the whole horizon.")
    return _run_retirement_projection(basis, annual_returns, target_weights=weights, compact=compact)


def _run_retirement_projection(basis, annual_returns=(), *, target_weights=None, compact=False):
    """Run the shared annual engine from detached inputs; never read live records.

    Each supplied row replaces all four returns after that year's spending.
    Remaining years use the frozen baseline returns. Inputs are never mutated.
    """
    base = dict(basis)
    inputs = base.pop("_inputs", None)
    if not base["calculation_complete"]:
        return base
    assumption = base["assumption"]
    horizon = assumption.final_age_years - assumption.current_age_years
    if len(annual_returns) > horizon:
        raise ValueError("Return path extends beyond the plan horizon.")
    for row in annual_returns:
        if set(row) != set(ROLE_RETURN_FIELDS) or any(
            not isinstance(value, Decimal) or not value.is_finite() or value < -1 or (target_weights is None and value > 1)
            for value in row.values()
        ):
            raise ValueError("Each year requires four finite Decimal returns in the supported range.")
    pools = [{**pool, "roles": dict(pool["roles"])} for pool in inputs["pools"]]
    income_schedule = inputs["income_schedule"]
    annual_spending = inputs["annual_spending"]
    inflation = inputs["inflation"]
    plan = inputs["plan"]
    spending = base["spending"]
    as_of_date = base["as_of_date"]
    returns = base["role_returns"]
    legacy_target_reporting = base["legacy_target_reporting_amount"]
    outstanding_opening_obligation = base["opening_negative_cash_amount"]
    years: list[dict[str, object]] = []
    depletion_age = None
    first_shortfall_age = None
    first_drawdown_age = None
    first_drawdown_calendar_year = None
    initial_withdrawal_rate = None

    for age in range(assumption.current_age_years, assumption.final_age_years):
        elapsed = age - assumption.current_age_years
        starting_role_values = _role_values(pools)
        starting_assets = sum(starting_role_values.values(), Decimal("0"))
        starting_value = starting_assets - outstanding_opening_obligation

        obligation_applied, outstanding_opening_obligation, obligation_apps = _consume(
            pools, outstanding_opening_obligation, age, proportional=target_weights is not None, evidence=not compact
        )
        boundary = anniversary(as_of_date, elapsed)
        tiers = inputs["annual_tiers"][elapsed] if "annual_tiers" in inputs else tier_amounts(plan, boundary) if plan else {"core": annual_spending * ((Decimal("1") + inflation) ** elapsed), "flexible": Decimal("0")}
        if plan:
            tiers = {tier: amount * spending["fx_rate"] for tier, amount in tiers.items()}
        if age < assumption.withdrawal_start_age_years:
            tiers = {"core": Decimal("0"), "flexible": Decimal("0")}
        spending_required = tiers["core"] + tiers["flexible"]
        if (
            age == assumption.withdrawal_start_age_years
            and starting_value > 0
        ):
            initial_withdrawal_rate = spending_required / starting_value
        income_entries = income_schedule.get(str(age), ())
        lifestyle = _spend_year(pools, age=age, capital=starting_value, core=tiers["core"],
                                flexible=tiers["flexible"], income=sum((row["amount"] for row in income_entries), Decimal("0")),
                                obligation=outstanding_opening_obligation, plan=plan,
                                proportional=target_weights is not None, evidence=not compact)
        outstanding_opening_obligation = lifestyle["remaining_obligation"]
        withdrawn = lifestyle["withdrawn_amount"]
        spending_shortfall = lifestyle["core_shortfall_amount"] + lifestyle["flexible_resource_shortfall_amount"]
        spending_apps = lifestyle.get("spending_applications", ())
        if spending_shortfall > 0 and first_shortfall_age is None:
            first_shortfall_age = age

        pre_return_assets = sum(_role_values(pools).values(), Decimal("0"))
        rebalance = _rebalance(pools, age, target_weights, evidence=not compact) if target_weights is not None else None
        year_returns = annual_returns[elapsed] if elapsed < len(annual_returns) else returns
        return_amount, return_by_role = _apply_returns(pools, year_returns)
        annual_surplus_drawdown = return_amount - spending_required
        if (
            age >= assumption.withdrawal_start_age_years
            and annual_surplus_drawdown < 0
            and first_drawdown_age is None
        ):
            first_drawdown_age = age
            first_drawdown_calendar_year = as_of_date.year + elapsed
        savings = Decimal("0")
        if "annual_savings_amount" in inputs and age < assumption.withdrawal_start_age_years:
            savings = inputs["annual_savings_amount"]
            if savings > 0 and target_weights is not None:
                pool = next((pool for pool in pools if pool["source_key"] == "retirement:mc-savings"), None)
                if pool is None:
                    pool = {"source_key": "retirement:mc-savings", "source_kind": "cash",
                            "source_name": "Working-year savings", "bucket_code": None,
                            "equity_exposed": False, "available_from_age": age + 1,
                            "access_date": None, "roles": {"liquidity": Decimal("0")}}
                    pools.append(pool)
                pool["roles"]["liquidity"] = pool["roles"].get("liquidity", Decimal("0")) + savings
            elif savings > 0:
                entries = [(allocation, role, weight)
                           for allocation in inputs["savings_allocation"]
                           for role, weight in allocation["weights"].items() if weight > 0]
                remaining = savings
                for index, (allocation, role, weight) in enumerate(entries):
                    pool = next((pool for pool in pools if pool["source_key"] == allocation["source_key"]), None)
                    if pool is None:
                        pool = {key: allocation[key] for key in (
                            "source_key", "source_kind", "source_name", "bucket_code", "equity_exposed")}
                        pool.update(available_from_age=age + 1, access_date=None,
                                    roles={role: Decimal("0") for role in ROLE_RETURN_FIELDS})
                        pools.append(pool)
                    amount = remaining if index == len(entries) - 1 else savings * weight
                    pool["roles"][role] += amount
                    remaining -= amount
        ending_role_values = _role_values(pools)
        ending_assets = sum(ending_role_values.values(), Decimal("0"))
        ending_value = ending_assets - outstanding_opening_obligation
        if compact:
            years.append({"ending_value_amount": ending_value,
                          "core_shortfall_amount": lifestyle["core_shortfall_amount"],
                          "flexible_resource_shortfall_amount": lifestyle["flexible_resource_shortfall_amount"],
                          "opening_obligation_remaining_amount": outstanding_opening_obligation})
            continue
        if depletion_age is None:
            if spending_required > 0 and ending_assets == 0:
                depletion_age = age
            elif pre_return_assets > 0 and ending_assets == 0:
                depletion_age = age + 1

        role_percentages = {
            role: (
                amount / ending_assets if ending_assets != 0 else None
            )
            for role, amount in ending_role_values.items()
        }
        applications = obligation_apps + spending_apps
        years.append(
            {
                **lifestyle,
                **({"rebalance": rebalance,
                     "source_balances": tuple({**pool, "roles": dict(pool["roles"]), "total_amount": _pool_total(pool)} for pool in pools)} if target_weights is not None else {}),
                **({"savings_added_amount": savings} if "annual_savings_amount" in inputs else {}),
                "age": age,
                "boundary_date": boundary,
                "income_sources": income_entries,
                "flexible_base_year_amount": lifestyle["flexible_paid_amount"] / ((Decimal("1") + plan.flexible_inflation_decimal) ** completed_years(plan.base_date, boundary)) if plan else Decimal("0"),
                "calendar_year": as_of_date.year + elapsed,
                "phase": (
                    "withdrawal"
                    if age >= assumption.withdrawal_start_age_years
                    else "accumulation"
                ),
                "starting_value_amount": starting_value,
                "opening_obligation_applied_amount": obligation_applied,
                "opening_obligation_remaining_amount": (
                    outstanding_opening_obligation
                ),
                "spending_required_amount": spending_required,
                "withdrawn_amount": withdrawn,
                "shortfall_amount": spending_shortfall,
                "return_amount": return_amount,
                "return_by_role": return_by_role,
                "role_returns": dict(year_returns),
                "annual_surplus_drawdown_amount": annual_surplus_drawdown,
                "ending_value_amount": ending_value,
                "ending_role_values": ending_role_values,
                "ending_role_percentages": role_percentages,
                "ending_bucket_values": _bucket_values(pools),
                "applications": applications,
                "buckets_used": tuple(
                    dict.fromkeys(
                        "cash" if item["source_kind"] == "cash" else item["bucket_code"]
                        for item in applications
                    )
                ),
                "equity_reliance_amount": sum(
                    (
                        item["amount"]
                        for item in applications
                        if item["equity_exposed"]
                    ),
                    Decimal("0"),
                ),
            }
        )

    final_value = years[-1]["ending_value_amount"] if years else Decimal("0")
    if compact:
        return {"years": tuple(years), "final_value_amount": final_value}
    legacy_target = legacy_target_reporting
    legacy_state = None
    legacy_gap = None
    if legacy_target is not None:
        legacy_gap = final_value - legacy_target
        legacy_state = "met" if legacy_gap >= 0 else "shortfall"
    return {
        **base,
        "lifestyle": _lifestyle_metrics(years),
        "years": tuple(years),
        "depletion_age": depletion_age,
        "first_shortfall_age": first_shortfall_age,
        "first_drawdown_age": first_drawdown_age,
        "first_drawdown_calendar_year": first_drawdown_calendar_year,
        "initial_withdrawal_rate": initial_withdrawal_rate,
        "final_value_amount": final_value,
        "final_role_values": years[-1]["ending_role_values"] if years else {},
        "final_bucket_values": years[-1]["ending_bucket_values"] if years else {},
        "legacy_target_state": legacy_state,
        "legacy_target_gap_amount": legacy_gap,
    }
