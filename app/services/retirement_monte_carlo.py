"""Paired USD experiments through the shared Decimal annual engine.

No database access, adoption, random global state or portfolio-derived seed.
"""

from copy import deepcopy
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from hashlib import sha256
from random import Random

from app.services.retirement import (
    ROLE_RETURN_FIELDS, _distribute, _pool_total, _role_values, run_generated_projection,
)
from app.services.retirement_plans import anniversary, plan_details, tier_amounts

ROLES = tuple(ROLE_RETURN_FIELDS)
ZERO = Decimal("0")
ONE = Decimal("1")
PATH_COUNT = 1000
MODEL_VERSION = "usd-lognormal-v2-preset-equity"
# Preserve the original underlying draws when recalibrating the risk profile.
DRAW_VERSION = "usd-lognormal-v1-decimal28-polar-mt19937"
SEED = 53001
CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)
EQUITY_ANNUAL_VOLATILITY = Decimal(".163")
FIXED_LOG_VOLATILITIES = dict(income=Decimal(".06"), liquidity=ZERO, alternatives=Decimal(".16"))
EXPOSURES = dict(zip(ROLES, ("Global shares in USD", "USD investment-grade aggregate bonds",
                           "Cash / deposits at a fixed return", "Gold in USD")))


class MonteCarloValidationError(ValueError):
    pass


def allocation_weights(percentages):
    if set(percentages) != set(ROLES) or any(
        not isinstance(v, Decimal) or not v.is_finite() or v < 0 or v > 100 for v in percentages.values()
    ) or sum(percentages.values(), ZERO) != 100:
        raise MonteCarloValidationError("Enter four allocation percentages between 0 and 100, totalling exactly 100%.")
    return _distribute(ONE, percentages)


def _log_volatilities(growth):
    """Match equity's ordinary annual-return SD without changing compound growth.

    For ln(1+r) = ln(1+g) + s*z, set q=exp(s²). Then
    variance(r) = (1+g)²*q*(q-1), so q solves a quadratic.
    Preset broad-market equity annual volatility: 16.3%.
    """
    with localcontext(CONTEXT):
        relative_variance = (EQUITY_ANNUAL_VOLATILITY / (ONE + growth['equity'])) ** 2
        q = (ONE + (ONE + 4 * relative_variance).sqrt()) / 2
        return {'equity': q.ln().sqrt(), **FIXED_LOG_VOLATILITIES}


def prepare_experiment(basis, summary, flexible):
    """Map facts explicitly without changing the reviewed or persisted basis."""
    with localcontext(CONTEXT):
        if basis["reporting_currency"] != "USD":
            raise MonteCarloValidationError("This risk profile models USD outcomes. Review a USD retirement projection to compare allocations.")
        if not basis["calculation_complete"]:
            raise MonteCarloValidationError("Complete the missing portfolio, income or FX inputs before running a comparison.")
        if not isinstance(flexible, Decimal) or not flexible.is_finite() or flexible < 0:
            raise MonteCarloValidationError("Flexible spending must be a finite amount of zero or more.")
        if any(not g.is_finite() or g <= -1 for g in basis["role_returns"].values()):
            raise MonteCarloValidationError("Compound return assumptions must be greater than -100%. Update the retirement projection.")
        candidate = deepcopy(basis)
        inputs = candidate["_inputs"]
        recorded = _role_values(inputs["pools"])
        fd_ids = {str(row["registration_id"]) for row in summary["holdings"] if row["fixed_deposit"] is not None}
        adjustments = []
        for pool in inputs["pools"]:
            is_fd = pool["source_kind"] == "holding" and pool["source_key"].split(":")[1] in fd_ids
            pool["is_fixed_deposit"] = is_fd
            if is_fd or pool["source_kind"] == "cash":
                before = pool["roles"].copy()
                pool["roles"] = dict.fromkeys(ROLES, ZERO)
                pool["roles"]["liquidity"] = sum(before.values(), ZERO)
                pool["equity_exposed"] = False
                if is_fd:
                    adjustments.append({"source_name": pool["source_name"], "source_key": pool["source_key"],
                                        "amount": _pool_total(pool), "recorded_roles": before})
        model = _role_values(inputs["pools"])
        total = sum(model.values(), ZERO)
        if total <= 0:
            raise MonteCarloValidationError("A current allocation needs positive non-Project starting assets. Update the retirement projection after recording them.")
        percentages = _distribute(Decimal("100"), model)
        plan = inputs["plan"]
        plan.flexible_amount = flexible
        plan.spending_policy = "full_budget"
        candidate["assumption"].flexible_amount = flexible
        candidate["assumption"].spending_policy = "full_budget"
        candidate["plan"] = plan_details(plan)
        candidate["spending_policy"] = "full_budget"
        candidate["spending"].update(native_amount=plan.core_amount + flexible,
                                      reporting_amount=plan.core_amount + flexible)
        horizon = plan.final_age_years - plan.current_age_years
        inputs["annual_tiers"] = tuple(tier_amounts(plan, anniversary(basis["as_of_date"], n)) for n in range(horizon))
        locked = sum((_pool_total(p) for p in inputs["pools"] if p["available_from_age"] > plan.current_age_years), ZERO)
        log_volatilities = _log_volatilities(basis["role_returns"])
        profile = []
        for role in ROLES:
            growth = basis["role_returns"][role]
            log_volatility = log_volatilities[role]
            q = (log_volatility ** 2).exp()
            annual_volatility = EQUITY_ANNUAL_VOLATILITY if role == 'equity' else (ONE + growth) * (q * (q - ONE)).sqrt()
            profile.append({"role": role, "exposure": EXPOSURES[role], "growth": growth,
                            "volatility": annual_volatility, "log_volatility": log_volatility})
        return {"basis": candidate, "current_weights": allocation_weights(percentages),
                "current_percentages": percentages, "recorded_percentages": _distribute(Decimal("100"), recorded),
                "adjustments": adjustments, "starting_assets": total,
                "starting_capital": total - basis["opening_negative_cash_amount"], "locked_amount": locked,
                "model_version": MODEL_VERSION, "seed": SEED,
                "profile": profile}


