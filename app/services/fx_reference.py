"""One public download, coherent dated sets, and explicit local corrections."""
from __future__ import annotations

import json
import re
import socket
from http.client import HTTPException
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from urllib.error import HTTPError, URLError
from urllib.request import Request, HTTPRedirectHandler, build_opener

from sqlalchemy import select, func, text

from app.extensions import db
from app.models import FxReferenceSet, Account, Institution, RelationshipRule

SOURCE_URL = "https://tassidicambio.bancaditalia.it/terzevalute-wf-web/rest/v1.0/latestRates?lang=en"
MAX_BYTES = 2 * 1024 * 1024
CODE = re.compile(r"[A-Z]{3}\Z")


class FxReferenceError(ValueError):
    pass


@dataclass(frozen=True)
class ReferenceData:
    effective_date: date
    rates: dict[str, str]
    unavailable: tuple[str, ...] = ()


def rate_text(value: str, *, manual: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise FxReferenceError("A rate is not a decimal number.")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise FxReferenceError("A rate is not a decimal number.") from exc
    if not number.is_finite() or not Decimal("0.000000000001") <= number <= Decimal("9999999999999999"):
        raise FxReferenceError("Rates must be positive finite decimals within the supported range.")
    if len(number.as_tuple().digits) > 28:
        raise FxReferenceError("Use at most 28 significant digits; the rate will not be silently rounded.")
    if manual:
        from app.decimal_policy import exact_units
        try:
            exact_units(number, 6)
        except (ValueError, TypeError) as error:
            raise FxReferenceError(str(error)) from error
    return format(number.normalize(), "f")


def validate_rates(rates: object) -> dict[str, str]:
    if not isinstance(rates, dict) or not 1 <= len(rates) <= 300:
        raise FxReferenceError("The rate table is missing or has an unexpected size.")
    result = {}
    for code, value in rates.items():
        if not isinstance(code, str) or not CODE.fullmatch(code):
            raise FxReferenceError("The rate table contains an invalid currency code.")
        result[code] = rate_text(value)
    if result.get("EUR") != "1":
        raise FxReferenceError("The rate table must use 1 EUR as its reference.")
    return dict(sorted(result.items()))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FxReferenceError("The JSON contains duplicate fields; its format may have changed.")
        result[key] = value
    return result


def parse_download(raw: bytes, today: date) -> ReferenceData:
    if len(raw) > MAX_BYTES:
        raise FxReferenceError("The download is unexpectedly large. The provider format may have changed.")
    try:
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise FxReferenceError("The download could not be read as JSON. The provider may have returned an error page or changed its format.") from exc
    if not isinstance(payload, dict):
        raise FxReferenceError("The JSON format has changed: a rate table was expected.")
    info, rows = payload.get("resultsInfo"), payload.get("latestRates")
    if (not isinstance(info, dict) or not isinstance(rows, list) or not 1 <= len(rows) <= 500
            or type(info.get("totalRecords")) is not int or info["totalRecords"] != len(rows)
            or not str(info.get("notice", "")).startswith("Foreign currency amount for 1 Euro")):
        raise FxReferenceError("The JSON format or EUR direction is unrecognized. The provider may have changed its format.")
    observations = {}
    dates = set()
    for row in rows:
        if not isinstance(row, dict) or not {"isoCode", "eurRate", "referenceDate"} <= row.keys():
            raise FxReferenceError("The JSON is missing currency, rate or reference-date fields.")
        code, value = row["isoCode"], row["eurRate"]
        if not isinstance(code, str) or not CODE.fullmatch(code):
            raise FxReferenceError("The download contains an invalid currency code.")
        try:
            effective = date.fromisoformat(row["referenceDate"])
            if effective.isoformat() != row["referenceDate"] or effective > today:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise FxReferenceError("The download contains an invalid or future reference date.") from exc
        dates.add(effective)
        normalized = None if value == "N.A." else rate_text(value)
        if code in observations and observations[code] != normalized:
            raise FxReferenceError(f"The download contains conflicting {code} rates. Nothing was saved.")
        observations[code] = normalized
    if len(dates) != 1:
        raise FxReferenceError("The download mixes reference dates. A single dated set is required.")
    rates = {c: v for c, v in observations.items() if v is not None}
    # The source's EUR identity is checked rather than silently repaired.
    return ReferenceData(dates.pop(), validate_rates(rates), tuple(sorted(c for c,v in observations.items() if v is None)))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FxReferenceError("The provider moved its download address. Use manual entry while the connection is updated.")


def download_reference_rates(today: date) -> ReferenceData:
    """No credentials, portfolio data, redirects, retries or background activity."""
    request = Request(SOURCE_URL, headers={"Accept": "application/json", "User-Agent": "LookThrough-local/1.0"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        raise FxReferenceError(f"Banca d’Italia returned HTTP {exc.code}. Its download service may be unavailable or changed.") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise FxReferenceError("The rate download timed out. Try again later or enter rates manually.") from exc
    except HTTPException as exc:
        raise FxReferenceError("The download was interrupted or incomplete. Try again later or enter rates manually.") from exc
    except (URLError, OSError) as exc:
        raise FxReferenceError("Could not connect securely to Banca d’Italia. Check your connection or use manual entry.") from exc
    return parse_download(raw, today)


def latest_set(as_of: date | None = None) -> FxReferenceSet | None:
    query = select(FxReferenceSet)
    if as_of is not None:
        query = query.where(FxReferenceSet.effective_date <= as_of)
    return db.session.scalar(query.order_by(FxReferenceSet.effective_date.desc(), FxReferenceSet.id.desc()).limit(1))


def state_id() -> int:
    return db.session.scalar(select(func.max(FxReferenceSet.id))) or 0


def state_digest() -> str:
    rows = db.session.execute(select(FxReferenceSet.id, FxReferenceSet.effective_date,
                                    FxReferenceSet.rates_json, FxReferenceSet.created_at).order_by(FxReferenceSet.id)).all()
    return sha256(repr(rows).encode()).hexdigest()


def set_rates(record: FxReferenceSet) -> dict[str, str]:
    return json.loads(record.rates_json)


def currency_uses(portfolio, as_of: date) -> dict[str, list[str]]:
    """Bound maintenance by real sources and explicit settings, never by FX history."""
    from app.services.portfolio_summary import build_portfolio_summary
    from app.services.retirement_plans import adopted_plan, plan_incomes
    uses: dict[str, list[str]] = {}
    def add(code, purpose):
        if code and purpose not in uses.setdefault(code, []):
            uses[code].append(purpose)
    if portfolio is None:
        return uses
    for account in db.session.scalars(select(Account).where(Account.portfolio_id == portfolio.id, Account.is_active.is_(True))):
        add(account.default_currency_code, f"Account: {account.name}")
    summary = build_portfolio_summary(portfolio, as_of)
    for row in summary["holdings"] + summary["cash_balances"]:
        add(row["native_currency"], "Holdings and cash")
    add(portfolio.reporting_currency_code, "Saved reporting currency")
    add(portfolio.annual_spending_currency_code, "Spending")
    plan = adopted_plan(portfolio.id)
    if plan:
        add(plan.currency_code, "Retirement plan")
        add(plan.terminal_legacy_target_currency_code, "Legacy goal")
        for income in plan_incomes(plan.id):
            add(income.currency_code, "Retirement income")
    for rule in db.session.scalars(select(RelationshipRule).join(Institution).where(Institution.portfolio_id == portfolio.id, RelationshipRule.is_active.is_(True))):
        add(rule.threshold_currency_code, f"Relationship: {rule.name}")
    return dict(sorted(uses.items()))


def reporting_choices(portfolio, as_of: date) -> list[str]:
    uses = currency_uses(portfolio, as_of)
    # Preserve the explicitly saved reporting preference even if no asset uses it.
    return sorted(c for c, purposes in uses.items() if c == portfolio.reporting_currency_code
                  or any(p == "Holdings and cash" or p.startswith("Account:") for p in purposes))


def missing_required(rates: dict[str, str], portfolio, as_of: date) -> list[str]:
    return sorted(set(currency_uses(portfolio, as_of)) - rates.keys())


def changes_for(data: ReferenceData) -> list[dict]:
    previous = latest_set(data.effective_date)
    old = set_rates(previous) if previous else {}
    changes = []
    for code in sorted(set(old) | data.rates.keys()):
        before, after = old.get(code), data.rates.get(code)
        if before == after:
            continue
        change = abs(Decimal(after) / Decimal(before) - 1) if before and after else None
        changes.append({"code": code, "before": before, "after": after,
                        "percent": change * 100 if change is not None else None,
                        "large": change is not None and change > Decimal("0.10")})
    return changes


def save_set(data: ReferenceData, *, source: str, note: str, expected_state: int,
             replace: bool = False, acknowledge: bool = False, expected_digest: str | None = None) -> bool:
    """Serialize saves, retain prior editions, and never rewrite raw FX history."""
    validate_rates(data.rates)
    if source not in {"banca_italia", "manual"} or not note.strip() or len(note) > 2000:
        raise FxReferenceError("Enter a source or correction reason of at most 2,000 characters.")
    try:
        db.session.execute(text("BEGIN IMMEDIATE"))
        if state_id() != expected_state or (expected_digest is not None and state_digest() != expected_digest):
            raise FxReferenceError("Saved rates changed in another tab. Review the current rates before saving again.")
        previous = latest_set(data.effective_date)
        same_day = previous is not None and previous.effective_date == data.effective_date
        retained = set_rates(previous) if same_day else {}
        if source == 'manual':
            for code, value in data.rates.items():
                if retained.get(code) != value:
                    rate_text(value, manual=True)
        if same_day and set_rates(previous) == data.rates:
            db.session.rollback()
            return False
        if same_day and not replace:
            raise FxReferenceError("Confirm replacement for this reference date. The previous edition will be retained.")
        if any(c["large"] for c in changes_for(data)) and not acknowledge:
            raise FxReferenceError("Check and acknowledge the rates changing by more than 10%.")
        db.session.add(FxReferenceSet(effective_date=data.effective_date,
                                    rates_json=json.dumps(data.rates, sort_keys=True),
                                    source=source, source_note=note.strip()))
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        raise
