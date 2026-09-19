"""User-entered allocation target ranges."""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping, Sequence

from sqlalchemy import select

from app.conventions import parse_decimal
from app.decimal_policy import exact_units
from app.extensions import db
from app.models import ASSET_ROLE_CODES, AllocationTarget, Portfolio


NOW_YEARS = Decimal("3")
BRIDGE_YEARS = Decimal("7")


class PlanningValidationError(ValueError):
    """An owner-entered planning target is invalid."""

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field


def _decimal(value: Decimal | str | int, *, field: str) -> Decimal:
    try:
        parsed = parse_decimal(value, field_name=field)
        exact_units(parsed, 8)
    except (TypeError, ValueError) as exc:
        raise PlanningValidationError(str(exc), field=field) from exc
    if not parsed.is_finite():
        raise PlanningValidationError(f"{field} must be finite.", field=field)
    return parsed


def save_allocation_targets(
    portfolio_id: int,
    ranges: Mapping[str, tuple[Decimal | str | int, Decimal | str | int]],
) -> tuple[AllocationTarget, ...]:
    """Replace one complete, feasible four-role target set."""

    portfolio_exists = db.session.scalar(
        select(Portfolio.id).where(Portfolio.id == portfolio_id)
    )
    if portfolio_exists is None:
        raise PlanningValidationError(
            "Portfolio was not found.", field="portfolio_id"
        )
    if set(ranges) != set(ASSET_ROLE_CODES):
        raise PlanningValidationError(
            "Enter an acceptable range for all four allocation roles.",
            field="targets",
        )

    parsed: dict[str, tuple[Decimal, Decimal]] = {}
    for role_code in ASSET_ROLE_CODES:
        raw_range = ranges[role_code]
        if not isinstance(raw_range, tuple) or len(raw_range) != 2:
            raise PlanningValidationError(
                "Each allocation target needs a minimum and maximum.",
                field=role_code,
            )
        minimum = _decimal(raw_range[0], field=f"{role_code} minimum")
        maximum = _decimal(raw_range[1], field=f"{role_code} maximum")
        if minimum < 0 or maximum > 1 or minimum > maximum:
            raise PlanningValidationError(
                "Target ranges must satisfy 0% ≤ minimum ≤ maximum ≤ 100%.",
                field=role_code,
            )
        parsed[role_code] = (minimum, maximum)

    minimum_total = sum((value[0] for value in parsed.values()), Decimal("0"))
    maximum_total = sum((value[1] for value in parsed.values()), Decimal("0"))
    if minimum_total > 1 or maximum_total < 1:
        raise PlanningValidationError(
            "The four ranges must allow a complete 100% allocation.", field="targets"
        )

    existing = allocation_targets(portfolio_id)
    targets_list: list[AllocationTarget] = []
    for role_code in ASSET_ROLE_CODES:
        target = existing.get(role_code)
        if target is None:
            target = AllocationTarget(
                portfolio_id=portfolio_id,
                asset_role_code=role_code,
                minimum_decimal=parsed[role_code][0],
                maximum_decimal=parsed[role_code][1],
            )
            db.session.add(target)
        else:
            target.minimum_decimal = parsed[role_code][0]
            target.maximum_decimal = parsed[role_code][1]
        targets_list.append(target)
    targets = tuple(targets_list)
    db.session.flush()
    return targets


def allocation_targets(portfolio_id: int) -> dict[str, AllocationTarget]:
    return {
        target.asset_role_code: target
        for target in db.session.scalars(
            select(AllocationTarget).where(
                AllocationTarget.portfolio_id == portfolio_id
            )
        )
    }


def save_annual_inflation(
    portfolio_id: int, value: Decimal | str | int
) -> Decimal:
    """Persist one explicit annual inflation assumption as a decimal rate."""

    portfolio = db.session.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise PlanningValidationError(
            "Portfolio was not found.", field="portfolio_id"
        )
    parsed = _decimal(value, field="Annual inflation")
    if parsed < 0 or parsed > 1:
        raise PlanningValidationError(
            "Annual inflation must be between 0% and 100%.",
            field="annual_inflation_percent",
        )
    from app.services.retirement_plans import adopted_plan
    if adopted_plan(portfolio_id):
        raise PlanningValidationError("Edit Core and Flexible inflation in Retirement.", field="annual_inflation_percent")
    portfolio.annual_inflation_decimal = parsed
    db.session.flush()
    return parsed


def _range_result(
    actual_amount: Decimal,
    denominator: Decimal,
    minimum: Decimal,
    maximum: Decimal,
) -> tuple[str, Decimal, Decimal]:
    percentage = actual_amount / denominator
    if percentage < minimum:
        return (
            "below",
            minimum - percentage,
            denominator * minimum - actual_amount,
        )
    if percentage > maximum:
        return (
            "above",
            percentage - maximum,
            actual_amount - denominator * maximum,
        )
    return "within", Decimal("0"), Decimal("0")


def compare_allocation_to_targets(
    portfolio_id: int,
    role_rows: Sequence[dict[str, object]],
    *,
    denominator: Decimal,
    calculation_complete: bool,
) -> dict[str, object]:
    """Attach exact range gaps without inventing a result from partial inputs."""

    targets = allocation_targets(portfolio_id)
    targets_complete = set(targets) == set(ASSET_ROLE_CODES)
    rows: list[dict[str, object]] = []
    for source_row in role_rows:
        row = dict(source_row)
        target = targets.get(str(row["code"]))
        row.update(
            {
                "target_minimum_decimal": (
                    target.minimum_decimal if target is not None else None
                ),
                "target_maximum_decimal": (
                    target.maximum_decimal if target is not None else None
                ),
                "range_state": None,
                "gap_to_range_percentage": None,
                "gap_to_range_amount": None,
            }
        )
        if (
            target is not None
            and targets_complete
            and calculation_complete
            and denominator != 0
        ):
            state, percentage_gap, amount_gap = _range_result(
                row["included_reporting_amount"],
                denominator,
                target.minimum_decimal,
                target.maximum_decimal,
            )
            row["range_state"] = state
            row["gap_to_range_percentage"] = percentage_gap
            row["gap_to_range_amount"] = amount_gap
        rows.append(row)
    return {
        "targets_complete": targets_complete,
        "calculation_complete": calculation_complete,
        "role_allocation": rows,
        "now_years": NOW_YEARS,
        "bridge_years": BRIDGE_YEARS,
        "projects_in_retirement_runway": False,
    }
