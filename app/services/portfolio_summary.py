"""Portfolio totals built from the shared position, cash, and FX paths."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.conventions import normalize_currency_code
from app.extensions import db
from app.models import (
    ECONOMIC_ROLE_CODES,
    FIRE_BUCKET_CODES,
    Account,
    Instrument,
    Portfolio,
    PositionRegistration,
)
from app.services.cash import cash_currencies, resolve_cash
from app.services.classification import classifications_as_of, current_fire_bucket_code
from app.services.fx import resolve_fx
from app.services.fixed_deposits import maturity_snapshot, terms_by_registration_ids
from app.services.planning import compare_allocation_to_targets
from app.services.valuation import value_position


_STALE_SOURCE_LABELS = {
    "price": "price",
    "statement": "statement value",
    "fx": "FX rate",
}


def _stale_detail(stale_sources: tuple[str, ...]) -> str:
    labels = [_STALE_SOURCE_LABELS[source] for source in stale_sources]
    if len(labels) == 1:
        subject = labels[0]
        verb = "is"
        possessive = "its"
    else:
        subject = " and ".join(labels)
        verb = "are"
        possessive = "their"
    return (
        f"The latest eligible {subject} {verb} older than {possessive} "
        "freshness setting."
    )


def _value_attention_source(item: dict[str, object]) -> str | None:
    if item["kind"] == "missing":
        return item.get("missing_source")
    sources = item.get("stale_sources") or ()
    return sources[0] if len(sources) == 1 else None


def _value_attention_key(item: dict[str, object]) -> tuple[object, ...] | None:
    """Identify one user-maintained source without hiding mixed-source warnings."""

    if item.get("action_kind") != "update_value":
        return None
    source = _value_attention_source(item)
    if source == "price":
        return (item["kind"], source, item.get("instrument_id"))
    if source == "statement":
        return (item["kind"], source, item.get("registration_id"))
    if source == "fx":
        return (
            item["kind"],
            source,
            item.get("native_currency"),
            item.get("reporting_currency"),
        )
    return None


def _deduplicate_value_attention(
    items: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Show an instrument price or FX pair once, while retaining its reach."""

    result: list[dict[str, object]] = []
    by_key: dict[tuple[object, ...], dict[str, object]] = {}
    for original in items:
        item = dict(original)
        account_ids = (item["account_id"],) if item.get("account_id") else ()
        account_names = (item["account_name"],) if item.get("account_name") else ()
        registration_ids = (
            (item["registration_id"],) if item.get("registration_id") else ()
        )
        item["affected_account_ids"] = account_ids
        item["affected_account_names"] = account_names
        item["affected_registration_ids"] = registration_ids
        item["affected_count"] = 1

        key = _value_attention_key(item)
        if key is None or key not in by_key:
            result.append(item)
            if key is not None:
                by_key[key] = item
            continue

        existing = by_key[key]
        existing["affected_account_ids"] = tuple(
            dict.fromkeys(existing["affected_account_ids"] + account_ids)
        )
        existing["affected_account_names"] = tuple(
            dict.fromkeys(existing["affected_account_names"] + account_names)
        )
        existing["affected_registration_ids"] = tuple(
            dict.fromkeys(existing["affected_registration_ids"] + registration_ids)
        )
        existing["affected_count"] += 1
    return result


def _total_status(
    item_count: int,
    valued_count: int,
    stale_count: int,
) -> str:
    """Apply the same honest completeness state to every portfolio subtotal."""

    missing_count = item_count - valued_count
    if item_count == 0:
        return "empty"
    if valued_count == 0:
        return "missing"
    if missing_count:
        return "partial"
    if stale_count:
        return "stale"
    return "current"


def _value_overlay(
    reporting_amount: Decimal | None,
    account: Account,
    *,
    term_locked: bool = False,
) -> dict[str, Decimal | None]:
    """Apply inclusion, account access, then instrument-level term access."""

    if reporting_amount is None:
        return {
            "included_reporting_amount": None,
            "accessible_reporting_amount": None,
            "term_liquidity_reporting_amount": None,
            "excluded_by_share_reporting_amount": None,
            "restricted_reporting_amount": None,
        }
    included = reporting_amount * account.portfolio_share_decimal
    account_accessible = included * account.present_access_decimal
    term_liquidity = account_accessible if term_locked else Decimal("0")
    accessible = Decimal("0") if term_locked else account_accessible
    return {
        "included_reporting_amount": included,
        "accessible_reporting_amount": accessible,
        "term_liquidity_reporting_amount": term_liquidity,
        "excluded_by_share_reporting_amount": reporting_amount - included,
        "restricted_reporting_amount": included - account_accessible,
    }


