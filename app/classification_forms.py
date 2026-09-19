"""WTForms contract for the instrument inputs used by current planning views."""

from __future__ import annotations

from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateField,
    SelectField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, InputRequired, Length, Optional, ValidationError

from app.form_fields import DecimalField
from app.models import ECONOMIC_ROLE_CODES
from app.services.classification import CLASSIFICATION_TOTAL_TOLERANCE


ECONOMIC_ROLE_LABELS = {
    "equity": "Equity",
    "income": "Income",
    "liquidity": "Liquidity",
    "alternatives": "Alternatives",
}
FIRE_BUCKET_CHOICES = [
    ("", "Not set"),
    ("now", "Now (0–3 years)"),
    ("bridge", "Bridge (3–10 years)"),
    ("growth", "Growth (10+ years)"),
    ("projects", "Projects"),
]


def _strip(value: str | None) -> str | None:
    return value.strip() if value is not None else None


def _finite_percent(form: FlaskForm, field: DecimalField) -> None:
    if field.data is None:
        return
    if not field.data.is_finite():
        raise ValidationError(f"{field.label.text} must be a finite decimal number.")
    if field.data < Decimal("0") or field.data > Decimal("100"):
        raise ValidationError(f"{field.label.text} must be between 0 and 100.")


class InstrumentClassificationForm(FlaskForm):
    effective_date = DateField(
        "Classification date",
        validators=[InputRequired()],
        format="%Y-%m-%d",
    )
    classification_mode = SelectField(
        "Asset allocation detail",
        choices=[
            ("simple", "One primary role"),
            ("advanced", "Split across roles"),
        ],
        validators=[DataRequired()],
        default="simple",
    )
    primary_role_code = SelectField(
        "Primary allocation role",
        choices=[("", "Choose a role")] + [
            (code, ECONOMIC_ROLE_LABELS[code]) for code in ECONOMIC_ROLE_CODES
        ],
        validators=[Optional()],
    )
    role_equity_percent = DecimalField(
        "Equity (%)", places=None, validators=[Optional(), _finite_percent]
    )
    role_income_percent = DecimalField(
        "Income (%)", places=None, validators=[Optional(), _finite_percent]
    )
    role_liquidity_percent = DecimalField(
        "Liquidity (%)", places=None, validators=[Optional(), _finite_percent]
    )
    role_alternatives_percent = DecimalField(
        "Alternatives (%)", places=None, validators=[Optional(), _finite_percent]
    )
    fire_bucket_code = SelectField(
        "Primary FIRE bucket", choices=FIRE_BUCKET_CHOICES, validators=[Optional()]
    )
    source_note = TextAreaField(
        "Source or reasoning",
        validators=[Optional(), Length(max=1000)],
        filters=[_strip],
    )
    replace_existing = BooleanField(
        "Replace the classification already saved for this date"
    )
    save_classification = SubmitField("Save classification")

    def validate(self, extra_validators=None) -> bool:
        valid = super().validate(extra_validators=extra_validators)
        if self.classification_mode.data == "simple":
            if not self.primary_role_code.data:
                self.primary_role_code.errors.append("Choose one primary allocation role.")
                valid = False
        elif self.classification_mode.data == "advanced":
            total = sum(
                (
                    getattr(self, f"role_{role_code}_percent").data
                    or Decimal("0")
                    for role_code in ECONOMIC_ROLE_CODES
                ),
                Decimal("0"),
            )
            percent_tolerance = CLASSIFICATION_TOTAL_TOLERANCE * Decimal("100")
            if abs(total - Decimal("100")) > percent_tolerance:
                self.classification_mode.errors.append(
                    f"Allocation role weights must total 100% (current total: {total}%)."
                )
                valid = False
        return valid

    def role_weights(self) -> dict[str, Decimal]:
        if self.classification_mode.data == "simple":
            return {self.primary_role_code.data: Decimal("1")}
        return {
            role_code: percent / Decimal("100")
            for role_code in ECONOMIC_ROLE_CODES
            if (
                percent := getattr(self, f"role_{role_code}_percent").data
            ) is not None
            and percent > 0
        }
