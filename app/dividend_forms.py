"""WTForms schema for cash and reinvested dividend outcomes."""

from __future__ import annotations

from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import DateField, SelectField, StringField, SubmitField
from wtforms.validators import InputRequired, Optional, ValidationError

from app.conventions import normalize_currency_code
from app.form_fields import DecimalField


DIVIDEND_OUTCOME_CHOICES = [
    ("cash", "Added to cash"),
    ("reinvest_same", "Reinvested in the same instrument"),
    ("reinvest_other", "Reinvested in another instrument"),
]


def _strip(value: str | None) -> str | None:
    return value.strip() if value is not None else None


def _currency(form: FlaskForm, field: StringField) -> None:
    try:
        field.data = normalize_currency_code(
            field.data,
            field_name="Dividend currency",
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(str(exc)) from exc


def _positive(form: FlaskForm, field: DecimalField) -> None:
    if field.data is not None and (
        not field.data.is_finite() or field.data <= 0
    ):
        raise ValidationError(f"{field.label.text} must be greater than zero.")


def _nonnegative(form: FlaskForm, field: DecimalField) -> None:
    if field.data is not None and (
        not field.data.is_finite() or field.data < 0
    ):
        raise ValidationError(f"{field.label.text} must not be negative.")


class DividendForm(FlaskForm):
    effective_date = DateField(
        "Payment date", validators=[InputRequired()], format="%Y-%m-%d"
    )
    account_id = SelectField(
        "Account", coerce=int, choices=[], validators=[InputRequired()]
    )
    instrument_id = SelectField(
        "Distributing instrument",
        coerce=int,
        choices=[],
        validators=[InputRequired()],
    )
    currency_code = StringField(
        "Dividend currency",
        filters=[_strip],
        validators=[InputRequired(), _currency],
    )
    net_amount = DecimalField(
        "Net amount received",
        places=None,
        validators=[InputRequired(), _positive],
    )
    outcome = SelectField(
        "What happened next?",
        choices=DIVIDEND_OUTCOME_CHOICES,
        validators=[InputRequired()],
        default="cash",
    )
    gross_amount = DecimalField(
        "Gross distribution",
        places=None,
        validators=[Optional(), _positive],
    )
    withholding_amount = DecimalField(
        "Entered withholding",
        places=None,
        validators=[Optional(), _nonnegative],
    )
    reinvestment_instrument_id = SelectField(
        "Instrument bought",
        coerce=int,
        choices=[],
        validators=[Optional()],
    )
    reinvestment_quantity = DecimalField(
        "Additional units",
        places=None,
        validators=[Optional()],
    )
    reinvestment_unit_price = DecimalField(
        "Unit price",
        places=None,
        validators=[Optional()],
    )
    reinvestment_purchase_amount = DecimalField(
        "Purchase amount",
        places=None,
        validators=[Optional()],
    )
    reinvestment_fee_amount = DecimalField(
        "Reinvestment fee",
        places=None,
        validators=[Optional()],
        default=Decimal("0"),
    )
    preview_dividend = SubmitField("Preview")
    post_dividend = SubmitField("Record dividend")

    def validate(self, extra_validators=None) -> bool:
        valid = super().validate(extra_validators)
        if (
            self.outcome.data == "reinvest_other"
            and not self.reinvestment_instrument_id.data
        ):
            self.reinvestment_instrument_id.errors.append(
                "Choose the instrument bought with the dividend."
            )
            valid = False
        if self.outcome.data in {"reinvest_same", "reinvest_other"}:
            if self.reinvestment_quantity.data is None:
                self.reinvestment_quantity.errors.append(
                    "Additional units are required for reinvestment."
                )
                valid = False
            if (
                self.reinvestment_unit_price.data is None
                and self.reinvestment_purchase_amount.data is None
            ):
                self.reinvestment_unit_price.errors.append(
                    "Enter the actual unit price or total purchase amount."
                )
                valid = False
            for field in (
                self.reinvestment_quantity,
                self.reinvestment_unit_price,
                self.reinvestment_purchase_amount,
            ):
                if field.data is not None and (
                    not field.data.is_finite() or field.data <= 0
                ):
                    field.errors.append(
                        f"{field.label.text} must be greater than zero."
                    )
                    valid = False
            fee = self.reinvestment_fee_amount.data
            if fee is not None and (not fee.is_finite() or fee < 0):
                self.reinvestment_fee_amount.errors.append(
                    "Reinvestment fee must not be negative."
                )
                valid = False
        return valid
