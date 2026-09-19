"""Deterministic direct, inverse, and single-pivot FX resolution."""

from __future__ import annotations

from app.decimal_policy import exact_units, rounded

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select

from app.extensions import db
from app.models import FxRate


@dataclass(frozen=True)
class FxResolution:
    rate: Decimal | None
    effective_date: date | None
    source_mode: str
    path: tuple[str, ...]
    status: str
    missing_reason: str | None = None


FX_CHANGE_WARNING_THRESHOLD = Decimal("0.10")


@dataclass(frozen=True)
class FxChangeAssessment:
    previous_rate: Decimal | None
    previous_date: date | None
    change_ratio: Decimal | None
    change_percent: Decimal | None
    requires_acknowledgement: bool


@dataclass(frozen=True)
class FxCorrectionContext:
    """Existing same-day sources and the explicit proposed replacement."""

    existing_sources: tuple[FxRate, ...]
    proposed_base_currency: str
    proposed_quote_currency: str
    proposed_quote_per_base: Decimal


def same_day_fx_rates(
    base_currency: str,
    quote_currency: str,
    effective_date: date,
) -> tuple[FxRate, ...]:
    """Return every direct/inverse source for one logical pair and date."""

    rows = tuple(
        db.session.scalars(
            select(FxRate)
            .where(
                FxRate.effective_date == effective_date,
                (
                    (FxRate.base_currency_code == base_currency)
                    & (FxRate.quote_currency_code == quote_currency)
                )
                | (
                    (FxRate.base_currency_code == quote_currency)
                    & (FxRate.quote_currency_code == base_currency)
                ),
            )
            .order_by(FxRate.id)
        )
    )
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row.base_currency_code != base_currency,
                row.id,
            ),
        )
    )


def fx_correction_context(
    existing_sources: tuple[FxRate, ...],
    *,
    base_currency: str,
    quote_currency: str,
    quote_per_base: Decimal,
) -> FxCorrectionContext:
    exact_units(quote_per_base, 6)
    if quote_per_base <= 0:
        raise ValueError('FX rate must be greater than zero.')
    if any(row.base_currency_code != base_currency for row in existing_sources):
        inverse = rounded(Decimal(1) / quote_per_base, 6)
        exact_units(inverse, 6)
        if inverse == 0:
            raise ValueError('The inverse rate would round to zero at six decimal places. Use a smaller rate.')
    return FxCorrectionContext(
        existing_sources=existing_sources,
        proposed_base_currency=base_currency,
        proposed_quote_currency=quote_currency,
        proposed_quote_per_base=quote_per_base,
    )


def apply_same_day_fx_correction(
    context: FxCorrectionContext,
) -> None:
    """Replace one logical same-day rate while retaining prior source values.

    Historical databases may contain both direct and inverse rows for the same
    date. Keeping their stored directions but updating them reciprocally prevents
    one old row from silently winning FX resolution after a correction.
    """

    for row in context.existing_sources:
        previous = format(row.quote_per_base_amount.normalize(), "f")
        note = (
            f"Corrected {row.effective_date.isoformat()}: previous 1 "
            f"{row.base_currency_code} = {previous} "
            f"{row.quote_currency_code}."
        )
        row.source_note = f"{row.source_note}\n{note}" if row.source_note else note
        if (
            row.base_currency_code == context.proposed_base_currency
            and row.quote_currency_code == context.proposed_quote_currency
        ):
            row.quote_per_base_amount = context.proposed_quote_per_base
        else:
            row.quote_per_base_amount = (
                rounded(Decimal("1") / context.proposed_quote_per_base, 6)
            )


