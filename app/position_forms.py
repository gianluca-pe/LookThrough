"""Request schemas for opening positions and manual source values."""

from __future__ import annotations

from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import BooleanField, DateField, SelectField, StringField, SubmitField
from wtforms.validators import InputRequired, Length, Optional, ValidationError

from app.conventions import normalize_currency_code
from app.form_fields import DecimalField


def _strip(value: str | None) -> str | None:
    return value.strip() if value is not None else None


INSTRUMENT_TYPE_CHOICES = [
    ("fund", "Fund"), ("etf", "ETF"), ("stock", "Stock"), ("bond", "Bond"),
    ("government_savings", "Government savings"), ("fixed_deposit", "Fixed deposit"),
    ("money_market", "Money market"), ("cash", "Cash instrument"), ("other", "Other"),
]
TRACKING_MODE_CHOICES = [
    ("transaction_tracked", "Tracked by quantity and transactions"),
    ("statement_valued", "Tracked by statement value"),
]

# Keep the "Add a new instrument" choice distinct from numeric instrument IDs.
NEW_INSTRUMENT_SENTINEL = "__new__"


def _instrument_coerce(value):
    if value == NEW_INSTRUMENT_SENTINEL:
        return value
    return int(value)


def _currency(form: FlaskForm, field: StringField) -> None:
    try:
        field.data = normalize_currency_code(field.data, field_name=field.label.text)
    except (TypeError, ValueError) as exc:
        raise ValidationError(str(exc)) from exc


def _positive(form: FlaskForm, field: DecimalField) -> None:
    if field.data is not None and field.data <= 0:
        raise ValidationError(f"{field.label.text} must be greater than zero.")


def _nonnegative(form: FlaskForm, field: DecimalField) -> None:
    if field.data is not None and field.data < 0:
        raise ValidationError(f"{field.label.text} must not be negative.")


class OpeningPositionForm(FlaskForm):
    account_id = SelectField("Account", coerce=int, choices=[], validators=[InputRequired()])
    instrument_id = SelectField(
        "Instrument", coerce=_instrument_coerce, choices=[], validators=[InputRequired()]
    )
    new_instrument_name = StringField(
        "New instrument name",
        filters=[_strip],
        validators=[Optional(), Length(max=200)],
    )
    ticker_or_isin = StringField("Ticker or ISIN", filters=[_strip], validators=[Optional(), Length(max=64)])
    instrument_type = SelectField("Instrument type", choices=INSTRUMENT_TYPE_CHOICES)
    valuation_currency_code = StringField("Valuation currency", filters=[_strip])
    tracking_mode = SelectField("Tracking mode", choices=TRACKING_MODE_CHOICES, validators=[InputRequired()])
    effective_date = DateField("Opening date", validators=[InputRequired()], format="%Y-%m-%d")
    opening_quantity = DecimalField("Opening quantity", places=None, validators=[Optional(), _positive])
    statement_value = DecimalField("Statement value", places=None, validators=[Optional(), _nonnegative])
    save_position = SubmitField("Add position")

    @property
    def new_instrument_selected(self) -> bool:
        return self.instrument_id.data == NEW_INSTRUMENT_SENTINEL

    def validate(self, extra_validators=None) -> bool:
        valid = super().validate(extra_validators)
        if self.new_instrument_selected:
            if not self.new_instrument_name.data:
                self.new_instrument_name.errors.append("New instrument name is required.")
                valid = False
            try:
                self.valuation_currency_code.data = normalize_currency_code(
                    self.valuation_currency_code.data, field_name="Valuation currency"
                )
            except (TypeError, ValueError) as exc:
                self.valuation_currency_code.errors.append(str(exc))
                valid = False
        if self.tracking_mode.data == "transaction_tracked" and self.opening_quantity.data is None:
            self.opening_quantity.errors.append("Opening quantity is required for quantity tracking.")
            valid = False
        if self.tracking_mode.data == "statement_valued" and self.statement_value.data is None:
            self.statement_value.errors.append("Statement value is required for statement tracking.")
            valid = False
        return valid


class PriceForm(FlaskForm):
    instrument_id = SelectField("Instrument", coerce=int, choices=[], validators=[InputRequired()])
    effective_date = DateField("Price date", validators=[InputRequired()], format="%Y-%m-%d")
    price_amount = DecimalField("Price", places=None, validators=[InputRequired(), _positive])
    currency_code = StringField("Price currency", filters=[_strip], validators=[InputRequired(), _currency])
    confirm_same_day_correction = BooleanField(
        "Replace the existing price for this instrument and date"
    )
    save_price = SubmitField("Save price")


class StatementValueForm(FlaskForm):
    registration_id = SelectField("Position", coerce=int, choices=[], validators=[InputRequired()])
    effective_date = DateField("Statement date", validators=[InputRequired()], format="%Y-%m-%d")
    native_value_amount = DecimalField(
        "Statement value", places=None, validators=[InputRequired(), _nonnegative]
    )
    currency_code = StringField("Currency", filters=[_strip], validators=[InputRequired(), _currency])
    save_statement_value = SubmitField("Save statement value")


class FxRateForm(FlaskForm):
    base_currency_code = StringField(
        "Base currency", filters=[_strip], validators=[InputRequired(), _currency]
    )
    quote_currency_code = StringField(
        "Quote currency", filters=[_strip], validators=[InputRequired(), _currency]
    )
    effective_date = DateField("FX date", validators=[InputRequired()], format="%Y-%m-%d")
    quote_per_base_amount = DecimalField(
        "Quote per base", places=None, validators=[InputRequired(), _positive]
    )
    acknowledge_large_change = BooleanField(
        "I checked this rate; save it anyway"
    )
    confirm_same_day_correction = BooleanField(
        "Replace the existing source for this pair and date"
    )
    save_fx = SubmitField("Save FX rate")

    def validate(self, extra_validators=None) -> bool:
        valid = super().validate(extra_validators)
        if valid and self.base_currency_code.data == self.quote_currency_code.data:
            self.quote_currency_code.errors.append("Base and quote currencies must differ.")
            return False
        return valid


class RoutineUpdateSectionForm(FlaskForm):
    """CSRF boundary for one independently atomic routine-update section."""

    save_updates = SubmitField("Save entered updates")