def _normal_pair(rng):
    # Marsaglia polar transform; integer draws -> Decimal, including ln/sqrt.
    # Midpoint uniforms avoid endpoint singularities. No binary-float arithmetic.
    denominator = Decimal(2**64)
    while True:
        x = (Decimal(rng.getrandbits(64)) + Decimal(".5")) / denominator * 2 - 1
        y = (Decimal(rng.getrandbits(64)) + Decimal(".5")) / denominator * 2 - 1
        radius = x*x + y*y
        if 0 < radius < 1:
            factor = (-2 * radius.ln() / radius).sqrt()
            return x * factor, y * factor


def return_path(growth, horizon, path_number, *, volatilities=None):
    """Stable one-based path IDs; a longer horizon retains the same prefix."""
    if not isinstance(path_number, int) or path_number < 1:
        raise MonteCarloValidationError("Choose a positive path number.")
    with localcontext(CONTEXT):
        if set(growth) != set(ROLES) or any(not isinstance(g, Decimal) or not g.is_finite() or g <= -1 for g in growth.values()):
            raise MonteCarloValidationError("Four finite compound returns greater than -100% are required.")
        vol = _log_volatilities(growth) if volatilities is None else volatilities
        if set(vol) != set(ROLES) or any(not isinstance(v, Decimal) or not v.is_finite() or v < 0 for v in vol.values()):
            raise MonteCarloValidationError("Volatilities must be finite, nonnegative Decimals.")
        if vol["liquidity"] != 0:
            raise MonteCarloValidationError("The accepted Liquidity model has zero volatility.")
        rng = Random(int.from_bytes(sha256(f"{DRAW_VERSION}:{SEED}:{path_number}".encode()).digest(), "big"))
        centres = {role: (1 + growth[role]).ln() for role in ROLES}
        rho = Decimal(".35")
        residual = (1 - rho*rho).sqrt()
        rows = []
        for _ in range(horizon):
            equity, bond = _normal_pair(rng)
            gold, _unused = _normal_pair(rng)
            shocks = dict(equity=equity, income=rho*equity + residual*bond, liquidity=ZERO, alternatives=gold)
            rows.append({role: (centres[role] + vol[role]*shocks[role]).exp() - 1 if vol[role] else growth[role] for role in ROLES})
        return tuple(rows)


def _percentile(sorted_values, fraction):
    position = Decimal(len(sorted_values) - 1) * fraction
    lower = int(position)
    remainder = position - lower
    return sorted_values[lower] if remainder == 0 else sorted_values[lower] + remainder * (sorted_values[lower+1] - sorted_values[lower])


