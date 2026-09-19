"""Simple institution relationship minimums using the shared valuation path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.conventions import normalize_currency_code
from app.extensions import db
from app.models import Account, Institution, Portfolio, RelationshipRule
from app.services.portfolio_summary import build_portfolio_summary


RELATIONSHIP_STATUS_CODES = ("current", "stale", "cannot_determine")
RELATIONSHIP_STATE_CODES = (
    "minimum_met",
    "within_warning_buffer",
    "below_minimum",
    "cannot_determine",
)


class RelationshipValidationError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class RelationshipRuleCommand:
    institution_id: int
    name: str
    threshold_amount: Decimal
    threshold_currency_code: str
    warning_buffer_amount: Decimal | None
    is_active: bool
    eligible_account_ids: tuple[int, ...]
    notes: str | None = None


@dataclass(frozen=True)
class RelationshipSnapshot:
    rule_id: int
    institution_id: int
    institution_name: str
    rule_name: str
    as_of_date: date
    threshold_amount: Decimal
    threshold_currency_code: str
    warning_buffer_amount: Decimal | None
    preferred_target_amount: Decimal
    known_eligible_value_amount: Decimal
    eligible_value_amount: Decimal | None
    buffer_amount: Decimal | None
    buffer_percent: Decimal | None
    status: str
    state: str
    latest_value_date: date | None
    eligible_account_count: int
    valued_account_count: int
    missing_account_ids: tuple[int, ...]
    stale_account_ids: tuple[int, ...]
    account_rows: tuple[dict[str, object], ...]
    value_dependencies: tuple[dict[str, object], ...]
    notes: str | None

    @property
    def requires_attention(self) -> bool:
        return self.status != "current" or self.state != "minimum_met"


def relationship_rule_for_institution(
    portfolio_id: int,
    institution_id: int,
    *,
    active_only: bool = True,
) -> RelationshipRule | None:
    statement = (
        select(RelationshipRule)
        .join(Institution)
        .where(
            RelationshipRule.institution_id == institution_id,
            Institution.portfolio_id == portfolio_id,
        )
    )
    if active_only:
        statement = statement.where(RelationshipRule.is_active.is_(True))
    return db.session.scalar(
        statement.order_by(
            RelationshipRule.is_active.desc(),
            RelationshipRule.updated_at.desc(),
            RelationshipRule.id.desc(),
        ).limit(1)
    )


def save_relationship_rule(
    portfolio_id: int,
    command: RelationshipRuleCommand,
) -> RelationshipRule:
    institution = db.session.scalar(
        select(Institution).where(
            Institution.id == command.institution_id,
            Institution.portfolio_id == portfolio_id,
        )
    )
    if institution is None:
        raise RelationshipValidationError(
            "institution_id", "Choose an institution in this portfolio."
        )

    name = (command.name or "").strip()
    if not name:
        raise RelationshipValidationError("name", "Name is required.")
    if len(name) > 160:
        raise RelationshipValidationError(
            "name", "Name must be 160 characters or fewer."
        )

    threshold = command.threshold_amount
    if threshold is None or not threshold.is_finite() or threshold <= 0:
        raise RelationshipValidationError(
            "threshold_amount", "Threshold must be a positive finite amount."
        )
    warning = command.warning_buffer_amount
    if warning is not None and (not warning.is_finite() or warning < 0):
        raise RelationshipValidationError(
            "warning_buffer_amount",
            "Preferred extra buffer must be a non-negative finite amount.",
        )
    try:
        currency = normalize_currency_code(
            command.threshold_currency_code,
            field_name="Threshold currency",
        )
    except (TypeError, ValueError) as exc:
        raise RelationshipValidationError(
            "threshold_currency_code", str(exc)
        ) from exc

    accounts = list(
        db.session.scalars(
            select(Account)
            .where(
                Account.portfolio_id == portfolio_id,
                Account.institution_id == institution.id,
                Account.is_active.is_(True),
            )
            .order_by(Account.name, Account.id)
        )
    )
    valid_account_ids = {account.id for account in accounts}
    selected_ids = set(command.eligible_account_ids)
    if not selected_ids <= valid_account_ids:
        raise RelationshipValidationError(
            "eligible_account_ids",
            "Choose only active accounts at this institution.",
        )
    if command.is_active and not selected_ids:
        raise RelationshipValidationError(
            "eligible_account_ids",
            "Choose at least one eligible account while the minimum is active.",
        )

    rule = relationship_rule_for_institution(
        portfolio_id, institution.id, active_only=False
    )
    if rule is None:
        rule = RelationshipRule(institution_id=institution.id)
        db.session.add(rule)

    rule.name = name
    rule.threshold_amount = threshold
    rule.threshold_currency_code = currency
    rule.warning_buffer_amount = warning
    rule.is_active = command.is_active
    rule.notes = (command.notes or "").strip() or None
    for account in accounts:
        account.relationship_eligible = account.id in selected_ids
    db.session.flush()
    return rule


def _source_dates(summary: dict[str, object], account_id: int) -> tuple[date, ...]:
    dates: set[date] = set()
    for row in summary["holdings"]:
        if row["account_id"] != account_id or row["reporting_amount"] is None:
            continue
        for key in ("value_date", "fx_date"):
            if row.get(key) is not None:
                dates.add(row[key])
    for row in summary["cash_balances"]:
        if row["account_id"] != account_id or row["reporting_amount"] is None:
            continue
        for key in ("checkpoint_date", "latest_cash_effect_date", "fx_date"):
            if row.get(key) is not None:
                dates.add(row[key])
    return tuple(sorted(dates))


def _relationship_value_dependencies(
    summary: dict[str, object], eligible_account_ids: set[int]
) -> tuple[dict[str, object], ...]:
    """Expose the exact value sources affecting the threshold-currency view."""

    dependencies: list[dict[str, object]] = []
    by_key: dict[tuple[object, ...], dict[str, object]] = {}
    for item in summary["value_attention_items"]:
        affected_ids = tuple(
            account_id
            for account_id in item["affected_account_ids"]
            if account_id in eligible_account_ids
        )
        if not affected_ids:
            continue
        affected_names = tuple(
            row["account_name"]
            for row in summary["account_summaries"]
            if row["account_id"] in affected_ids
        )
        sources = (
            (item.get("missing_source"),)
            if item["kind"] == "missing"
            else tuple(item.get("stale_sources") or ())
        )
        for source in sources:
            if source == "price":
                key = (item["kind"], source, item.get("instrument_id"))
                effective_date = item.get("value_date")
            elif source == "statement":
                key = (item["kind"], source, item.get("registration_id"))
                effective_date = item.get("value_date")
            elif source == "fx":
                key = (
                    item["kind"], source, item.get("native_currency"),
                    item.get("reporting_currency"),
                )
                effective_date = item.get("fx_date")
            else:
                continue
            dependency = by_key.get(key)
            if dependency is None:
                dependency = {
                    "status": item["kind"],
                    "source": source,
                    "instrument_id": item.get("instrument_id"),
                    "registration_id": item.get("registration_id"),
                    "instrument_name": item.get("instrument_name"),
                    "base_currency": item.get("native_currency") if source == "fx" else None,
                    "quote_currency": item.get("reporting_currency") if source == "fx" else None,
                    "effective_date": effective_date,
                    "fx_path": item.get("fx_path") if source == "fx" else None,
                    "affected_account_ids": affected_ids,
                    "affected_account_names": affected_names,
                }
                dependencies.append(dependency)
                by_key[key] = dependency
            else:
                dependency["affected_account_ids"] = tuple(
                    dict.fromkeys(dependency["affected_account_ids"] + affected_ids)
                )
                dependency["affected_account_names"] = tuple(
                    dict.fromkeys(dependency["affected_account_names"] + affected_names)
                )
    return tuple(dependencies)


def evaluate_relationship_rule(
    portfolio: Portfolio,
    rule: RelationshipRule,
    as_of_date: date,
) -> RelationshipSnapshot:
    if rule.institution.portfolio_id != portfolio.id:
        raise RelationshipValidationError(
            "institution_id", "Relationship minimum belongs to another portfolio."
        )

    summary = build_portfolio_summary(
        portfolio,
        as_of_date,
        reporting_currency=rule.threshold_currency_code,
    )
    summaries_by_account = {
        row["account_id"]: row for row in summary["account_summaries"]
    }
    eligible_accounts = list(
        db.session.scalars(
            select(Account)
            .where(
                Account.portfolio_id == portfolio.id,
                Account.institution_id == rule.institution_id,
                Account.relationship_eligible.is_(True),
                Account.is_active.is_(True),
            )
            .order_by(Account.name, Account.id)
        )
    )
    eligible_account_ids = {account.id for account in eligible_accounts}
    value_dependencies = _relationship_value_dependencies(
        summary, eligible_account_ids
    )

    known_total = Decimal("0")
    missing_ids: list[int] = []
    stale_ids: list[int] = []
    account_rows: list[dict[str, object]] = []
    all_source_dates: set[date] = set()
    valued_account_count = 0
    for account in eligible_accounts:
        account_summary = summaries_by_account[account.id]
        amount = account_summary["gross_reporting_amount"]
        if amount is not None:
            known_total += amount
            valued_account_count += 1
        if account_summary["status"] in {"empty", "missing", "partial"}:
            missing_ids.append(account.id)
        if account_summary["stale_count"]:
            stale_ids.append(account.id)
        dates = _source_dates(summary, account.id)
        all_source_dates.update(dates)
        account_rows.append(
            {
                "account_id": account.id,
                "account_name": account.name,
                "reference": account.reference,
                "amount": amount,
                "currency": rule.threshold_currency_code,
                "status": account_summary["status"],
                "item_count": account_summary["item_count"],
                "missing_count": account_summary["missing_count"],
                "stale_count": account_summary["stale_count"],
                "source_dates": dates,
                "latest_value_date": max(dates) if dates else None,
                "value_dependencies": tuple(
                    dependency
                    for dependency in value_dependencies
                    if account.id in dependency["affected_account_ids"]
                ),
            }
        )

    cannot_determine = not eligible_accounts or bool(missing_ids)
    if cannot_determine:
        eligible_value = None
        buffer_amount = None
        buffer_percent = None
        status = "cannot_determine"
        state = "cannot_determine"
    else:
        eligible_value = known_total
        buffer_amount = eligible_value - rule.threshold_amount
        buffer_percent = buffer_amount / rule.threshold_amount
        status = "stale" if stale_ids else "current"
        if buffer_amount < 0:
            state = "below_minimum"
        elif (
            rule.warning_buffer_amount is not None
            and buffer_amount < rule.warning_buffer_amount
        ):
            state = "within_warning_buffer"
        else:
            state = "minimum_met"

    warning = rule.warning_buffer_amount or Decimal("0")
    return RelationshipSnapshot(
        rule_id=rule.id,
        institution_id=rule.institution_id,
        institution_name=rule.institution.name,
        rule_name=rule.name,
        as_of_date=as_of_date,
        threshold_amount=rule.threshold_amount,
        threshold_currency_code=rule.threshold_currency_code,
        warning_buffer_amount=rule.warning_buffer_amount,
        preferred_target_amount=rule.threshold_amount + warning,
        known_eligible_value_amount=known_total,
        eligible_value_amount=eligible_value,
        buffer_amount=buffer_amount,
        buffer_percent=buffer_percent,
        status=status,
        state=state,
        latest_value_date=max(all_source_dates) if all_source_dates else None,
        eligible_account_count=len(eligible_accounts),
        valued_account_count=valued_account_count,
        missing_account_ids=tuple(missing_ids),
        stale_account_ids=tuple(stale_ids),
        account_rows=tuple(account_rows),
        value_dependencies=value_dependencies,
        notes=rule.notes,
    )


def relationship_attention_item(
    snapshot: RelationshipSnapshot,
) -> dict[str, object] | None:
    if not snapshot.requires_attention:
        return None
    if snapshot.status == "cannot_determine":
        detail = "The current relationship balance cannot be determined from all eligible accounts."
        kind = "missing"
    elif snapshot.status == "stale":
        detail = "The relationship balance uses one or more stale eligible values."
        kind = "stale"
    elif snapshot.state == "below_minimum":
        detail = "The eligible value is below the user-entered relationship minimum."
        kind = "warning"
    else:
        detail = "The minimum is met, but the eligible value is inside the preferred extra buffer."
        kind = "warning"
    return {
        "kind": kind,
        "subject_kind": "relationship_minimum",
        "action_kind": "edit_relationship_minimum",
        "institution_id": snapshot.institution_id,
        "institution_name": snapshot.institution_name,
        "rule_id": snapshot.rule_id,
        "status": snapshot.status,
        "state": snapshot.state,
        "detail": detail,
        "threshold_amount": snapshot.threshold_amount,
        "threshold_currency": snapshot.threshold_currency_code,
        "eligible_value_amount": snapshot.eligible_value_amount,
        "buffer_amount": snapshot.buffer_amount,
        "as_of_date": snapshot.as_of_date,
        "value_dependencies": snapshot.value_dependencies,
    }


def active_relationship_snapshots(
    portfolio: Portfolio,
    as_of_date: date,
) -> tuple[RelationshipSnapshot, ...]:
    rules = list(
        db.session.scalars(
            select(RelationshipRule)
            .join(Institution)
            .where(
                Institution.portfolio_id == portfolio.id,
                RelationshipRule.is_active.is_(True),
            )
            .order_by(Institution.name, RelationshipRule.id)
        )
    )
    return tuple(
        evaluate_relationship_rule(portfolio, rule, as_of_date) for rule in rules
    )
