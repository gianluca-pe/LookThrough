"""Request schema for a simple institution relationship minimum."""

from __future__ import annotations

from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    SelectMultipleField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import (
    DataRequired,
    InputRequired,
    Length,
    Optional,
    ValidationError,
)

from app.conventions import normalize_currency_code
from app.form_fields import DecimalField


def _strip(value: str | None) -> str | None:
    return value.strip() if value is not None else None


def _currency(form: FlaskForm, field: StringField) -> None:
    try:
        field.data = normalize_currency_code(field.data, field_name=field.label.text)
    except (TypeError, ValueError) as exc:
        raise ValidationError(str(exc)) from exc


def _positive(form: FlaskForm, field: DecimalField) -> None:
    if field.data is None or not field.data.is_finite() or field.data <= Decimal("0"):
        raise ValidationError("Threshold must be a positive finite amount.")


def _nonnegative(form: FlaskForm, field: DecimalField) -> None:
    if field.data is not None and (
        not field.data.is_finite() or field.data < Decimal("0")
    ):
        raise ValidationError(
            "Preferred extra buffer must be a non-negative finite amount."
        )


class RelationshipRuleForm(FlaskForm):
    name = StringField(
        "Minimum name",
        validators=[DataRequired(), Length(max=160)],
        filters=[_strip],
    )
    threshold_amount = DecimalField(
        "Threshold amount", places=None, validators=[InputRequired(), _positive]
    )
    threshold_currency_code = StringField(
        "Threshold currency",
        validators=[InputRequired(), _currency],
        filters=[_strip],
    )
    warning_buffer_amount = DecimalField(
        "Preferred extra buffer", places=None, validators=[Optional(), _nonnegative]
    )
    eligible_account_ids = SelectMultipleField(
        "Eligible accounts", coerce=int, choices=[], validators=[Optional()]
    )
    is_active = BooleanField("Use this relationship minimum")
    notes = TextAreaField(
        "Notes", validators=[Optional(), Length(max=1000)], filters=[_strip]
    )
    save_rule = SubmitField("Save relationship minimum")