def _empty_account_totals(account: Account) -> dict[str, object]:
    return {
        "account": account,
        "position_count": 0,
        "position_reporting_valued_count": 0,
        "cash_balance_count": 0,
        "cash_reporting_valued_count": 0,
        "stale_count": 0,
        "investment_reporting_amount": Decimal("0"),
        "cash_reporting_amount": Decimal("0"),
        "gross_reporting_amount": Decimal("0"),
        "included_reporting_amount": Decimal("0"),
        "accessible_reporting_amount": Decimal("0"),
        "term_liquidity_reporting_amount": Decimal("0"),
        "excluded_by_share_reporting_amount": Decimal("0"),
        "restricted_reporting_amount": Decimal("0"),
    }


def _record_account_value(
    totals: dict[str, object],
    *,
    source: str,
    reporting_amount: Decimal | None,
    status: str,
    overlay: dict[str, Decimal | None],
) -> None:
    count_key = "position_count" if source == "investment" else "cash_balance_count"
    valued_key = (
        "position_reporting_valued_count"
        if source == "investment"
        else "cash_reporting_valued_count"
    )
    amount_key = (
        "investment_reporting_amount"
        if source == "investment"
        else "cash_reporting_amount"
    )
    totals[count_key] += 1
    if status == "stale":
        totals["stale_count"] += 1
    if reporting_amount is None:
        return
    totals[valued_key] += 1
    totals[amount_key] += reporting_amount
    totals["gross_reporting_amount"] += reporting_amount
    for key, amount in overlay.items():
        assert amount is not None
        totals[key] += amount


def _known_amount(amount: Decimal, valued_count: int) -> Decimal | None:
    return amount if valued_count else None


def _reporting_breakdown(
    rows: list[dict[str, object]],
    *,
    key_name: str,
    label_name: str,
    include_native: bool = False,
) -> list[dict[str, object]]:
    """Group shared-summary rows without hiding missing or stale sources."""

    groups: dict[object, dict[str, object]] = {}
    for row in rows:
        key = row[key_name]
        group = groups.setdefault(
            key,
            {
                key_name: key,
                label_name: row[label_name],
                "item_count": 0,
                "reporting_valued_count": 0,
                "stale_count": 0,
                "included_reporting_amount": Decimal("0"),
                "native_valued_count": 0,
                "native_amount": Decimal("0"),
            },
        )
        group["item_count"] += 1
        if row["status"] == "stale":
            group["stale_count"] += 1
        if row["reporting_amount"] is not None:
            group["reporting_valued_count"] += 1
            group["included_reporting_amount"] += row[
                "included_reporting_amount"
            ]
        if include_native and row["native_amount"] is not None:
            group["native_valued_count"] += 1
            group["native_amount"] += row["native_amount"]

    result = []
    for group in groups.values():
        valued_count = group["reporting_valued_count"]
        group["included_reporting_amount"] = _known_amount(
            group["included_reporting_amount"], valued_count
        )
        group["native_amount"] = (
            _known_amount(group["native_amount"], group["native_valued_count"])
            if include_native
            else None
        )
        group["status"] = _total_status(
            group["item_count"],
            valued_count,
            group["stale_count"],
        )
        result.append(group)

    known_included_total = sum(
        (
            row["included_reporting_amount"]
            for row in result
            if row["included_reporting_amount"] is not None
        ),
        Decimal("0"),
    )
    for row in result:
        amount = row["included_reporting_amount"]
        row["included_percentage"] = (
            amount / known_included_total
            if amount is not None and known_included_total != 0
            else None
        )
    return sorted(result, key=lambda row: row[label_name])


