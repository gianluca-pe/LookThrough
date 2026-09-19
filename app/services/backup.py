"""Versioned logical backup with disposable validation and atomic restore."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from secrets import token_hex
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, Text, create_engine, select
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models.types import UTCDateTime, ScaledDecimal
from app.decimal_policy import DECIMAL_FIELDS, normalize_legacy, validate_normalized_history
from app.services.classification import ClassificationValidationError, validate_role_weights


BACKUP_FORMAT = "lookthrough-local-backup"
BACKUP_VERSION = 11
SUPPORTED_BACKUP_VERSIONS = set(range(1, BACKUP_VERSION + 1))
MAX_BACKUP_BYTES = 20 * 1024 * 1024
TABLE_ORDER = (
    "portfolios",
    "allocation_targets",
    "retirement_assumptions",
    "retirement_plans",
    "retirement_income",
    "retirement_scenarios",
    "portfolio_snapshots",
    "institutions",
    "accounts",
    "instruments",
    "instrument_classifications",
    "transactions",
    "postings",
    "cash_balance_checkpoints",
    "prices",
    "position_registrations",
    "fixed_deposits",
    "valuation_observations",
    "fx_rates",
    "fx_reference_sets",
    "relationship_rules",
    "decimal_conversions",
)
TABLE_ORDER_V10 = tuple(name for name in TABLE_ORDER if name != "decimal_conversions")
TABLE_ORDER_V9 = tuple(name for name in TABLE_ORDER_V10 if name != "fx_reference_sets")
TABLE_ORDER_V7 = tuple(name for name in TABLE_ORDER_V9 if name != "retirement_scenarios")
TABLE_ORDER_V5 = tuple(name for name in TABLE_ORDER_V7 if name not in {"retirement_plans", "retirement_income"})
TABLE_ORDER_V4 = tuple(name for name in TABLE_ORDER_V5 if name != "retirement_assumptions")
TABLE_ORDER_V3 = tuple(
    name for name in TABLE_ORDER_V4 if name != "portfolio_snapshots"
)
TABLE_ORDER_V1 = tuple(
    name for name in TABLE_ORDER_V3 if name != "allocation_targets"
)
_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")


class BackupValidationError(ValueError):
    """A backup cannot be trusted or restored."""


@dataclass(frozen=True)
class BackupPreview:
    created_at: datetime
    version: int
    table_counts: dict[str, int]
    total_records: int


@dataclass(frozen=True)
class ValidatedBackup:
    payload: dict[str, Any]
    decoded_tables: dict[str, list[dict[str, Any]]]
    preview: BackupPreview


def export_backup() -> bytes:
    """Serialize every local source record without converting Decimals to floats."""

    tables: dict[str, list[dict[str, Any]]] = {}
    # sqlite3's legacy transaction mode does not BEGIN for SELECT. Use a
    # dedicated connection and an explicit read transaction so every table sees
    # the same committed state, without flushing or ending the caller's work.
    with db.engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        for name in TABLE_ORDER:
            table = db.metadata.tables[name]
            primary_key = list(table.primary_key.columns)
            statement = select(table)
            if primary_key:
                statement = statement.order_by(*primary_key)
            records = connection.execute(statement).mappings()
            tables[name] = [
                {column.name: _encode(row[column.name]) for column in table.columns}
                for row in records
            ]
    payload = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "tables": tables,
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def validate_backup(blob: bytes) -> ValidatedBackup:
    """Validate shape, types, constraints, and relationships in a clean database."""

    if not blob:
        raise BackupValidationError("Choose a non-empty LookThrough backup file.")
    if len(blob) > MAX_BACKUP_BYTES:
        raise BackupValidationError("The backup is larger than the 20 MB local limit.")
    try:
        payload = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupValidationError("The selected file is not valid UTF-8 JSON.") from error
    if not isinstance(payload, dict):
        raise BackupValidationError("The backup must contain one JSON object.")
    if set(payload) != {"format", "version", "created_at", "tables"}:
        raise BackupValidationError("The backup header does not match this app version.")
    if payload.get("format") != BACKUP_FORMAT:
        raise BackupValidationError("This is not a LookThrough local backup.")
    if (
        isinstance(payload.get("version"), bool)
        or not isinstance(payload.get("version"), int)
        or payload.get("version") not in SUPPORTED_BACKUP_VERSIONS
    ):
        raise BackupValidationError(
            f"Backup version {payload.get('version')!r} is not supported by this app."
        )
    try:
        created_at = _parse_datetime(payload["created_at"], "created_at")
    except KeyError as error:
        raise BackupValidationError("The backup has no creation timestamp.") from error
    raw_tables = payload.get("tables")
    if not isinstance(raw_tables, dict):
        raise BackupValidationError("The backup has no valid tables section.")
    source_version = payload["version"]
    if source_version == 1:
        expected_table_order = TABLE_ORDER_V1
    elif source_version < 4:
        expected_table_order = TABLE_ORDER_V3
    elif source_version == 4:
        expected_table_order = TABLE_ORDER_V4
    elif source_version == 5:
        expected_table_order = TABLE_ORDER_V5
    elif source_version < 8:
        expected_table_order = TABLE_ORDER_V7
    elif source_version < 10:
        expected_table_order = TABLE_ORDER_V9
    elif source_version == 10:
        expected_table_order = TABLE_ORDER_V10
    else:
        expected_table_order = TABLE_ORDER
    if set(raw_tables) != set(expected_table_order):
        raise BackupValidationError("The backup table set does not match this app version.")

    decoded: dict[str, list[dict[str, Any]]] = {}
    for name in TABLE_ORDER:
        raw_rows = raw_tables.get(name, [])
        if not isinstance(raw_rows, list):
            raise BackupValidationError(f"Table {name} must be a list of records.")
        table = db.metadata.tables[name]
        expected_columns = {column.name for column in table.columns}
        if source_version < 3 and name == "portfolios":
            expected_columns.remove("annual_inflation_decimal")
        if source_version < 9 and name == "retirement_plans":
            expected_columns -= {"planning_mode", "annual_savings_amount", "legacy_value_basis"}
        if source_version == 6 and name == "retirement_plans":
            expected_columns.remove("spending_policy")
        decoded_rows: list[dict[str, Any]] = []
        for index, raw_row in enumerate(raw_rows, start=1):
            if not isinstance(raw_row, dict) or set(raw_row) != expected_columns:
                raise BackupValidationError(
                    f"Record {index} in {name} does not match this app version."
                )
            decoded_rows.append({
                column.name: (
                    {"planning_mode": "budget", "annual_savings_amount": Decimal("0"), "legacy_value_basis": "nominal"}[column.name]
                    if source_version < 9 and name == "retirement_plans" and column.name in {"planning_mode", "annual_savings_amount", "legacy_value_basis"}
                    else "guardrails"
                    if column.name == "spending_policy" and name == "retirement_plans" and source_version == 6
                    else None
                    if column.name == "annual_inflation_decimal"
                    and source_version < 3
                    else _decode(
                        raw_row[column.name],
                        column,
                        f"{name}[{index}].{column.name}",
                    )
                )
                for column in table.columns
            })
        decoded[name] = decoded_rows

    if source_version < 11:
        try:
            converted_at = datetime.now(UTC)
            for name in DECIMAL_FIELDS:
                normalized = []
                for row in decoded[name]:
                    converted, changes = normalize_legacy(name, row)
                    normalized.append(converted)
                    for change in changes:
                        decoded['decimal_conversions'].append(dict(change, id=len(decoded['decimal_conversions']) + 1, created_at=converted_at))
                decoded[name] = normalized
            validate_normalized_history(decoded)
        except (ValueError, TypeError) as error:
            raise BackupValidationError(f'This backup cannot be safely rounded: {error}') from error

    from app.services.retirement_plans import validate_plan, validate_income, PlanValidationError
    try:
        for row in decoded["retirement_plans"]:
            validated = validate_plan(row)
            if any(row[key] != value for key, value in validated.items()):
                raise BackupValidationError("Retirement plan values must use canonical source formats.")
        for row in decoded["retirement_income"]:
            validated = validate_income(row)
            if any(row[key] != value for key, value in validated.items()):
                raise BackupValidationError("Retirement income values must use canonical source formats.")
    except PlanValidationError as error:
        raise BackupValidationError(f"Invalid retirement plan: {error}") from error
    from app.services.retirement_scenarios import validate_scenario_record
    try:
        for row in decoded["retirement_scenarios"]:
            validate_scenario_record(row)
    except ValueError as error:
        raise BackupValidationError(str(error)) from error
    from app.services.fx_reference import validate_rates, FxReferenceError
    try:
        for row in decoded["fx_reference_sets"]:
            rates = json.loads(row["rates_json"])
            if validate_rates(rates) != rates or not row["source_note"].strip() or len(row["source_note"]) > 2000:
                raise ValueError("Invalid reference rate metadata.")
    except (ValueError, TypeError, AttributeError, FxReferenceError) as error:
        raise BackupValidationError(f"Invalid reference FX set: {error}") from error
    # Row constraints cannot enforce the total of a dated allocation set.
    role_sets: dict[tuple[int, date], dict[str, Decimal]] = {}
    for row in decoded["instrument_classifications"]:
        key = (row["instrument_id"], row["effective_date"])
        role_sets.setdefault(key, {})[row["economic_role_code"]] = row["weight_decimal"]
    for (instrument_id, effective_date), weights in role_sets.items():
        try:
            validate_role_weights(weights)
        except ClassificationValidationError as error:
            raise BackupValidationError(
                f"Instrument {instrument_id}, classification dated {effective_date}: {error}"
            ) from error

    _validate_in_disposable_database(decoded)
    counts = {name: len(decoded[name]) for name in TABLE_ORDER}
    canonical_tables = {
        name: [
            {column.name: _encode(row[column.name]) for column in db.metadata.tables[name].columns}
            for row in decoded[name]
        ]
        for name in TABLE_ORDER
    }
    canonical_payload = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "created_at": payload["created_at"],
        "tables": canonical_tables,
    }
    return ValidatedBackup(
        payload=canonical_payload,
        decoded_tables=decoded,
        preview=BackupPreview(
            created_at=created_at,
            version=source_version,
            table_counts=counts,
            total_records=sum(counts.values()),
        ),
    )


def stage_backup(validated: ValidatedBackup, staging_directory: Path) -> str:
    """Store only a validated canonical payload behind an unguessable local token."""

    staging_directory.mkdir(parents=True, exist_ok=True)
    token = token_hex(16)
    path = staging_directory / f"{token}.json"
    content = (json.dumps(validated.payload, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
    return token


def load_staged_backup(token: str, staging_directory: Path) -> ValidatedBackup:
    if not isinstance(token, str) or _TOKEN_PATTERN.fullmatch(token) is None:
        raise BackupValidationError("The restore preview has expired or is invalid.")
    path = staging_directory / f"{token}.json"
    try:
        blob = path.read_bytes()
    except FileNotFoundError as error:
        raise BackupValidationError("The restore preview has expired or is missing.") from error
    return validate_backup(blob)


def discard_staged_backup(token: str, staging_directory: Path) -> None:
    if isinstance(token, str) and _TOKEN_PATTERN.fullmatch(token):
        (staging_directory / f"{token}.json").unlink(missing_ok=True)


def restore_backup(validated: ValidatedBackup) -> None:
    """Replace all source records in one transaction after prior validation."""

    db.session.remove()
    try:
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
            for name in reversed(TABLE_ORDER):
                connection.execute(db.metadata.tables[name].delete())
            for name in TABLE_ORDER:
                rows = validated.decoded_tables[name]
                if rows:
                    connection.execute(db.metadata.tables[name].insert(), rows)
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise BackupValidationError(
                    "The backup contains records with invalid relationships."
                )
    except BackupValidationError:
        raise
    except SQLAlchemyError as error:
        raise BackupValidationError(
            "The backup could not replace the active data; the active data was unchanged."
        ) from error
    finally:
        db.session.remove()


def _validate_in_disposable_database(
    decoded: dict[str, list[dict[str, Any]]]
) -> None:
    engine = create_engine("sqlite:///:memory:")
    try:
        db.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            connection.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
            for name in TABLE_ORDER:
                rows = decoded[name]
                if rows:
                    connection.execute(db.metadata.tables[name].insert(), rows)
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise BackupValidationError(
                    "The backup contains records with invalid relationships."
                )
    except BackupValidationError:
        raise
    except (SQLAlchemyError, ValueError, TypeError) as error:
        raise BackupValidationError(
            "The backup contains invalid or inconsistent records."
        ) from error
    finally:
        engine.dispose()


def _encode(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Unsupported backup value type: {type(value).__name__}")


def _decode(value: Any, column, path: str) -> Any:
    if value is None:
        if not column.nullable or column.primary_key:
            raise BackupValidationError(f"{path} cannot be null.")
        return None
    column_type = column.type
    try:
        if isinstance(column_type, UTCDateTime):
            return _parse_datetime(value, path)
        if isinstance(column_type, DateTime):
            return _parse_datetime(value, path)
        if isinstance(column_type, Date):
            if not isinstance(value, str):
                raise ValueError
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
            return parsed
        if isinstance(column_type, (Numeric, ScaledDecimal)):
            if not isinstance(value, str):
                raise ValueError
            parsed = Decimal(value)
            if not parsed.is_finite():
                raise ValueError
            return parsed
        if isinstance(column_type, Boolean):
            if not isinstance(value, bool):
                raise ValueError
            return value
        if isinstance(column_type, Integer):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError
            return value
        if isinstance(column_type, Text) or hasattr(column_type, "length"):
            if not isinstance(value, str):
                raise ValueError
            return value
    except (ValueError, TypeError, InvalidOperation) as error:
        raise BackupValidationError(f"{path} has an invalid value.") from error
    raise BackupValidationError(f"{path} uses an unsupported column type.")


def _parse_datetime(value: Any, path: str) -> datetime:
    if not isinstance(value, str):
        raise BackupValidationError(f"{path} must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BackupValidationError(f"{path} must be an ISO timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BackupValidationError(f"{path} must include a timezone.")
    return parsed.astimezone(UTC)
