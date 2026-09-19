"""Forms for owner-entered allocation target ranges."""

from __future__ import annotations

from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import IntegerField, SubmitField
from wtforms.validators import InputRequired, NumberRange, Optional

from app.form_fields import DecimalField


def _target_field(label: str) -> DecimalField:
    return DecimalField(
        label,
        places=None,
        validators=[InputRequired(), NumberRange(min=0, max=100)],
    )


class AllocationTargetsForm(FlaskForm):
    equity_minimum_percent = _target_field("Equity minimum (%)")
    equity_maximum_percent = _target_field("Equity maximum (%)")
    income_minimum_percent = _target_field("Income minimum (%)")
    income_maximum_percent = _target_field("Income maximum (%)")
    liquidity_minimum_percent = _target_field("Liquidity minimum (%)")
    liquidity_maximum_percent = _target_field("Liquidity maximum (%)")
    alternatives_minimum_percent = _target_field("Alternatives minimum (%)")
    alternatives_maximum_percent = _target_field("Alternatives maximum (%)")
    save_targets = SubmitField("Save target ranges")

    def validate(self, extra_validators=None) -> bool:
        valid = super().validate(extra_validators=extra_validators)
        ranges = self.ranges_percent()
        for role_code, (minimum, maximum) in ranges.items():
            if minimum is not None and maximum is not None and minimum > maximum:
                getattr(self, f"{role_code}_maximum_percent").errors.append(
                    "Maximum must be greater than or equal to minimum."
                )
                valid = False
        if all(value is not None for pair in ranges.values() for value in pair):
            minimum_total = sum((pair[0] for pair in ranges.values()), Decimal("0"))
            maximum_total = sum((pair[1] for pair in ranges.values()), Decimal("0"))
            if minimum_total > 100 or maximum_total < 100:
                self.equity_minimum_percent.errors.append(
                    "The four ranges must allow a complete 100% allocation."
                )
                valid = False
        return valid

    def ranges_percent(self) -> dict[str, tuple[Decimal | None, Decimal | None]]:
        return {
            role_code: (
                getattr(self, f"{role_code}_minimum_percent").data,
                getattr(self, f"{role_code}_maximum_percent").data,
            )
            for role_code in ("equity", "income", "liquidity", "alternatives")
        }

    def ranges_decimal(self) -> dict[str, tuple[Decimal, Decimal]]:
        return {
            role_code: (minimum / 100, maximum / 100)
            for role_code, (minimum, maximum) in self.ranges_percent().items()
            if minimum is not None and maximum is not None
        }


class FundingAssumptionsForm(FlaskForm):
    annual_inflation_percent = DecimalField(
        "Annual inflation assumption (%)",
        places=None,
        validators=[InputRequired(), NumberRange(min=0, max=100)],
    )
    save_assumptions = SubmitField("Save funding assumptions")


def _return_field(label: str) -> DecimalField:
    return DecimalField(
        label,
        places=None,
        validators=[InputRequired(), NumberRange(min=-100, max=100)],
    )


class RetirementProjectionForm(FlaskForm):
    current_age_years = IntegerField(
        "Current age", validators=[InputRequired(), NumberRange(min=0, max=120)]
    )
    withdrawal_start_age_years = IntegerField(
        "Withdrawal-start age",
        validators=[InputRequired(), NumberRange(min=0, max=120)],
    )
    final_age_years = IntegerField(
        "Final age", validators=[InputRequired(), NumberRange(min=0, max=130)]
    )
    equity_return_percent = _return_field("Equity nominal return (%)")
    income_return_percent = _return_field("Income nominal return (%)")
    liquidity_return_percent = _return_field("Liquidity nominal return (%)")
    alternatives_return_percent = _return_field(
        "Alternatives nominal return (%)"
    )
    terminal_legacy_target_amount = DecimalField(
        "Terminal legacy target",
        places=None,
        validators=[Optional(), NumberRange(min=0)],
    )
    save_projection = SubmitField("Save assumptions and project")

    def validate(self, extra_validators=None) -> bool:
        valid = super().validate(extra_validators=extra_validators)
        current = self.current_age_years.data
        withdrawal = self.withdrawal_start_age_years.data
        final = self.final_age_years.data
        if current is not None and withdrawal is not None and withdrawal <= current:
            self.withdrawal_start_age_years.errors.append(
                "Withdrawal-start age must be greater than current age."
            )
            valid = False
        if withdrawal is not None and final is not None and final <= withdrawal:
            self.final_age_years.errors.append(
                "Final age must be greater than withdrawal-start age."
            )
            valid = False
        return valid

    def role_returns_decimal(self) -> dict[str, Decimal]:
        return {
            role: getattr(self, f"{role}_return_percent").data / Decimal("100")
            for role in ("equity", "income", "liquidity", "alternatives")
        }