def _allocation_summary(
    holdings: list[dict[str, object]],
    cash_balances: list[dict[str, object]],
    *,
    included_reporting_total: Decimal,
    accessible_reporting_total: Decimal,
) -> dict[str, object]:
    """Allocate included values without hiding unclassified amounts."""

    role_included = {code: Decimal("0") for code in ECONOMIC_ROLE_CODES}
    role_accessible = {code: Decimal("0") for code in ECONOMIC_ROLE_CODES}
    classified_investment = Decimal("0")
    classified_accessible_investment = Decimal("0")

    bucket_included = {code: Decimal("0") for code in FIRE_BUCKET_CODES}
    bucket_accessible = {code: Decimal("0") for code in FIRE_BUCKET_CODES}
    bucket_classified_investment = Decimal("0")
    bucket_classified_accessible_investment = Decimal("0")

    classification_gaps: dict[int, dict[str, object]] = {}
    for holding in holdings:
        included = holding["included_reporting_amount"]
        accessible = holding["accessible_reporting_amount"]
        role_weights = holding["role_weights"]
        fire_bucket_code = holding["fire_bucket_code"]

        if included is not None:
            if role_weights:
                classified_investment += included
                classified_accessible_investment += accessible
                for role in role_weights:
                    code = role["code"]
                    weight = role["weight_decimal"]
                    role_included[code] += included * weight
                    role_accessible[code] += accessible * weight
            if fire_bucket_code:
                bucket_classified_investment += included
                bucket_classified_accessible_investment += accessible
                bucket_included[fire_bucket_code] += included
                bucket_accessible[fire_bucket_code] += accessible
            if included != 0 and (not role_weights or not fire_bucket_code):
                instrument_id = holding["instrument_id"]
                gap = classification_gaps.setdefault(
                    instrument_id,
                    {
                        "instrument_id": instrument_id,
                        "instrument_name": holding["instrument_name"],
                        "reporting_currency": holding["reporting_currency"],
                        "included_reporting_amount": Decimal("0"),
                        "accessible_reporting_amount": Decimal("0"),
                        "registration_ids": [],
                        "account_ids": [],
                        "missing_role": not role_weights,
                        "missing_bucket": not fire_bucket_code,
                    },
                )
                gap["included_reporting_amount"] += included
                gap["accessible_reporting_amount"] += accessible
                gap["registration_ids"].append(holding["registration_id"])
                if holding["account_id"] not in gap["account_ids"]:
                    gap["account_ids"].append(holding["account_id"])

    cash_included = Decimal("0")
    cash_accessible = Decimal("0")
    for cash in cash_balances:
        included = cash["included_reporting_amount"]
        accessible = cash["accessible_reporting_amount"]
        if included is None:
            continue
        cash_included += included
        cash_accessible += accessible
    role_included["liquidity"] += cash_included
    role_accessible["liquidity"] += cash_accessible

    role_denominator = classified_investment + cash_included
    role_accessible_denominator = (
        classified_accessible_investment + cash_accessible
    )
    bucket_denominator = bucket_classified_investment
    bucket_accessible_denominator = bucket_classified_accessible_investment
    # Reconcile gap amounts to the portfolio's canonical included/access totals.
    # Summing the same high-precision Decimals in a different account/holding order
    # can otherwise leave a harmless but visible 1E-22 residual.
    unclassified_investment = included_reporting_total - role_denominator
    unclassified_accessible_investment = (
        accessible_reporting_total - role_accessible_denominator
    )
    unbucketed_investment = (
        included_reporting_total - bucket_denominator - cash_included
    )
    unbucketed_accessible_investment = (
        accessible_reporting_total
        - bucket_accessible_denominator
        - cash_accessible
    )

    role_rows = [
        {
            "code": code,
            "included_reporting_amount": role_included[code],
            "accessible_reporting_amount": role_accessible[code],
            "included_percentage": (
                role_included[code] / role_denominator
                if role_denominator != 0
                else None
            ),
            "accessible_percentage": (
                role_accessible[code] / role_accessible_denominator
                if role_accessible_denominator != 0
                else None
            ),
        }
        for code in ECONOMIC_ROLE_CODES
    ]
    bucket_rows = [
        {
            "code": code,
            "included_reporting_amount": bucket_included[code],
            "accessible_reporting_amount": bucket_accessible[code],
            "included_percentage": (
                bucket_included[code] / bucket_denominator
                if bucket_denominator != 0
                else None
            ),
            "accessible_percentage": (
                bucket_accessible[code] / bucket_accessible_denominator
                if bucket_accessible_denominator != 0
                else None
            ),
        }
        for code in FIRE_BUCKET_CODES
    ]

    classification_attention_items = []
    for gap in classification_gaps.values():
        if gap["missing_role"] and gap["missing_bucket"]:
            detail = "Allocation role and FIRE bucket are not set."
        elif gap["missing_role"]:
            detail = "Allocation role is not set."
        else:
            detail = "FIRE bucket is not set."
        classification_attention_items.append(
            {
                "kind": "warning",
                "subject_kind": "instrument_classification",
                "action_kind": "classify_instrument",
                "detail": detail,
                **gap,
            }
        )

    return {
        "role_allocation": role_rows,
        "role_classified_denominator_amount": role_denominator,
        "role_accessible_denominator_amount": role_accessible_denominator,
        "role_classified_investment_amount": classified_investment,
        "role_unclassified_investment_amount": unclassified_investment,
        "role_unclassified_accessible_investment_amount": (
            unclassified_accessible_investment
        ),
        "role_cash_amount": cash_included,
        "role_accessible_cash_amount": cash_accessible,
        "bucket_allocation": bucket_rows,
        "bucket_classified_denominator_amount": bucket_denominator,
        "bucket_accessible_denominator_amount": bucket_accessible_denominator,
        "bucket_unclassified_investment_amount": unbucketed_investment,
        "bucket_unclassified_accessible_investment_amount": (
            unbucketed_accessible_investment
        ),
        "bucket_unassigned_cash_amount": cash_included,
        "bucket_unassigned_accessible_cash_amount": cash_accessible,
        "classification_attention_items": classification_attention_items,
    }


