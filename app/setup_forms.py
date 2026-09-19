"""WTForms request schemas for the first two setup steps."""

from __future__ import annotations

from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import (
    DataRequired,
    InputRequired,
    Length,
    NumberRange,
    Optional,
    ValidationError,
)

from app.conventions import normalize_currency_code
from app.form_fields import DecimalField


ACCOUNT_TYPE_CHOICES = [
    ("cash", "Cash account"),
    ("brokerage", "Brokerage account"),
    ("retirement", "Retirement account"),
    ("deposit", "Deposit account"),
    ("other", "Other"),
]

CASH_TRACKING_MODE_CHOICES = [
    ("separate_cash", "Track cash separately"),
    ("included_in_aggregate", "Cash is included in an aggregate statement value"),
]


def _strip(value: str | None) -> str | None:
    return value.strip() if value is not None else None


def _currency_code(form: FlaskForm, field: StringField) -> None:
    try:
        field.data = normalize_currency_code(field.data, field_name=field.label.text)
    except (TypeError, ValueError) as exc:
        raise ValidationError(str(exc)) from exc


def _positive_decimal(form: FlaskForm, field: DecimalField) -> None:
    if field.data is not None and field.data <= 0:
        raise ValidationError(f"{field.label.text} must be greater than zero.")


class PortfolioSetupForm(FlaskForm):
    name = StringField(
        "Portfolio name",
        validators=[DataRequired(), Length(max=120)],
        filters=[_strip],
    )
    reporting_currency_code = StringField(
        "Reporting currency",
        validators=[InputRequired(), _currency_code],
        filters=[_strip],
    )
    annual_spending_amount = DecimalField(
        "Annual spending",
        places=None,
        validators=[InputRequired(), _positive_decimal],
    )
    annual_spending_currency_code = StringField(
        "Annual spending currency",
        validators=[DataRequired(), _currency_code],
        filters=[_strip],
    )
    default_as_of_date = DateField(
        "Default as-of date", validators=[Optional()], format="%Y-%m-%d"
    )
    save_and_continue = SubmitField("Save and continue")
    save_and_finish_later = SubmitField("Save and finish later")


class InstitutionSetupForm(FlaskForm):
    name = StringField(
        "Institution name",
        validators=[DataRequired(), Length(max=160)],
        filters=[_strip],
    )
    add_institution = SubmitField("Add institution")


class AccountSetupForm(FlaskForm):
    institution_id = SelectField(
        "Institution", coerce=int, validators=[InputRequired()], choices=[]
    )
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
    account_type = SelectField(
        "Account type", choices=ACCOUNT_TYPE_CHOICES, validators=[InputRequired()]
    )
    default_currency_code = StringField(
        "Default currency",
        validators=[InputRequired(), _currency_code],
        filters=[_strip],
    )
    is_multicurrency = BooleanField("This account holds more than one currency")
    cash_tracking_mode = SelectField(
        "Cash tracking",
        choices=CASH_TRACKING_MODE_CHOICES,
        validators=[InputRequired()],
        default="separate_cash",
    )
    portfolio_share_percent = DecimalField(
        "Portfolio share (%)",
        places=None,
        validators=[
            InputRequired(),
            NumberRange(
                min=Decimal("0"),
                max=Decimal("100"),
                message="Portfolio share must be between 0 and 100.",
            ),
        ],
        default=Decimal("100"),
    )
    present_access_percent = DecimalField(
        "Present access to your included share (%)",
        places=None,
        validators=[
            InputRequired(),
            NumberRange(
                min=Decimal("0"),
                max=Decimal("100"),
                message="Present access must be between 0 and 100.",
            ),
        ],
        default=Decimal("100"),
    )
    earliest_access_date = DateField(
        "Earliest access date", validators=[Optional()], format="%Y-%m-%d"
    )
    access_note = TextAreaField(
        "Access note", validators=[Optional(), Length(max=1000)], filters=[_strip]
    )
    relationship_eligible = BooleanField(
        "Include this account in institution relationship totals", default=True
    )
    add_account = SubmitField("Add account")
