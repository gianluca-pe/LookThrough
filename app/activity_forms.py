"""WTForms schema for the compact same-currency Buy/Sell flow."""

from __future__ import annotations

from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import (
    DateField,
    HiddenField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import AnyOf, InputRequired, Length, Optional, ValidationError

from app.conventions import normalize_currency_code
from app.form_fields import DecimalField
from app.position_forms import INSTRUMENT_TYPE_CHOICES


QUANTITY_MODE_CHOICES = [
    ("entered", "Enter quantity"),
    ("entire_holding", "Sell the entire holding"),
]


def _strip(value: str | None) -> str | None:
    return value.strip() if value is not None else None


def _positive(form: FlaskForm, field: DecimalField) -> None:
    if field.data is not None and field.data <= 0:
        raise ValidationError(f"{field.label.text} must be greater than zero.")


def _nonnegative(form: FlaskForm, field: DecimalField) -> None:
    if field.data is not None and field.data < 0:
        raise ValidationError(f"{field.label.text} must not be negative.")


class TradeForm(FlaskForm):
    activity_type = HiddenField(
        "Activity type", validators=[InputRequired(), AnyOf(["buy", "sell"])]
    )
    replacement_sale_id = HiddenField("Replacement sale")
    effective_date = DateField(
        "Date", validators=[InputRequired()], format="%Y-%m-%d"
    )
    account_id = SelectField(
        "Account", coerce=int, choices=[], validators=[InputRequired()]
    )
    instrument_id = SelectField(
        "Instrument", coerce=int, choices=[], validators=[InputRequired()]
    )
    quantity_mode = SelectField(
        "Quantity choice",
        choices=QUANTITY_MODE_CHOICES,
        validators=[AnyOf(["entered", "entire_holding"])],
        default="entered",
    )
    quantity = DecimalField(
        "Quantity", places=None, validators=[Optional(), _positive]
    )
    unit_price = DecimalField(
        "Price per unit", places=None, validators=[InputRequired(), _positive]
    )
    fee_amount = DecimalField(
        "Fee", places=None, validators=[Optional(), _nonnegative], default=Decimal("0")
    )
    new_instrument_name = StringField("Name", filters=[_strip])
    new_ticker_or_isin = StringField("Ticker or ISIN", filters=[_strip])
    new_valuation_currency_code = StringField(
        "Valuation currency", filters=[_strip]
    )
    new_instrument_type = SelectField(
        "Broad type",
        choices=INSTRUMENT_TYPE_CHOICES,
        default="fund",
        validate_choice=False,
    )
    preview_trade = SubmitField("Preview")
    post_trade = SubmitField("Record activity")
    create_instrument = SubmitField("Save and use instrument")

    def validate(self, extra_validators=None) -> bool:
        if self.create_instrument.data:
            return self._validate_new_instrument()
        valid = super().validate(extra_validators)
        if self.quantity_mode.data == "entered" and self.quantity.data is None:
            self.quantity.errors.append("Quantity is required.")
            valid = False
        if (
            self.quantity_mode.data == "entire_holding"
            and self.activity_type.data != "sell"
        ):
            self.quantity_mode.errors.append(
                "The entire-holding choice is available only for a Sell."
            )
            valid = False
        return valid

    def _validate_new_instrument(self) -> bool:
        """Validate only the compact identity fields for the inline save action."""

        valid = True
        for field in (
            self.new_instrument_name,
            self.new_ticker_or_isin,
            self.new_valuation_currency_code,
            self.new_instrument_type,
        ):
            field.errors = []

        if not self.new_instrument_name.data:
            self.new_instrument_name.errors.append("Name is required.")
            valid = False
        elif len(self.new_instrument_name.data) > 200:
            self.new_instrument_name.errors.append(
                "Name must be 200 characters or fewer."
            )
            valid = False

        if (
            self.new_ticker_or_isin.data
            and len(self.new_ticker_or_isin.data) > 64
        ):
            self.new_ticker_or_isin.errors.append(
                "Ticker or ISIN must be 64 characters or fewer."
            )
            valid = False

        try:
            self.new_valuation_currency_code.data = normalize_currency_code(
                self.new_valuation_currency_code.data,
                field_name="Valuation currency",
            )
        except (TypeError, ValueError) as exc:
            self.new_valuation_currency_code.errors.append(str(exc))
            valid = False

        supported_types = {value for value, _ in INSTRUMENT_TYPE_CHOICES}
        if self.new_instrument_type.data not in supported_types:
            self.new_instrument_type.errors.append("Choose a supported broad type.")
            valid = False

        if self.activity_type.data != "buy":
            self.activity_type.errors.append(
                "A new instrument can be created while recording a Buy."
            )
            valid = False
        return valid


class ReversalForm(FlaskForm):
    reason = TextAreaField(
        "Reason",
        filters=[_strip],
        validators=[InputRequired(), Length(max=1000)],
    )
    confirm_reversal = SubmitField("Reverse activity")
