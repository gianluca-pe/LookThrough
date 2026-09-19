"""Request schema for simple fixed-deposit terms."""

from __future__ import annotations

from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import DateField, SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, InputRequired, Length, Optional, ValidationError

from app.conventions import normalize_currency_code
from app.form_fields import DecimalField


MATURITY_ACTION_CHOICES = [
    ("undecided", "Undecided"),
    ("return_to_cash", "Return to cash"),
    ("rollover", "Rollover"),
    ("switch", "Switch to another investment"),
    ("manual", "Handle manually"),
]


def _strip(value: str | None) -> str | None:
    return value.strip() if value is not None else None


def _currency(form: FlaskForm, field: StringField) -> None:
    try:
        field.data = normalize_currency_code(field.data, field_name=field.label.text)
    except (TypeError, ValueError) as exc:
        raise ValidationError(str(exc)) from exc


def _nonnegative_rate(form: FlaskForm, field: DecimalField) -> None:
    if field.data is None:
        return
    if not field.data.is_finite():
        raise ValidationError("Annual rate must be a finite decimal number.")
    if field.data < Decimal("0"):
        raise ValidationError("Annual rate cannot be negative.")


def _positive_optional_amount(form: FlaskForm, field: DecimalField) -> None:
    if field.data is None:
        return
    if not field.data.is_finite() or field.data <= Decimal("0"):
        raise ValidationError("Amount must be a positive decimal number.")


def _positive_amount(form: FlaskForm, field: DecimalField) -> None:
    if field.data is None or not field.data.is_finite() or field.data <= Decimal("0"):
        raise ValidationError("Amount must be a positive decimal number.")


def _nonnegative_amount(form: FlaskForm, field: DecimalField) -> None:
    if field.data is None or not field.data.is_finite() or field.data < Decimal("0"):
        raise ValidationError("Amount must be zero or a positive decimal number.")


class FixedDepositTermsForm(FlaskForm):
    currency_code = StringField(
        "Currency", validators=[InputRequired(), _currency], filters=[_strip]
    )
    start_date = DateField(
        "Start date", validators=[DataRequired()], format="%Y-%m-%d"
    )
    maturity_date = DateField(
        "Maturity date", validators=[DataRequired()], format="%Y-%m-%d"
    )
    annual_rate_percent = DecimalField(
        "Annual rate (%)", places=None, validators=[Optional(), _nonnegative_rate]
    )
    expected_maturity_proceeds_amount = DecimalField(
        "Bank-quoted expected maturity proceeds",
        places=None,
        validators=[Optional(), _positive_optional_amount],
    )
    maturity_action = SelectField(
        "At maturity", choices=MATURITY_ACTION_CHOICES, validators=[DataRequired()]
    )
    notes = TextAreaField(
        "Notes", validators=[Optional(), Length(max=1000)], filters=[_strip]
    )
    save_terms = SubmitField("Save fixed deposit terms")


class MaturityDispositionForm(FlaskForm):
    disposition_type = SelectField(
        "Maturity outcome",
        choices=[
            ("return_to_cash", "Return principal and interest to cash"),
            ("rollover", "Rollover principal; take interest in cash"),
            ("manual", "Other / manual disposition"),
        ],
        validators=[DataRequired()],
    )
    effective_date = DateField(
        "Bank settlement date", validators=[DataRequired()], format="%Y-%m-%d"
    )
    confirmed_principal_amount = DecimalField(
        "Confirmed principal", places=None, validators=[InputRequired(), _positive_amount]
    )
    confirmed_interest_amount = DecimalField(
        "Confirmed interest",
        places=None,
        validators=[InputRequired(), _nonnegative_amount],
    )
    cash_account_id = SelectField(
        "Cash account", coerce=int, validators=[Optional()]
    )
    note = TextAreaField(
        "Note", validators=[Optional(), Length(max=1000)], filters=[_strip]
    )
    successor_instrument_name = StringField(
        "Successor deposit name",
        validators=[Optional(), Length(max=200)],
        filters=[_strip],
    )
    successor_reference = StringField(
        "Successor reference",
        validators=[Optional(), Length(max=64)],
        filters=[_strip],
    )
    successor_maturity_date = DateField(
        "Successor maturity date", validators=[Optional()], format="%Y-%m-%d"
    )
    successor_annual_rate_percent = DecimalField(
        "Successor annual rate (%)",
        places=None,
        validators=[Optional(), _nonnegative_rate],
    )
    successor_expected_proceeds_amount = DecimalField(
        "Successor bank-quoted expected proceeds",
        places=None,
        validators=[Optional(), _positive_optional_amount],
    )
    successor_maturity_action = SelectField(
        "Successor action at maturity",
        choices=MATURITY_ACTION_CHOICES,
        validators=[DataRequired()],
    )
    preview_disposition = SubmitField("Preview maturity disposition")
    confirm_disposition = SubmitField("Record maturity disposition")