def build_portfolio_summary(
    portfolio: Portfolio,
    as_of_date: date,
    *,
    reporting_currency: str | None = None,
) -> dict[str, object]:
    """Return traceable gross, included, and accessible portfolio values.

    An untouched separate-cash account is not treated as evidence of a zero
    balance. A confirmed zero, or a zero reached through posted activity, is a real
    sourced value and remains included. Account portfolio share is applied before
    present access; access therefore never makes another person's excluded share a
    FIRE resource. A request-scoped reporting currency changes only this derived
    valuation view; it never rewrites a native source or portfolio setting.
    """

    selected_reporting_currency = normalize_currency_code(
        reporting_currency or portfolio.reporting_currency_code,
        field_name="Reporting currency",
    )

    accounts = list(
        db.session.scalars(
            select(Account)
            .where(
                Account.portfolio_id == portfolio.id,
                Account.is_active.is_(True),
            )
            .order_by(Account.name, Account.id)
        )
    )
    account_totals = {
        account.id: _empty_account_totals(account) for account in accounts
    }

    registrations = list(
        db.session.scalars(
            select(PositionRegistration)
            .join(Instrument)
            .join(Account, PositionRegistration.account_id == Account.id)
            .where(
                Instrument.portfolio_id == portfolio.id,
                Account.portfolio_id == portfolio.id,
                Account.is_active.is_(True),
            )
            .order_by(Instrument.name, Account.name, PositionRegistration.id)
        )
    )
    classification_snapshots = classifications_as_of(
        [registration.instrument_id for registration in registrations],
        as_of_date,
    )
    fixed_deposit_terms = terms_by_registration_ids(
        [
            registration.id
            for registration in registrations
            if registration.instrument.instrument_type == "fixed_deposit"
        ]
    )

    holdings: list[dict[str, object]] = []
    native_totals: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    investment_reporting_total = Decimal("0")
    reporting_valued_count = 0
    stale_count = 0
    attention_items: list[dict[str, object]] = []

    for registration in registrations:
        if (
            registration.opening_date is not None
            and registration.opening_date > as_of_date
        ) or (
            registration.closing_date is not None
            and registration.closing_date <= as_of_date
        ):
            continue
        valued = value_position(
            registration,
            as_of_date,
            reporting_currency=selected_reporting_currency,
            price_stale_days=portfolio.price_stale_days,
            fx_stale_days=portfolio.fx_stale_days,
            statement_stale_days=portfolio.statement_value_stale_days,
        )
        if (
            valued.tracking_mode == "transaction_tracked"
            and valued.quantity == Decimal("0")
        ):
            continue
        classification = classification_snapshots.get(registration.instrument_id)
        role_weights = [
            {"code": role_code, "weight_decimal": weight}
            for role_code, weight in classification.role_weights
        ] if classification else []
        fixed_deposit = (
            maturity_snapshot(
                registration,
                fixed_deposit_terms.get(registration.id),
                as_of_date,
            )
            if registration.instrument.instrument_type == "fixed_deposit"
            else None
        )
        row = {
            **valued.__dict__,
            "instrument_id": registration.instrument_id,
            "instrument_name": registration.instrument.name,
            "account_id": registration.account_id,
            "account_name": registration.account.name,
            "institution_name": registration.account.institution.name,
            "portfolio_share_decimal": (
                registration.account.portfolio_share_decimal
            ),
            "present_access_decimal": (
                registration.account.present_access_decimal
            ),
            "earliest_access_date": registration.account.earliest_access_date,
            "access_note": registration.account.access_note,
            "role_weights": role_weights,
            "primary_role_code": (
                classification.primary_role_code if classification else None
            ),
            "classification_date": (
                classification.effective_date if classification else None
            ),
            "classification_source_note": (
                classification.source_note if classification else None
            ),
            "fire_bucket_code": current_fire_bucket_code(
                registration.instrument.fire_bucket_code
            ),
            "fixed_deposit": (
                fixed_deposit.__dict__ if fixed_deposit is not None else None
            ),
        }
        overlay = _value_overlay(
            valued.reporting_amount,
            registration.account,
            term_locked=(
                fixed_deposit.is_term_liquidity
                if fixed_deposit is not None
                else False
            ),
        )
        row.update(overlay)
        holdings.append(row)
        _record_account_value(
            account_totals[registration.account_id],
            source="investment",
            reporting_amount=valued.reporting_amount,
            status=valued.status,
            overlay=overlay,
        )

        if valued.native_amount is not None:
            native_totals[valued.native_currency] += valued.native_amount
        if valued.reporting_amount is not None:
            investment_reporting_total += valued.reporting_amount
            reporting_valued_count += 1

        if valued.status == "missing":
            attention_items.append(
                {
                    "kind": "missing",
                    "subject_kind": "position",
                    "registration_id": registration.id,
                    "instrument_id": registration.instrument_id,
                    "instrument_name": registration.instrument.name,
                    "account_id": registration.account_id,
                    "account_name": registration.account.name,
                    "institution_name": registration.account.institution.name,
                    "native_currency": valued.native_currency,
                    "reporting_currency": valued.reporting_currency,
                    "value_date": valued.value_date,
                    "fx_date": valued.fx_date,
                    "fx_path": valued.fx_path,
                    "detail": valued.missing_reason,
                    "missing_source": valued.missing_source,
                    "action_kind": "update_value",
                }
            )
        elif valued.status == "stale":
            stale_count += 1
            attention_items.append(
                {
                    "kind": "stale",
                    "subject_kind": "position",
                    "registration_id": registration.id,
                    "instrument_id": registration.instrument_id,
                    "instrument_name": registration.instrument.name,
                    "account_id": registration.account_id,
                    "account_name": registration.account.name,
                    "institution_name": registration.account.institution.name,
                    "native_currency": valued.native_currency,
                    "reporting_currency": valued.reporting_currency,
                    "value_date": valued.value_date,
                    "fx_date": valued.fx_date,
                    "fx_path": valued.fx_path,
                    "detail": _stale_detail(valued.stale_sources),
                    "stale_sources": valued.stale_sources,
                    "action_source": (
                        valued.stale_sources[0]
                        if len(valued.stale_sources) == 1
                        else None
                    ),
                    "action_kind": "update_value",
                }
            )

        if fixed_deposit is not None and fixed_deposit.requires_attention:
            if fixed_deposit.maturity_status == "missing_terms":
                detail = (
                    "Add its start and maturity dates so term liquidity and "
                    "maturity attention can be dated."
                )
            elif fixed_deposit.maturity_status == "approaching":
                detail = (
                    f"Matures in {fixed_deposit.days_to_maturity} days on "
                    f"{fixed_deposit.maturity_date.isoformat()}; maturity action "
                    "is still undecided."
                )
            elif fixed_deposit.maturity_status == "due":
                detail = (
                    f"Matures today ({fixed_deposit.maturity_date.isoformat()}); "
                    "record the bank-confirmed maturity disposition."
                )
            else:
                days_overdue = abs(fixed_deposit.days_to_maturity or 0)
                detail = (
                    f"Matured {days_overdue} days ago on "
                    f"{fixed_deposit.maturity_date.isoformat()}; no cash or "
                    "rollover is posted automatically. Record the confirmed "
                    "maturity disposition."
                )
            attention_items.append(
                {
                    "kind": "fixed_deposit",
                    "subject_kind": "position",
                    "registration_id": registration.id,
                    "instrument_id": registration.instrument_id,
                    "instrument_name": registration.instrument.name,
                    "account_id": registration.account_id,
                    "account_name": registration.account.name,
                    "institution_name": registration.account.institution.name,
                    "native_currency": valued.native_currency,
                    "reporting_currency": valued.reporting_currency,
                    "detail": detail,
                    "maturity_status": fixed_deposit.maturity_status,
                    "maturity_date": fixed_deposit.maturity_date,
                    "action_kind": (
                        "fixed_deposit_disposition"
                        if fixed_deposit.maturity_status in {"due", "overdue"}
                        else "fixed_deposit_terms"
                    ),
                }
            )

    position_count = len(holdings)
    missing_count = position_count - reporting_valued_count
    investment_status = _total_status(
        position_count, reporting_valued_count, stale_count
    )

    cash_balances: list[dict[str, object]] = []
    cash_native_totals: defaultdict[str, Decimal] = defaultdict(
        lambda: Decimal("0")
    )
    cash_reporting_total = Decimal("0")
    cash_reporting_valued_count = 0
    cash_stale_count = 0

    for account in accounts:
        for currency in cash_currencies(
            portfolio.id, account.id, as_of_date=as_of_date
        ):
            cash = resolve_cash(portfolio.id, account.id, currency, as_of_date)
            if cash.amount is None:
                # Aggregate-valued cash and custody cash settled to another account
                # are explicit non-numeric states, never additional portfolio value.
                continue
            if cash.checkpoint is None and not cash.effects:
                # A newly created account with no confirmation or activity is not
                # evidence that its real-world balance is exactly zero.
                continue

            fx = resolve_fx(
                currency,
                selected_reporting_currency,
                as_of_date,
                stale_days=portfolio.fx_stale_days,
            )
            reporting_amount = cash.amount * fx.rate if fx.rate is not None else None
            status = fx.status
            latest_effect_date = (
                cash.effects[-1].effective_date if cash.effects else None
            )
            row = {
                "account_id": account.id,
                "account_name": account.name,
                "institution_name": account.institution.name,
                "as_of_date": as_of_date,
                "native_amount": cash.amount,
                "native_currency": currency,
                "reporting_amount": reporting_amount,
                "reporting_currency": selected_reporting_currency,
                "status": status,
                "source_mode": cash.source_mode,
                "cash_status": cash.status,
                "checkpoint_date": (
                    cash.checkpoint.effective_date if cash.checkpoint else None
                ),
                "latest_cash_effect_date": latest_effect_date,
                "later_cash_effect_amount": cash.later_cash_effect_amount,
                "warnings": cash.warnings,
                "fx_rate": fx.rate,
                "fx_date": fx.effective_date,
                "fx_source_mode": fx.source_mode,
                "fx_path": fx.path,
                "missing_reason": fx.missing_reason,
                "portfolio_share_decimal": account.portfolio_share_decimal,
                "present_access_decimal": account.present_access_decimal,
                "earliest_access_date": account.earliest_access_date,
                "access_note": account.access_note,
            }
            overlay = _value_overlay(reporting_amount, account)
            row.update(overlay)
            cash_balances.append(row)
            _record_account_value(
                account_totals[account.id],
                source="cash",
                reporting_amount=reporting_amount,
                status=status,
                overlay=overlay,
            )
            cash_native_totals[currency] += cash.amount

            if reporting_amount is not None:
                cash_reporting_total += reporting_amount
                cash_reporting_valued_count += 1
            if status == "missing":
                attention_items.append(
                    {
                        "kind": "missing",
                        "subject_kind": "cash",
                        "account_id": account.id,
                        "account_name": account.name,
                        "institution_name": account.institution.name,
                        "instrument_id": None,
                        "registration_id": None,
                        "instrument_name": f"{currency} cash",
                        "native_currency": currency,
                        "reporting_currency": selected_reporting_currency,
                        "value_date": None,
                        "fx_date": fx.effective_date,
                        "fx_path": fx.path,
                        "detail": fx.missing_reason,
                        "missing_source": "fx",
                        "action_kind": "update_value",
                    }
                )
            elif status == "stale":
                cash_stale_count += 1
                attention_items.append(
                    {
                        "kind": "stale",
                        "subject_kind": "cash",
                        "account_id": account.id,
                        "account_name": account.name,
                        "institution_name": account.institution.name,
                        "instrument_id": None,
                        "registration_id": None,
                        "instrument_name": f"{currency} cash",
                        "native_currency": currency,
                        "reporting_currency": selected_reporting_currency,
                        "value_date": None,
                        "fx_date": fx.effective_date,
                        "fx_path": fx.path,
                        "detail": _stale_detail(("fx",)),
                        "stale_sources": ("fx",),
                        "action_source": "fx",
                        "action_kind": "update_value",
                    }
                )

    account_summaries: list[dict[str, object]] = []
    access_attention_items: list[dict[str, object]] = []
    for account in accounts:
        totals = account_totals[account.id]
        account_position_count = totals["position_count"]
        account_cash_count = totals["cash_balance_count"]
        account_item_count = account_position_count + account_cash_count
        account_position_valued = totals["position_reporting_valued_count"]
        account_cash_valued = totals["cash_reporting_valued_count"]
        account_valued_count = account_position_valued + account_cash_valued
        account_missing_count = account_item_count - account_valued_count
        account_status = _total_status(
            account_item_count,
            account_valued_count,
            totals["stale_count"],
        )
        summary = {
            "account_id": account.id,
            "account_name": account.name,
            "institution_name": account.institution.name,
            "cash_tracking_mode": account.cash_tracking_mode,
            "cash_settlement_account_id": account.cash_settlement_account_id,
            "portfolio_share_decimal": account.portfolio_share_decimal,
            "present_access_decimal": account.present_access_decimal,
            "earliest_access_date": account.earliest_access_date,
            "access_note": account.access_note,
            "position_count": account_position_count,
            "position_reporting_valued_count": account_position_valued,
            "cash_balance_count": account_cash_count,
            "cash_reporting_valued_count": account_cash_valued,
            "item_count": account_item_count,
            "reporting_valued_count": account_valued_count,
            "missing_count": account_missing_count,
            "stale_count": totals["stale_count"],
            "status": account_status,
            "investment_reporting_amount": _known_amount(
                totals["investment_reporting_amount"], account_position_valued
            ),
            "cash_reporting_amount": _known_amount(
                totals["cash_reporting_amount"], account_cash_valued
            ),
            "gross_reporting_amount": _known_amount(
                totals["gross_reporting_amount"], account_valued_count
            ),
            "included_reporting_amount": _known_amount(
                totals["included_reporting_amount"], account_valued_count
            ),
            "accessible_reporting_amount": _known_amount(
                totals["accessible_reporting_amount"], account_valued_count
            ),
            "term_liquidity_reporting_amount": _known_amount(
                totals["term_liquidity_reporting_amount"], account_valued_count
            ),
            "excluded_by_share_reporting_amount": _known_amount(
                totals["excluded_by_share_reporting_amount"], account_valued_count
            ),
            "restricted_reporting_amount": _known_amount(
                totals["restricted_reporting_amount"], account_valued_count
            ),
        }
        account_summaries.append(summary)

        if account_item_count and (
            account.portfolio_share_decimal < Decimal("1")
            or account.present_access_decimal < Decimal("1")
        ):
            if account.portfolio_share_decimal == Decimal("0"):
                detail = "This account is excluded from the portfolio value."
            elif account.present_access_decimal == Decimal("0"):
                detail = (
                    "Its portfolio share is included, but none of that share is "
                    "accessible now."
                )
            elif account.portfolio_share_decimal < Decimal("1") and (
                account.present_access_decimal < Decimal("1")
            ):
                detail = (
                    "Only part of this account is included, and only part of that "
                    "included share is accessible now."
                )
            elif account.portfolio_share_decimal < Decimal("1"):
                detail = "Only part of this account is included in the portfolio."
            else:
                detail = "Only part of this account is accessible now."
            access_attention_items.append(
                {
                    "kind": "warning",
                    "subject_kind": "account_access",
                    "account_id": account.id,
                    "account_name": account.name,
                    "institution_name": account.institution.name,
                    "portfolio_share_decimal": account.portfolio_share_decimal,
                    "present_access_decimal": account.present_access_decimal,
                    "earliest_access_date": account.earliest_access_date,
                    "access_note": account.access_note,
                    "gross_reporting_amount": summary["gross_reporting_amount"],
                    "included_reporting_amount": summary[
                        "included_reporting_amount"
                    ],
                    "accessible_reporting_amount": summary[
                        "accessible_reporting_amount"
                    ],
                    "excluded_by_share_reporting_amount": summary[
                        "excluded_by_share_reporting_amount"
                    ],
                    "restricted_reporting_amount": summary[
                        "restricted_reporting_amount"
                    ],
                    "reporting_currency": selected_reporting_currency,
                    "detail": detail,
                    "action_kind": "edit_account",
                }
            )

    cash_balance_count = len(cash_balances)
    cash_missing_count = cash_balance_count - cash_reporting_valued_count
    cash_status = _total_status(
        cash_balance_count, cash_reporting_valued_count, cash_stale_count
    )
    total_item_count = position_count + cash_balance_count
    total_reporting_valued_count = (
        reporting_valued_count + cash_reporting_valued_count
    )
    total_missing_count = total_item_count - total_reporting_valued_count
    total_stale_count = stale_count + cash_stale_count
    total_status = _total_status(
        total_item_count, total_reporting_valued_count, total_stale_count
    )
    total_reporting_amount = (
        investment_reporting_total + cash_reporting_total
        if total_reporting_valued_count
        else None
    )
    included_reporting_total = sum(
        (
            row["included_reporting_amount"]
            for row in account_summaries
            if row["included_reporting_amount"] is not None
        ),
        Decimal("0"),
    )
    accessible_reporting_total = sum(
        (
            row["accessible_reporting_amount"]
            for row in account_summaries
            if row["accessible_reporting_amount"] is not None
        ),
        Decimal("0"),
    )
    term_liquidity_reporting_total = sum(
        (
            row["term_liquidity_reporting_amount"]
            for row in account_summaries
            if row["term_liquidity_reporting_amount"] is not None
        ),
        Decimal("0"),
    )
    excluded_by_share_reporting_total = sum(
        (
            row["excluded_by_share_reporting_amount"]
            for row in account_summaries
            if row["excluded_by_share_reporting_amount"] is not None
        ),
        Decimal("0"),
    )
    restricted_reporting_total = sum(
        (
            row["restricted_reporting_amount"]
            for row in account_summaries
            if row["restricted_reporting_amount"] is not None
        ),
        Decimal("0"),
    )
    portfolio_native_totals = defaultdict(lambda: Decimal("0"), native_totals)
    for currency, amount in cash_native_totals.items():
        portfolio_native_totals[currency] += amount
    allocation = _allocation_summary(
        holdings,
        cash_balances,
        included_reporting_total=included_reporting_total,
        accessible_reporting_total=accessible_reporting_total,
    )
    target_comparison = compare_allocation_to_targets(
        portfolio.id,
        allocation["role_allocation"],
        denominator=allocation["role_classified_denominator_amount"],
        calculation_complete=(
            total_missing_count == 0
            and allocation["role_unclassified_investment_amount"] == 0
        ),
    )
    allocation["role_allocation"] = target_comparison["role_allocation"]
    allocation["allocation_targets_complete"] = target_comparison[
        "targets_complete"
    ]
    allocation["allocation_target_calculation_complete"] = target_comparison[
        "calculation_complete"
    ]
    allocation["planning_now_years"] = target_comparison["now_years"]
    allocation["planning_bridge_years"] = target_comparison["bridge_years"]
    allocation["projects_in_retirement_runway"] = target_comparison[
        "projects_in_retirement_runway"
    ]
    classification_attention_items = allocation["classification_attention_items"]
    attention_items = _deduplicate_value_attention(attention_items)
    source_rows = holdings + cash_balances
    reporting_totals_by_currency = _reporting_breakdown(
        source_rows,
        key_name="native_currency",
        label_name="native_currency",
        include_native=True,
    )
    reporting_totals_by_institution = _reporting_breakdown(
        source_rows,
        key_name="institution_name",
        label_name="institution_name",
    )

    return {
        "portfolio_id": portfolio.id,
        "portfolio_name": portfolio.name,
        "as_of_date": as_of_date,
        "reporting_currency": selected_reporting_currency,
        "holdings": holdings,
        "cash_balances": cash_balances,
        "account_summaries": account_summaries,
        "position_count": position_count,
        "reporting_valued_count": reporting_valued_count,
        "missing_count": missing_count,
        "stale_count": stale_count,
        "investment_reporting_amount": (
            investment_reporting_total if reporting_valued_count else None
        ),
        "investment_status": investment_status,
        "cash_balance_count": cash_balance_count,
        "cash_reporting_valued_count": cash_reporting_valued_count,
        "cash_missing_count": cash_missing_count,
        "cash_stale_count": cash_stale_count,
        "cash_reporting_amount": (
            cash_reporting_total if cash_reporting_valued_count else None
        ),
        "cash_status": cash_status,
        "total_item_count": total_item_count,
        "total_reporting_valued_count": total_reporting_valued_count,
        "total_missing_count": total_missing_count,
        "total_stale_count": total_stale_count,
        "gross_reporting_amount": total_reporting_amount,
        "gross_status": total_status,
        "total_reporting_amount": total_reporting_amount,
        "total_status": total_status,
        "included_reporting_amount": (
            included_reporting_total if total_reporting_valued_count else None
        ),
        "included_status": total_status,
        "accessible_reporting_amount": (
            accessible_reporting_total if total_reporting_valued_count else None
        ),
        "accessible_status": total_status,
        "term_liquidity_reporting_amount": (
            term_liquidity_reporting_total
            if total_reporting_valued_count
            else None
        ),
        "excluded_by_share_reporting_amount": (
            excluded_by_share_reporting_total
            if total_reporting_valued_count
            else None
        ),
        "restricted_reporting_amount": (
            restricted_reporting_total if total_reporting_valued_count else None
        ),
        "native_totals": [
            {"currency": currency, "amount": amount}
            for currency, amount in sorted(native_totals.items())
        ],
        "cash_native_totals": [
            {"currency": currency, "amount": amount}
            for currency, amount in sorted(cash_native_totals.items())
        ],
        "portfolio_native_totals": [
            {"currency": currency, "amount": amount}
            for currency, amount in sorted(portfolio_native_totals.items())
        ],
        "reporting_totals_by_currency": reporting_totals_by_currency,
        "reporting_totals_by_institution": reporting_totals_by_institution,
        "attention_items": attention_items,
        "value_attention_items": [
            item for item in attention_items if item.get("action_kind") == "update_value"
        ],
        "access_attention_items": access_attention_items,
        "all_attention_items": attention_items + access_attention_items,
        "actionable_attention_items": (
            attention_items + classification_attention_items
        ),
        **allocation,
    }
