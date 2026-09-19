"""Exact-number, financial-date, currency, and audit-time conventions."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.conventions import (
    normalize_currency_code,
    parse_decimal,
    parse_iso_date,
    utc_now,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10275.00", Decimal("10275.00")),
        (" 0.000001 ", Decimal("0.000001")),
        (25, Decimal("25")),
        (Decimal("11.10"), Decimal("11.10")),
    ],
)
def test_parse_decimal_preserves_exact_values(
    raw: str | int | Decimal, expected: Decimal
) -> None:
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", [10.25, float("nan"), float("inf"), True])
def test_parse_decimal_rejects_binary_floats_and_booleans(raw: object) -> None:
    with pytest.raises(TypeError):
        parse_decimal(raw)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", ["", "not-a-number", "NaN", "Infinity"])
def test_parse_decimal_rejects_missing_invalid_or_non_finite_text(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_decimal(raw)


def test_parse_iso_date_accepts_canonical_financial_date() -> None:
    assert parse_iso_date("2026-08-02") == date(2026, 8, 2)


@pytest.mark.parametrize("raw", ["02/08/2026", "2026-8-2", ""])
def test_parse_iso_date_rejects_non_iso_text(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_iso_date(raw)


def test_parse_iso_date_rejects_timestamp() -> None:
    with pytest.raises(TypeError):
        parse_iso_date(datetime(2026, 8, 2, 12, 0))


@pytest.mark.parametrize(
    ("raw", "expected"), [("eur", "EUR"), (" SGD ", "SGD"), ("USD", "USD")]
)
def test_normalize_currency_code(raw: str, expected: str) -> None:
    assert normalize_currency_code(raw) == expected


@pytest.mark.parametrize("raw", ["", "EU", "EURO", "12A"])
def test_normalize_currency_code_rejects_invalid_codes(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_currency_code(raw)


def test_utc_now_returns_aware_utc_timestamp() -> None:
    timestamp = utc_now()

    assert timestamp.tzinfo is UTC
    assert timestamp.utcoffset() is not None
