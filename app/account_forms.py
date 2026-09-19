"""Request schema for bounded account maintenance."""

from __future__ import annotations

from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import DateField, SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional, ValidationError

from app.conventions import normalize_currency_code
from app.form_fields import DecimalField
from app.setup_forms import CASH_TRACKING_MODE_CHOICES


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


def _percentage(form: FlaskForm, field: DecimalField) -> None:
    if field.data is None:
        raise ValidationError(f"{field.label.text} is required.")
    if not field.data.is_finite():
        raise ValidationError(f"{field.label.text} must be a finite decimal number.")
    if field.data < Decimal("0") or field.data > Decimal("100"):
        raise ValidationError(f"{field.label.text} must be between 0 and 100.")


class AccountMaintenanceForm(FlaskForm):
    name = StringField(
        "Account name",
        validators=[DataRequired(), Length(max=160)],
        filters=[_strip],
    )
    reference = StringField(
        "Reference or account number",
        validators=[Optional(), Length(max=200)],
        filters=[_strip],
    )
    cash_tracking_mode = SelectField(
        "Cash tracking",
        choices=CASH_TRACKING_MODE_CHOICES,
        validators=[DataRequired()],
    )
    cash_settlement_account_id = SelectField(
        "Where activity cash settles",
        coerce=int,
        choices=[(0, "Cash stays in this account")],
        validators=[Optional()],
    )
    portfolio_share_percent = DecimalField(
        "Portfolio share (%)",
        places=None,
        validators=[_percentage],
    )
    present_access_percent = DecimalField(
        "Present access to your included share (%)",
        places=None,
        validators=[_percentage],
    )
    earliest_access_date = DateField(
        "Earliest access date", validators=[Optional()], format="%Y-%m-%d"
    )
    access_note = TextAreaField(
        "Access note",
        validators=[Optional(), Length(max=1000)],
        filters=[_strip],
    )
    currency_code = StringField(
        "Fresh cash currency",
        validators=[Optional(), _currency],
        filters=[_strip],
    )
    confirmed_balance_amount = DecimalField(
        "Fresh confirmed balance",
        places=None,
        validators=[Optional(), _finite],
    )
    effective_date = DateField(
        "Confirmation date", validators=[Optional()], format="%Y-%m-%d"
    )
    source_note = TextAreaField(
        "Confirmation source note",
        validators=[Optional(), Length(max=1000)],
        filters=[_strip],
    )
    save_account = SubmitField("Save account")


class EmptyAccountArchiveForm(FlaskForm):
    archive_account = SubmitField("Archive empty account")