def assess_fx_change(
    base_currency: str,
    quote_currency: str,
    effective_date: date,
    proposed_quote_per_base: Decimal,
) -> FxChangeAssessment:
    """Compare with the latest earlier direct/inverse row in one direction."""

    if isinstance(effective_date, datetime) or not isinstance(effective_date, date):
        raise ValueError("Enter a valid FX date.")
    if (
        not isinstance(proposed_quote_per_base, Decimal)
        or not proposed_quote_per_base.is_finite()
        or proposed_quote_per_base <= 0
    ):
        raise ValueError("FX rate must be a finite number greater than zero.")
    previous = db.session.scalar(
        select(FxRate)
        .where(
            FxRate.effective_date < effective_date,
            (
                (FxRate.base_currency_code == base_currency)
                & (FxRate.quote_currency_code == quote_currency)
            )
            | (
                (FxRate.base_currency_code == quote_currency)
                & (FxRate.quote_currency_code == base_currency)
            ),
        )
        .order_by(FxRate.effective_date.desc(), FxRate.id.desc())
        .limit(1)
    )
    if previous is None:
        return FxChangeAssessment(None, None, None, None, False)

    normalized = (
        previous.quote_per_base_amount
        if (
            previous.base_currency_code == base_currency
            and previous.quote_currency_code == quote_currency
        )
        else Decimal("1") / previous.quote_per_base_amount
    )
    change_ratio = abs(proposed_quote_per_base / normalized - Decimal("1"))
    return FxChangeAssessment(
        previous_rate=normalized,
        previous_date=previous.effective_date,
        change_ratio=change_ratio,
        change_percent=change_ratio * Decimal("100"),
        requires_acknowledgement=(
            change_ratio > FX_CHANGE_WARNING_THRESHOLD
        ),
    )


def resolve_fx(
    base_currency: str,
    quote_currency: str,
    as_of_date: date,
    *,
    stale_days: int,
) -> FxResolution:
    """Resolve quote units per base unit using at most one disclosed pivot."""

    if base_currency == quote_currency:
        return FxResolution(
            Decimal("1"), None, "identity", (base_currency,), "current"
        )

    from app.services.fx_reference import latest_set, set_rates
    reference = latest_set(as_of_date)
    if reference is not None:
        rates = set_rates(reference)
        path = ((base_currency, quote_currency) if "EUR" in {base_currency, quote_currency}
                else (base_currency, "EUR", quote_currency))
        if base_currency not in rates or quote_currency not in rates:
            missing = ", ".join(c for c in (base_currency, quote_currency) if c not in rates)
            return FxResolution(None, reference.effective_date, "missing", path, "missing",
                                f"No {missing} rate in the reference set dated {reference.effective_date}. Update rates in Settings.")
        base_rate = rounded(Decimal(rates[base_currency]), 6)
        quote_rate = rounded(Decimal(rates[quote_currency]), 6)
        if base_rate <= 0 or quote_rate <= 0:
            return FxResolution(None, reference.effective_date, "missing", path, "missing",
                                "A reference rate is too small for six-decimal calculations. Review rates in Settings.")
        return FxResolution(quote_rate / base_rate,
                            reference.effective_date, reference.source, path,
                            _freshness(reference.effective_date, as_of_date, stale_days))

    rows = list(
        db.session.scalars(
            select(FxRate)
            .where(FxRate.effective_date <= as_of_date)
            .order_by(FxRate.effective_date.desc(), FxRate.id.desc())
        )
    )
    edges: dict[tuple[str, str], tuple[Decimal, date, str]] = {}
    for row in rows:
        direct = (row.base_currency_code, row.quote_currency_code)
        inverse = (row.quote_currency_code, row.base_currency_code)
        edges.setdefault(
            direct, (row.quote_per_base_amount, row.effective_date, "direct")
        )
        edges.setdefault(
            inverse,
            (Decimal("1") / row.quote_per_base_amount, row.effective_date, "inverse"),
        )

    direct_edge = edges.get((base_currency, quote_currency))
    if direct_edge is not None:
        rate, effective, mode = direct_edge
        return FxResolution(
            rate,
            effective,
            mode,
            (base_currency, quote_currency),
            _freshness(effective, as_of_date, stale_days),
        )

    paths = []
    currencies = sorted({currency for edge in edges for currency in edge})
    for pivot in currencies:
        if pivot in {base_currency, quote_currency}:
            continue
        first = edges.get((base_currency, pivot))
        second = edges.get((pivot, quote_currency))
        if first is not None and second is not None:
            effective = min(first[1], second[1])
            paths.append((effective, pivot, first[0] * second[0]))

    if paths:
        effective, pivot, rate = sorted(
            paths, key=lambda item: (-item[0].toordinal(), item[1])
        )[0]
        return FxResolution(
            rate,
            effective,
            "pivot",
            (base_currency, pivot, quote_currency),
            _freshness(effective, as_of_date, stale_days),
        )

    return FxResolution(
        None,
        None,
        "missing",
        (base_currency, quote_currency),
        "missing",
        f"No eligible FX path from {base_currency} to {quote_currency}",
    )


def _freshness(effective: date, as_of_date: date, stale_days: int) -> str:
    return "stale" if (as_of_date - effective).days > stale_days else "current"
