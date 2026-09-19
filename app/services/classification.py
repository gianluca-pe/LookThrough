"""Instrument role classification and compact FIRE metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping, Sequence

from sqlalchemy import delete, func, select

from app.conventions import normalize_currency_code, parse_decimal, parse_iso_date
from app.extensions import db
from app.decimal_policy import exact_units
from app.models import (
    CAPITAL_CERTAINTY_CODES,
    CREDIT_BAND_CODES,
    CURRENCY_TREATMENT_CODES,
    DURATION_BAND_CODES,
    ECONOMIC_ROLE_CODES,
    EQUITY_SENSITIVITY_CODES,
    FIRE_BUCKET_CODES,
    HEDGING_STATUS_CODES,
    LIQUIDITY_PROFILE_CODES,
    Instrument,
    InstrumentClassification,
)


CLASSIFICATION_TOTAL_TOLERANCE = Decimal("0.000001")
LEGACY_ROLE_MAP = {
    "liquidity": "liquidity",
    "ballast": "liquidity",
    "inflation_defence": "income",
    "income_credit": "income",
    "growth": "equity",
    "opportunistic": "equity",
    "diversifiers": "alternatives",
}
LEGACY_BUCKET_MAP = {
    "now": "now",
    "next": "bridge",
    "bridge": "bridge",
    "growth": "growth",
    "flex": "projects",
}


class ClassificationValidationError(ValueError):
    """A classification command is invalid or needs explicit replacement."""

    def __init__(self, message: str, *, field: str = "role_weights") -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class ClassificationSnapshot:
    instrument_id: int
    effective_date: date
    role_weights: tuple[tuple[str, Decimal], ...]
    source_note: str | None

    @property
    def primary_role_code(self) -> str | None:
        if len(self.role_weights) != 1:
            return None
        role_code, weight = self.role_weights[0]
        return role_code if weight == Decimal("1") else None


@dataclass(frozen=True)
class ClassificationCommand:
    instrument_id: int
    effective_date: date
    role_weights: Mapping[str, Decimal | str | int]
    source_note: str | None = None
    fund_base_currency_code: str | None = None
    hedging_status: str | None = None
    fire_bucket_code: str | None = None
    capital_certainty_code: str | None = None
    equity_sensitivity_code: str | None = None
    liquidity_profile_code: str | None = None
    duration_band_code: str | None = None
    credit_band_code: str | None = None
    currency_treatment_code: str | None = None
    preserve_existing_metadata: bool = False
    replace_existing: bool = False


def _optional_code(
    value: str | None,
    allowed: Sequence[str],
    *,
    field: str,
    label: str,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ClassificationValidationError(f"{label} must be text.", field=field)
    if not value.strip():
        return None
    candidate = value.strip()
    if candidate not in allowed:
        raise ClassificationValidationError(
            f"{label} is not supported.", field=field
        )
    return candidate


def validate_role_weights(
    weights: Mapping[str, Decimal | str | int],
) -> tuple[tuple[str, Decimal], ...]:
    """Validate a complete dated set, preserving supported legacy mappings."""

    if not weights:
        raise ClassificationValidationError(
            "Choose at least one allocation role.", field="role_weights"
        )

    supported_inputs = set(ECONOMIC_ROLE_CODES) | set(LEGACY_ROLE_MAP)
    unsupported = set(weights) - supported_inputs
    if unsupported:
        raise ClassificationValidationError(
            "Allocation role is not supported.", field="role_weights"
        )

    consolidated = {role_code: Decimal("0") for role_code in ECONOMIC_ROLE_CODES}
    for input_code, raw_weight in weights.items():
        role_code = LEGACY_ROLE_MAP.get(input_code, input_code)
        try:
            weight = parse_decimal(
                raw_weight, field_name=f"{input_code} weight"
            )
            exact_units(weight, 8)
        except (TypeError, ValueError) as exc:
            raise ClassificationValidationError(
                str(exc), field=role_code
            ) from exc
        if weight <= 0 or weight > 1:
            raise ClassificationValidationError(
                "Each allocation role weight must be greater than 0% and at most 100%.",
                field=input_code,
            )
        consolidated[role_code] += weight

    parsed = [
        (role_code, consolidated[role_code])
        for role_code in ECONOMIC_ROLE_CODES
        if consolidated[role_code] > 0
    ]

    total = sum((weight for _, weight in parsed), Decimal("0"))
    if abs(total - Decimal("1")) > CLASSIFICATION_TOTAL_TOLERANCE:
        raise ClassificationValidationError(
            f"Allocation role weights must total 100% (current total: {total * 100}%).",
            field="role_weights",
        )
    return tuple(parsed)


def classifications_as_of(
    instrument_ids: Sequence[int],
    as_of_date: date,
) -> dict[int, ClassificationSnapshot]:
    """Return the latest eligible complete role set for each instrument."""

    unique_ids = tuple(dict.fromkeys(instrument_ids))
    if not unique_ids:
        return {}
    rows = list(
        db.session.scalars(
            select(InstrumentClassification)
            .where(
                InstrumentClassification.instrument_id.in_(unique_ids),
                InstrumentClassification.effective_date <= as_of_date,
            )
            .order_by(
                InstrumentClassification.instrument_id,
                InstrumentClassification.effective_date.desc(),
                InstrumentClassification.economic_role_code,
            )
        )
    )

    grouped: dict[int, list[InstrumentClassification]] = {}
    latest_dates: dict[int, date] = {}
    for row in rows:
        latest_date = latest_dates.setdefault(row.instrument_id, row.effective_date)
        if row.effective_date == latest_date:
            grouped.setdefault(row.instrument_id, []).append(row)

    snapshots: dict[int, ClassificationSnapshot] = {}
    for instrument_id, rows_for_instrument in grouped.items():
        try:
            role_weights = validate_role_weights({
                row.economic_role_code: row.weight_decimal
                for row in rows_for_instrument
            })
        except ClassificationValidationError:
            # An invalid latest set is unknown, never an older classification
            # or a partial allocation that silently discards source capital.
            continue
        snapshots[instrument_id] = ClassificationSnapshot(
            instrument_id=instrument_id,
            effective_date=rows_for_instrument[0].effective_date,
            role_weights=role_weights,
            source_note=rows_for_instrument[0].source_note,
        )
    return snapshots


def current_fire_bucket_code(code: str | None) -> str | None:
    """Map current or legacy stored input to a supported bucket code."""

    if code is None:
        return None
    mapped = LEGACY_BUCKET_MAP.get(code, code)
    return mapped if mapped in FIRE_BUCKET_CODES else None


def _validated_fire_bucket(code: str | None) -> str | None:
    if code is None:
        return None
    if not isinstance(code, str):
        raise ClassificationValidationError(
            "FIRE bucket must be text.", field="fire_bucket_code"
        )
    candidate = code.strip()
    if not candidate:
        return None
    mapped = current_fire_bucket_code(candidate)
    if mapped is None:
        raise ClassificationValidationError(
            "FIRE bucket is not supported.", field="fire_bucket_code"
        )
    return mapped


def save_classification(
    portfolio_id: int,
    command: ClassificationCommand,
) -> ClassificationSnapshot:
    """Validate and stage one dated role set plus current instrument metadata."""

    instrument = db.session.scalar(
        select(Instrument).where(
            Instrument.id == command.instrument_id,
            Instrument.portfolio_id == portfolio_id,
            Instrument.is_active.is_(True),
        )
    )
    if instrument is None:
        raise ClassificationValidationError(
            "Instrument was not found in this portfolio.", field="instrument_id"
        )

    try:
        effective_date = parse_iso_date(
            command.effective_date, field_name="Classification date"
        )
    except (TypeError, ValueError) as exc:
        raise ClassificationValidationError(
            str(exc), field="effective_date"
        ) from exc
    role_weights = validate_role_weights(command.role_weights)
    fire_bucket_code = _validated_fire_bucket(command.fire_bucket_code)
    if not command.preserve_existing_metadata:
        fund_base_currency_code = None
        if command.fund_base_currency_code is not None and not isinstance(
            command.fund_base_currency_code, str
        ):
            raise ClassificationValidationError(
                "Fund base currency must be text.", field="fund_base_currency_code"
            )
        if command.fund_base_currency_code and command.fund_base_currency_code.strip():
            try:
                fund_base_currency_code = normalize_currency_code(
                    command.fund_base_currency_code,
                    field_name="Fund base currency",
                )
            except (TypeError, ValueError) as exc:
                raise ClassificationValidationError(
                    str(exc), field="fund_base_currency_code"
                ) from exc

        metadata = {
            "hedging_status": _optional_code(
                command.hedging_status,
                HEDGING_STATUS_CODES,
                field="hedging_status",
                label="Hedging status",
            ),
            "capital_certainty_code": _optional_code(
                command.capital_certainty_code,
                CAPITAL_CERTAINTY_CODES,
                field="capital_certainty_code",
                label="Capital certainty",
            ),
            "equity_sensitivity_code": _optional_code(
                command.equity_sensitivity_code,
                EQUITY_SENSITIVITY_CODES,
                field="equity_sensitivity_code",
                label="Equity sensitivity",
            ),
            "liquidity_profile_code": _optional_code(
                command.liquidity_profile_code,
                LIQUIDITY_PROFILE_CODES,
                field="liquidity_profile_code",
                label="Liquidity",
            ),
            "duration_band_code": _optional_code(
                command.duration_band_code,
                DURATION_BAND_CODES,
                field="duration_band_code",
                label="Duration band",
            ),
            "credit_band_code": _optional_code(
                command.credit_band_code,
                CREDIT_BAND_CODES,
                field="credit_band_code",
                label="Credit band",
            ),
            "currency_treatment_code": _optional_code(
                command.currency_treatment_code,
                CURRENCY_TREATMENT_CODES,
                field="currency_treatment_code",
                label="Currency treatment",
            ),
        }

    source_note = None
    if command.source_note is not None:
        if not isinstance(command.source_note, str):
            raise ClassificationValidationError(
                "Classification source note must be text.", field="source_note"
            )
        source_note = command.source_note.strip() or None
        if source_note and len(source_note) > 1000:
            raise ClassificationValidationError(
                "Classification source note must be 1,000 characters or fewer.",
                field="source_note",
            )

    existing_count = db.session.scalar(
        select(func.count(InstrumentClassification.id)).where(
            InstrumentClassification.instrument_id == instrument.id,
            InstrumentClassification.effective_date == effective_date,
        )
    )
    if existing_count and not command.replace_existing:
        raise ClassificationValidationError(
            "A classification already exists for this date. Confirm replacement to save.",
            field="replace_existing",
        )
    if existing_count:
        db.session.execute(
            delete(InstrumentClassification).where(
                InstrumentClassification.instrument_id == instrument.id,
                InstrumentClassification.effective_date == effective_date,
            )
        )

    instrument.fire_bucket_code = fire_bucket_code
    if not command.preserve_existing_metadata:
        instrument.fund_base_currency_code = fund_base_currency_code
        for field, value in metadata.items():
            setattr(instrument, field, value)

    for role_code, weight in role_weights:
        db.session.add(
            InstrumentClassification(
                instrument_id=instrument.id,
                economic_role_code=role_code,
                weight_decimal=weight,
                effective_date=effective_date,
                source_note=source_note,
            )
        )
    db.session.flush()
    return ClassificationSnapshot(
        instrument_id=instrument.id,
        effective_date=effective_date,
        role_weights=role_weights,
        source_note=source_note,
    )
