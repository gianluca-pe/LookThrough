"""Cash-first bucket reserves in as-of-date purchasing power."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from app.conventions import normalize_currency_code
from app.models import Portfolio
from app.services.spending import build_spending_value
from app.services.retirement_plans import adopted_plan, plan_details


PERIODS = (
    {"code": "now", "start_year": 1, "end_year": 3},
    {"code": "bridge", "start_year": 4, "end_year": 10},
)
RETIREMENT_BUCKETS = ("now", "bridge", "growth")
BUCKET_ORDER = {"now": 0, "bridge": 1, "growth": 2}


def _add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        # 29 February uses the last valid February day in the target year.
        return value.replace(year=value.year + years, day=28)


def _cash_source_state(summary: dict[str, object]) -> dict[str, object]:
    expected_accounts = [
        account
        for account in summary["account_summaries"]
        if account["cash_tracking_mode"] == "separate_cash"
        and account["cash_settlement_account_id"] is None
    ]
    unsourced_accounts = [
        account
        for account in expected_accounts
        if account["cash_balance_count"] == 0
    ]
    unknown = bool(summary["cash_missing_count"] or unsourced_accounts)
    known = bool(summary["cash_reporting_valued_count"])
    if unknown and not known:
        status = "missing"
    elif unknown:
        status = "partial"
    elif summary["cash_stale_count"]:
        status = "stale"
    elif not expected_accounts and not summary["cash_balance_count"]:
        # A portfolio with no separate-cash account has a real zero separate-cash
        # pool. Aggregate wrappers remain available through their holding bucket.
        status = "current"
    else:
        status = "current"
    amount = (
        summary["role_accessible_cash_amount"]
        if known or status == "current"
        else None
    )
    return {
        "amount": amount,
        "status": status,
        "unsourced_account_ids": tuple(
            account["account_id"] for account in unsourced_accounts
        ),
    }


def _holding_availability_rank(
    holding: dict[str, object], now_end: date, bridge_end: date
) -> int | None:
    fixed_deposit = holding["fixed_deposit"]
    if fixed_deposit is None:
        return 0
    maturity_date = fixed_deposit["maturity_date"]
    if maturity_date is None:
        return None
    if maturity_date <= now_end:
        return 0
    if maturity_date <= bridge_end:
        return 1
    return 2


def _holding_pools(
    summary: dict[str, object], as_of_date: date
) -> tuple[list[dict[str, object]], dict[str, object]]:
    now_end = _add_years(as_of_date, 3)
    bridge_end = _add_years(as_of_date, 10)
    amounts: defaultdict[tuple[str, bool | None, int], Decimal] = defaultdict(
        lambda: Decimal("0")
    )
    projects_included = Decimal("0")
    projects_access_adjusted = Decimal("0")
    unknown_sources: list[dict[str, object]] = []
    stale_sources: list[int] = []

    for holding in summary["holdings"]:
        bucket = holding["fire_bucket_code"]
        included = holding["included_reporting_amount"]
        if bucket == "projects":
            if included is not None:
                projects_included += included
                projects_access_adjusted += (
                    holding["accessible_reporting_amount"]
                    + holding["term_liquidity_reporting_amount"]
                )
            continue
        if included is None:
            unknown_sources.append({
                "instrument_id": holding["instrument_id"],
                "reason": "missing_value",
            })
            continue
        if bucket not in RETIREMENT_BUCKETS:
            if included != 0:
                unknown_sources.append({
                    "instrument_id": holding["instrument_id"],
                    "reason": "missing_bucket",
                })
            continue

        access_adjusted = (
            holding["accessible_reporting_amount"]
            + holding["term_liquidity_reporting_amount"]
        )
        if access_adjusted == 0:
            continue
        availability_rank = _holding_availability_rank(
            holding, now_end, bridge_end
        )
        if availability_rank is None:
            unknown_sources.append({
                "instrument_id": holding["instrument_id"],
                "reason": "missing_maturity",
            })
            continue
        role_weights = holding["role_weights"]
        equity_exposed = (
            None
            if not role_weights
            else any(role["code"] == "equity" for role in role_weights)
        )
        if equity_exposed is None:
            unknown_sources.append({
                "instrument_id": holding["instrument_id"],
                "reason": "missing_role",
            })
        amounts[(bucket, equity_exposed, availability_rank)] += access_adjusted
        if holding["status"] == "stale":
            stale_sources.append(holding["registration_id"])

    pools = [
        {
            "source_kind": "bucket",
            "bucket_code": bucket,
            "equity_exposed": equity_exposed,
            "availability_rank": availability_rank,
            "amount": amount,
        }
        for (bucket, equity_exposed, availability_rank), amount in amounts.items()
        if amount > 0
    ]
    return pools, {
        "projects_included_amount": projects_included,
        "projects_access_adjusted_amount": projects_access_adjusted,
        "unknown_sources": tuple(unknown_sources),
        "stale_registration_ids": tuple(dict.fromkeys(stale_sources)),
        "now_end_date": now_end,
        "bridge_end_date": bridge_end,
    }


def _pool_order(pool: dict[str, object]) -> tuple[int, int, int]:
    equity_order = {False: 0, None: 1, True: 2}
    return (
        0 if pool["source_kind"] == "cash" else 1 + BUCKET_ORDER[pool["bucket_code"]],
        equity_order[pool["equity_exposed"]],
        pool["availability_rank"],
    )


def _apply_period(
    period: dict[str, object],
    pools: list[dict[str, object]],
    *,
    period_rank: int,
    cash_deficit: Decimal,
) -> dict[str, object]:
    spending_requirement = period["spending_requirement_amount"]
    required = spending_requirement + (cash_deficit if period_rank == 0 else 0)
    # Assess uncapped reserves before this period consumes them. Cash is assigned
    # only up to this period's requirement; its remainder belongs to later periods.
    # Bridge sees only balances left by Now, so carry is never counted twice.
    cash_assigned = min(required, sum(
        (pool["amount"] for pool in pools if pool["source_kind"] == "cash"),
        Decimal("0"),
    ))
    own_reserves = sum(
        (pool["amount"] for pool in pools
         if pool["bucket_code"] == period["code"]
         and pool["availability_rank"] <= period_rank), Decimal("0"),
    )
    earlier_reserves = sum(
        (pool["amount"] for pool in pools
         if pool["source_kind"] == "bucket"
         and BUCKET_ORDER[pool["bucket_code"]] < period_rank
         and pool["availability_rank"] <= period_rank), Decimal("0"),
    )
    assigned_reserves = own_reserves + cash_assigned
    eligible_reserves = assigned_reserves + earlier_reserves
    remaining = required
    applications: list[dict[str, object]] = []
    for pool in sorted(pools, key=_pool_order):
        if pool["availability_rank"] > period_rank or pool["amount"] <= 0:
            continue
        applied = min(pool["amount"], remaining)
        if applied <= 0:
            continue
        pool["amount"] -= applied
        remaining -= applied
        bucket = pool["bucket_code"]
        later_period = (
            period["code"] == "now" and bucket in {"bridge", "growth"}
        ) or (period["code"] == "bridge" and bucket == "growth")
        applications.append({
            "source_kind": pool["source_kind"],
            "bucket_code": bucket,
            "equity_exposed": pool["equity_exposed"],
            "amount": applied,
            "later_period": later_period,
        })
        if remaining == 0:
            break
    funded = required - remaining
    return {
        **period,
        "cash_deficit_amount": cash_deficit if period_rank == 0 else Decimal("0"),
        "required_amount": required,
        "own_bucket_reserve_amount": own_reserves,
        "cash_assigned_amount": cash_assigned,
        "assigned_reserve_amount": assigned_reserves,
        "assigned_reserve_balance_amount": assigned_reserves - required,
        "earlier_reserve_amount": earlier_reserves,
        "eligible_reserve_amount": eligible_reserves,
        "reserve_surplus_amount": max(eligible_reserves - required, Decimal("0")),
        "reserve_gap_amount": max(required - eligible_reserves, Decimal("0")),
        "funded_amount": funded,
        "shortfall_amount": remaining,
        "is_funded": remaining == 0,
        "applications": tuple(applications),
        "cash_applied_amount": sum(
            (item["amount"] for item in applications if item["source_kind"] == "cash"),
            Decimal("0"),
        ),
        "later_period_dependency_amount": sum(
            (item["amount"] for item in applications if item["later_period"]),
            Decimal("0"),
        ),
        "equity_dependency_amount": sum(
            (item["amount"] for item in applications if item["equity_exposed"] is True),
            Decimal("0"),
        ),
        "unknown_role_dependency_amount": sum(
            (item["amount"] for item in applications if item["equity_exposed"] is None),
            Decimal("0"),
        ),
    }


def build_funding_summary(
    portfolio: Portfolio,
    summary: dict[str, object],
    as_of_date: date,
    *,
    reporting_currency: str,
) -> dict[str, object]:
    """Return a traceable ten-year funding waterfall without reclassification."""

    selected_currency = normalize_currency_code(
        reporting_currency, field_name="Reporting currency"
    )
    if summary["reporting_currency"] != selected_currency:
        raise ValueError("Funding and portfolio summary reporting currencies must match")

    spending = build_spending_value(portfolio, as_of_date, selected_currency, core_only=True)
    inflation = portfolio.annual_inflation_decimal
    plan = adopted_plan(portfolio.id)
    # Freeze the adopted annual budget at the selected date. This reserve check
    # assumes zero real return; the retirement engine owns nominal projections.
    schedule = (spending['reporting_amount'],) * 10 if spending['reporting_amount'] is not None else None
    cash = _cash_source_state(summary)
    holding_pools, source_facts = _holding_pools(summary, as_of_date)
    attention_items: list[dict[str, object]] = []

    if spending["status"] in {"missing", "stale"}:
        attention_items.append({
            "kind": spending["status"],
            "subject_kind": "annual_spending_fx",
            "action_kind": "update_value",
            "action_source": "fx",
            "base_currency": spending["native_currency"],
            "quote_currency": selected_currency,
            "detail": (
                spending["missing_reason"]
                if spending["status"] == "missing"
                else "The annual spending FX rate is older than the freshness setting."
            ),
        })

    requirements_available = schedule is not None
    if not requirements_available:
        status = "missing"
        period_results: tuple[dict[str, object], ...] = ()
        growth_remainder = None
        assessment = None
        cash_deficit = None
        unknown_role_dependency_total = None
    else:
        requirements = tuple({**period,
            "annual_amounts": schedule[period["start_year"] - 1:period["end_year"]],
            "spending_requirement_amount": sum(schedule[period["start_year"] - 1:period["end_year"]], Decimal("0"))}
            for period in PERIODS)
        cash_amount = cash["amount"] or Decimal("0")
        cash_deficit = -cash_amount if cash_amount < 0 else Decimal("0")
        pools = [
            {
                "source_kind": "cash",
                "bucket_code": None,
                "equity_exposed": False,
                "availability_rank": 0,
                "amount": max(cash_amount, Decimal("0")),
            },
            *holding_pools,
        ]
        # Snapshot each bucket before any cross-bucket spending. The chart tests
        # the current placement; the subsequent waterfall explains transfers.
        allocation_capital = sum((pool['amount'] for pool in pools), Decimal('0'))
        bucket_targets = []
        cash_for_targets = max(cash_amount, Decimal('0'))
        for rank, period in enumerate(requirements):
            required = period['spending_requirement_amount'] + (cash_deficit if rank == 0 else 0)
            assigned_cash = min(cash_for_targets, required)
            cash_for_targets -= assigned_cash
            bucket_amount = sum((p['amount'] for p in pools
                                 if p['bucket_code'] == period['code'] and p['availability_rank'] <= rank), Decimal('0'))
            current = bucket_amount + assigned_cash
            bucket_targets.append({'code': period['code'], 'required_amount': required,
                                   'current_percent': None, 'required_percent': None,
                                   'bucket_amount': bucket_amount, 'cash_assigned_amount': assigned_cash,
                                   'current_amount': current, 'surplus_amount': max(current-required, Decimal('0')),
                                   'gap_amount': max(required-current, Decimal('0'))})
        earlier_reserves = sum(
            (pool["amount"] for pool in pools
             if pool["bucket_code"] != "growth"
             and pool["availability_rank"] <= 1), Decimal("0"),
        )
        period_results_list = []
        for period_rank, period in enumerate(requirements):
            period_results_list.append(
                _apply_period(
                    period,
                    pools,
                    period_rank=period_rank,
                    cash_deficit=cash_deficit,
                )
            )
        period_results = tuple(period_results_list)
        growth_remainder = sum(
            (pool["amount"] for pool in pools), Decimal("0")
        )
        required_total = sum(
            (period["required_amount"] for period in period_results), Decimal("0")
        )
        growth_applied = sum(
            (item["amount"] for period in period_results
             for item in period["applications"] if item["bucket_code"] == "growth"),
            Decimal("0"),
        )
        shortfall = sum(
            (period["shortfall_amount"] for period in period_results), Decimal("0")
        )
        assessment = {
            "allocation_capital_amount": allocation_capital,
            "bucket_targets": tuple(bucket_targets),
            "required_amount": required_total,
            "earlier_reserve_amount": earlier_reserves,
            "earlier_reserve_applied_amount": required_total - growth_applied - shortfall,
            "growth_applied_amount": growth_applied,
            "shortfall_amount": shortfall,
            "covered_without_growth": growth_applied == 0 and shortfall == 0,
            "remainder_sources": tuple({
                "code": code,
                "amount": sum(
                    (pool["amount"] for pool in pools if pool["bucket_code"] == code
                     and pool["availability_rank"] <= 1), Decimal("0"),
                ),
            } for code in (None, "now", "bridge")),
            "late_earlier_reserve_amount": sum(
                (pool["amount"] for pool in pools
                 if pool["bucket_code"] in {"now", "bridge"}
                 and pool["availability_rank"] > 1), Decimal("0"),
            ),
            "growth_remaining_amount": sum(
                (pool["amount"] for pool in pools if pool["bucket_code"] == "growth"),
                Decimal("0"),
            ),
        }
        unknown_role_dependency_total = sum(
            (period["unknown_role_dependency_amount"] for period in period_results),
            Decimal("0"),
        )
        incomplete = (
            cash["status"] in {"missing", "partial"}
            or bool(source_facts["unknown_sources"])
        )
        stale = (
            cash["status"] == "stale"
            or spending["status"] == "stale"
            or bool(source_facts["stale_registration_ids"])
        )
        status = "partial" if incomplete else "stale" if stale else "current"
        if not incomplete and allocation_capital > 0:
            for row in bucket_targets:
                row['current_percent'] = row['current_amount'] * 100 / allocation_capital
                row['required_percent'] = row['required_amount'] * 100 / allocation_capital

    return {
        "as_of_date": as_of_date,
        "reporting_currency": selected_currency,
        "money_basis": "as_of_purchasing_power",
        "status": status,
        "calculation_complete": status in {"current", "stale"},
        "spending": spending,
        "annual_inflation_decimal": inflation,
        "retirement_plan": plan_details(plan),
        "cash": cash,
        "cash_deficit_amount": cash_deficit,
        "periods": period_results,
        # Historical aggregate retained for existing consumers; the assessment
        # separates its sources and must be used for Growth-specific presentation.
        "growth_remainder_amount": growth_remainder,
        "assessment": assessment,
        "unknown_role_dependency_total_amount": unknown_role_dependency_total,
        "projects_included_amount": source_facts["projects_included_amount"],
        "projects_access_adjusted_amount": source_facts[
            "projects_access_adjusted_amount"
        ],
        "unknown_sources": source_facts["unknown_sources"],
        "stale_registration_ids": source_facts["stale_registration_ids"],
        "now_end_date": source_facts["now_end_date"],
        "bridge_end_date": source_facts["bridge_end_date"],
        "attention_items": tuple(attention_items),
    }
