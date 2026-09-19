"""WTForms schema for a dated cash confirmation."""

from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import (
    DateField,
    HiddenField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import InputRequired, Length, Optional, ValidationError

from app.conventions import normalize_currency_code
from app.form_fields import DecimalField


def _strip(value: str | None) -> str | None:
    return value.strip() if value is not None else None


def _currency(form: FlaskForm, field: StringField) -> None:
    try:
        field.data = normalize_currency_code(field.data, field_name=field.label.text)
    except (TypeError, ValueError) as exc:
        raise ValidationError(str(exc)) from exc


def _finite(form: FlaskForm, field: DecimalField) -> None:
    if field.data is not None and not field.data.is_finite():
        raise ValidationError(f"{field.label.text} must be a finite decimal number.")


class CashConfirmationForm(FlaskForm):
    return_to = HiddenField(validators=[Optional()])
    currency_code = StringField(
        "Currency",
        filters=[_strip],
        validators=[InputRequired(), _currency],
    )
    effective_date = DateField(
        "As of", validators=[InputRequired()], format="%Y-%m-%d"
    )
    confirmed_balance_amount = DecimalField(
        "Statement balance", places=None, validators=[InputRequired(), _finite]
    )
    source_note = TextAreaField(
        "Source note",
        filters=[_strip],
        validators=[Optional(), Length(max=1000)],
    )
    save_confirmation = SubmitField("Save cash confirmation")
