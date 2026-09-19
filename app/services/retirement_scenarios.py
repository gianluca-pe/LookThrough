"""Named early-return comparisons with frozen inputs and inspectable results."""

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from sqlalchemy import select

from app.extensions import db
from app.models import RetirementScenario
from app.services.portfolio_summary import build_portfolio_summary
from app.services.retirement import prepare_retirement_projection, run_retirement_projection, ROLE_RETURN_FIELDS
from app.services.retirement_plans import adopted_plan, plan_incomes, validate_plan, validate_income, completed_years

ENGINE_VERSION = "annual-core-flexible-2"
PAYLOAD_VERSION = 1


class ScenarioValidationError(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field, self.message = field, message


def parse_return_path(text, horizon):
    """One line per consecutive projection year; no inferred numerical returns."""
    lines = text.strip().splitlines()
    if not lines or len(lines) > horizon:
        raise ScenarioValidationError("return_path", f"Enter between 1 and {horizon} annual rows.")
    rows = []
    for index, line in enumerate(lines, 1):
        try:
            parts = line.split(",")
            if len(parts) != 4:
                raise ValueError
            values = [Decimal(part.strip()) / Decimal("100") for part in parts]
            if any(not v.is_finite() or not -1 <= v <= 1 or v != v.quantize(Decimal("0.00000001")) for v in values):
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ScenarioValidationError("return_path", f"Row {index}: enter four percentages from -100 to 100, with at most six decimal places, separated by commas.") from None
        rows.append(dict(zip(ROLE_RETURN_FIELDS, values)))
    return rows


def _encode(value):
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, SimpleNamespace):
        return {"$namespace": vars(value)}
    raise TypeError(f"Unsupported comparison value: {type(value).__name__}")


def _decode(value):
    if set(value) == {"$decimal"}:
        result = Decimal(value["$decimal"])
        if not result.is_finite():
            raise ValueError("Nonfinite comparison amount.")
        return result
    if set(value) == {"$datetime"}:
        return datetime.fromisoformat(value["$datetime"])
    if set(value) == {"$date"}:
        return date.fromisoformat(value["$date"])
    if set(value) == {"$namespace"}:
        return SimpleNamespace(**value["$namespace"])
    return value


def encode_payload(payload):
    # Preserve source-role insertion order: Decimal accumulation must replay exactly.
    return json.dumps(payload, default=_encode, separators=(",", ":"), allow_nan=False)


def load_payload(text):
    payload = json.loads(text, object_hook=_decode)
    if payload["version"] != PAYLOAD_VERSION or payload["engine_version"] != ENGINE_VERSION:
        raise ValueError("Unsupported retirement comparison version.")
    return payload


def build_scenario(portfolio, as_of_date, *, name, return_path):
    name = name.strip()
    if not name or len(name) > 160:
        raise ScenarioValidationError("name", "Enter a comparison name of 1 to 160 characters.")
    plan = adopted_plan(portfolio.id)
    if plan is None:
        raise ScenarioValidationError("name", "Adopt a retirement plan before comparing return paths.")
    summary = build_portfolio_summary(portfolio, as_of_date, reporting_currency=plan.currency_code)
    basis = prepare_retirement_projection(portfolio, summary, as_of_date, reporting_currency=plan.currency_code)
    if not basis["calculation_complete"]:
        raise ScenarioValidationError("as_of_date", "The starting portfolio or plan is incomplete for this date. Review the dated Retirement result before saving a comparison.")
    horizon = basis["assumption"].final_age_years - basis["assumption"].current_age_years
    path = parse_return_path(return_path, horizon)
    fields = ("registration_id", "instrument_id", "instrument_name", "account_id", "account_name",
              "institution_name", "native_amount", "native_currency", "included_reporting_amount",
              "value_date", "checkpoint_date", "latest_cash_effect_date", "fx_date", "fx_path",
              "status", "source_mode", "role_weights", "fire_bucket_code", "portfolio_share_decimal",
              "present_access_decimal", "earliest_access_date", "classification_date",
              "classification_source_note", "fixed_deposit", "fx_rate", "access_note", "cash_status")
    payload = {"version": PAYLOAD_VERSION, "engine_version": ENGINE_VERSION,
               "policy_version": "core-flexible-0049", "name": name,
               "basis": basis, "annual_returns": path,
               "income_assumptions": [{c.name: getattr(row, c.name) for c in row.__table__.columns}
                                      for row in plan_incomes(plan.id)],
               "source_evidence": {kind: [{key: row.get(key) for key in fields} for row in summary[kind]]
                                   for kind in ("holdings", "cash_balances")},
               "baseline": run_retirement_projection(basis),
               "stress": run_retirement_projection(basis, path)}
    # Ensure the complete record is serializable before any write.
    encode_payload(payload)
    return payload


def save_scenario(portfolio, as_of_date, *, name, return_path):
    payload = build_scenario(portfolio, as_of_date, name=name, return_path=return_path)
    row = RetirementScenario(portfolio_id=portfolio.id, name=payload["name"], as_of_date=as_of_date,
                             payload_json=encode_payload(payload))
    db.session.add(row)
    db.session.flush()
    return row


def scenarios_for_portfolio(portfolio_id):
    return tuple(db.session.scalars(select(RetirementScenario)
        .where(RetirementScenario.portfolio_id == portfolio_id)
        .order_by(RetirementScenario.created_at.desc(), RetirementScenario.id.desc())))


def validate_scenario_record(row):
    """Reject broken saved runs before backup restore reaches the active database."""
    try:
        payload = load_payload(row["payload_json"])
        if set(payload) != {"version", "engine_version", "policy_version", "name", "basis",
                            "annual_returns", "income_assumptions", "source_evidence", "baseline", "stress"}:
            raise ValueError("Unexpected comparison payload fields.")
        if payload["policy_version"] != "core-flexible-0049":
            raise ValueError("Unsupported spending policy version.")
        basis = payload["basis"]
        validate_plan(vars(basis["_inputs"]["plan"]))
        expected_assumption = dict(vars(basis["_inputs"]["plan"]))
        expected_assumption["current_age_years"] += max(0, completed_years(
            expected_assumption["base_date"], basis["as_of_date"]))
        if vars(basis["assumption"]) != expected_assumption or not (
            basis["_inputs"]["plan"].base_date <= basis["as_of_date"]
            and basis["assumption"].current_age_years < basis["assumption"].final_age_years
        ):
            raise ValueError("Comparison dates do not match the frozen plan.")
        for income in payload["income_assumptions"]:
            validate_income(income)
        if not isinstance(row["name"], str) or not row["name"].strip() or len(row["name"]) > 160:
            raise ValueError("Invalid comparison name.")
        if payload["name"] != row["name"] or payload["basis"]["as_of_date"] != row["as_of_date"]:
            raise ValueError("Comparison metadata does not match its frozen inputs.")
        if not payload["basis"]["calculation_complete"] or not payload["annual_returns"]:
            raise ValueError("Comparison requires complete inputs and a return path.")
        if payload["basis"]["_inputs"]["plan"].portfolio_id != row["portfolio_id"]:
            raise ValueError("Comparison belongs to another portfolio.")
        for name, path in (("baseline", ()), ("stress", payload["annual_returns"])):
            expected = run_retirement_projection(payload["basis"], path)
            if encode_payload(expected) != encode_payload(payload[name]):
                raise ValueError("Saved result does not match the frozen calculation inputs.")
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError, RecursionError) as error:
        raise ValueError(f"Invalid retirement comparison: {error}") from error
