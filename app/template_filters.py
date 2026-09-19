"""Presentation-layer template filters: display formatting only.

The frontend may format values but never recomputes financial totals. Inputs come
from server view models as exact numeric values, Decimal-safe strings or dates.
"""

from __future__ import annotations

from datetime import date
from app.decimal_policy import currency_places
from decimal import Decimal, InvalidOperation, localcontext


def money_amount(value: str | int | Decimal | None, places: int | None = 2, currency: str | None = None) -> str:
    """Group thousands for display.

    An explicit currency selects its minor-unit precision; otherwise money
    defaults to two display decimals. This is presentation rounding only; the
    stored Decimal is untouched. Pass ``places=None`` to preserve the given
    precision, e.g. for quantities. ``None`` renders as an em-dash; missing
    values should normally use the ``money_missing`` macro with a reason.
    """

    if currency is not None:
        places = currency_places(currency)
    if value is None:
        return "—"

    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        return str(value)

    if places is not None:
        with localcontext() as context:
            context.prec = max(context.prec, parsed.adjusted() + places + 2)
            parsed = parsed.quantize(Decimal(1).scaleb(-places))

    sign = "-" if parsed < 0 else ""
    return sign + format(parsed.copy_abs(), ",f")


def display_date(value: date | str) -> str:
    """Render an ISO financial date as e.g. ``30 Jun 2026`` (no leading zero)."""

    if isinstance(value, str):
        value = date.fromisoformat(value)
    return f"{value.day} {value.strftime('%b %Y')}"


def trim_decimal(value: str | int | Decimal) -> str:
    """Drop insignificant trailing zeros for display (``100.00000000`` → ``100``)."""

    parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    return format(parsed.normalize(), "f")


def signed_money_amount(value: str | int | Decimal | None, places: int | None = 2, currency: str | None = None) -> str:
    """``money_amount`` with an explicit leading ``+`` for positive values.

    For change/delta figures where direction must be legible without colour.
    """

    if value is None:
        return "—"
    rendered = money_amount(value, places, currency)
    try:
        positive = Decimal(str(value)) > 0
    except InvalidOperation:
        positive = False
    return f"+{rendered}" if positive else rendered


def percentage_amount(
    value: str | int | Decimal | None, places: int | None = 1
) -> str:
    """Render a Decimal fraction as a percentage without a binary-float step.

    ``places`` is display rounding, as in ``money_amount``; pass
    ``places=None`` to preserve the given precision with insignificant
    trailing zeros trimmed (``0.6`` → ``60``), the idiom for classification
    role weights. ``None`` renders as an em-dash.
    """

    if value is None:
        return "—"
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        return str(value)
    scaled = parsed * Decimal("100")
    if places is None:
        return trim_decimal(scaled)
    return money_amount(scaled, places)


def register_filters(blueprint) -> None:
    """Attach the filters to every template via a blueprint."""

    blueprint.add_app_template_filter(currency_places, "currency_places")
    blueprint.add_app_template_filter(money_amount, "money_amount")
    blueprint.add_app_template_filter(signed_money_amount, "signed_money_amount")
    blueprint.add_app_template_filter(display_date, "display_date")
    blueprint.add_app_template_filter(trim_decimal, "trim_decimal")
    blueprint.add_app_template_filter(percentage_amount, "percentage_amount")
