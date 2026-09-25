"""Exact-number parsing guards that browser input cannot exercise."""

import pytest

from app.conventions import parse_decimal

@pytest.mark.parametrize("raw", [10.25, float("nan"), float("inf"), True])
def test_parse_decimal_rejects_binary_floats_and_booleans(raw: object) -> None:
    with pytest.raises(TypeError):
        parse_decimal(raw)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", ["", "not-a-number", "NaN", "Infinity"])
def test_parse_decimal_rejects_missing_invalid_or_non_finite_text(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_decimal(raw)
