"""Shared parsing and timestamp conventions for financial source values."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation


_CURRENCY_CODE_PATTERN = re.compile(r"[A-Z]{3}")


def parse_decimal(value: str | int | Decimal, *, field_name: str = "value") -> Decimal:
    """Parse an exact finite Decimal and reject binary floating-point input."""

    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field_name} must not be a binary floating-point value")

    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            raise ValueError(f"{field_name} is required")
        try:
            parsed = Decimal(candidate)
        except InvalidOperation as exc:
            raise ValueError(f"{field_name} must be a decimal number") from exc
    else:
        raise TypeError(f"{field_name} must be provided as text, an integer, or Decimal")

    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")

    return parsed


def parse_iso_date(value: str | date, *, field_name: str = "date") -> date:
    """Parse a financial effective date in canonical ISO ``YYYY-MM-DD`` form."""

    if isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a date without a time")

    if isinstance(value, date):
        return value

    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be provided as ISO date text or a date")

    candidate = value.strip()
    if not candidate:
        raise ValueError(f"{field_name} is required")

    try:
        parsed = date.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{field_name} must use YYYY-MM-DD") from exc

    if parsed.isoformat() != candidate:
        raise ValueError(f"{field_name} must use YYYY-MM-DD")

    return parsed


def normalize_currency_code(value: str, *, field_name: str = "currency") -> str:
    """Normalize and validate a three-letter ISO-style currency code."""

    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text")

    candidate = value.strip().upper()
    if _CURRENCY_CODE_PATTERN.fullmatch(candidate) is None:
        raise ValueError(f"{field_name} must be a three-letter currency code")

    return candidate


def utc_now() -> datetime:
    """Return an aware UTC audit timestamp."""

    return datetime.now(UTC)