def compare_allocations(experiment, alternative_percentages, *, path_count=PATH_COUNT):
    for update in comparison_steps(experiment, alternative_percentages, path_count=path_count):
        if 'result' in update:
            return update['result']


def comparison_steps(experiment, alternative_percentages, *, path_count=PATH_COUNT):
    """Only compact yearly balances retained; the inspector replays one actual path."""
    with localcontext(CONTEXT):
        if not isinstance(path_count, int) or path_count < 1:
            raise MonteCarloValidationError("A run needs a positive path count.")
        alternative = allocation_weights(alternative_percentages)
        basis = experiment["basis"]
        plan = basis["_inputs"]["plan"]
        horizon = plan.final_age_years - plan.current_age_years
        weights = (experiment["current_weights"], alternative)
        samples = [[[] for _ in range(horizon)] for _ in weights]
        counts = [dict(lifestyle=0, core_shortfall=0, lifestyle_and_goal=0) for _ in weights]
        initial_mixes = []
        yield {'completed': 0, 'total': path_count}
        for number in range(1, path_count + 1):
            returns = return_path(basis["role_returns"], horizon, number)
            paired = []
            for index, mix in enumerate(weights):
                projection = paired[0] if index == 1 and mix == weights[0] else run_generated_projection(basis, returns, mix, compact=number != 1)
                paired.append(projection)
                if number == 1:
                    initial_mixes.append(projection['years'][0]['rebalance'])
                rows = projection["years"]
                core_gap = any(row["core_shortfall_amount"] > 0 for row in rows)
                funded = not core_gap and not any(row["flexible_resource_shortfall_amount"] > 0 for row in rows) and rows[-1]["opening_obligation_remaining_amount"] == 0
                counts[index]["lifestyle"] += int(funded)
                counts[index]["core_shortfall"] += int(core_gap)
                counts[index]["lifestyle_and_goal"] += int(funded and projection["final_value_amount"] >= basis["legacy_target_reporting_amount"])
                for elapsed, row in enumerate(rows):
                    samples[index][elapsed].append(row["ending_value_amount"])
            if number % 25 == 0 or number == path_count:
                yield {'completed': number, 'total': path_count}
        results = []
        for index, label in enumerate(("Current model mix", "Alternative mix")):
            rows = []
            for elapsed, values in enumerate(samples[index]):
                values.sort()
                factor = (1 + plan.core_inflation_decimal) ** (elapsed + 1)
                nominal = {name: _percentile(values, q) for name, q in (("p10", Decimal(".1")), ("median", Decimal(".5")), ("p90", Decimal(".9")))}
                rows.append({"ending_age": plan.current_age_years + elapsed + 1,
                             "nominal": nominal, "real": {name: value / factor for name, value in nominal.items()}})
            results.append({"label": label, "weights": weights[index], "rows": rows, "initial_rebalance": initial_mixes[index],
                            "outcomes": {key: {"count": count, "percent": (Decimal(count) * 100 / path_count).quantize(ONE)} for key, count in counts[index].items()}})
        yield {'result': {"allocations": results, "path_count": path_count, "model_version": MODEL_VERSION, "seed": SEED,
                "legacy_real": plan.terminal_legacy_target_amount, "legacy_nominal": basis["legacy_target_reporting_amount"]}}


def inspect_path(experiment, alternative_percentages, path_number):
    with localcontext(CONTEXT):
        if not 1 <= path_number <= PATH_COUNT:
            raise MonteCarloValidationError(f"Choose a path from 1 to {PATH_COUNT}.")
        basis = experiment["basis"]
        plan = basis["_inputs"]["plan"]
        returns = return_path(basis["role_returns"], plan.final_age_years - plan.current_age_years, path_number)
        paths = []
        for weights in (experiment["current_weights"], allocation_weights(alternative_percentages)):
            projection = run_generated_projection(basis, returns, weights)
            rows = []
            for elapsed, row in enumerate(projection["years"]):
                rows.append({**row, "ending_age": row["age"] + 1,
                             "ending_value_today": row["ending_value_amount"] / (1 + plan.core_inflation_decimal) ** (elapsed + 1)})
            paths.append({**projection, "years": rows})
        return paths
