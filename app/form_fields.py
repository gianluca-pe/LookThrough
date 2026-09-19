"""Shared decimal input validation and compact rendering."""

from __future__ import annotations

from wtforms import DecimalField as _DecimalField


class DecimalField(_DecimalField):
    """Validate business precision and trim only prefilled trailing zeros."""

    def _value(self) -> str:
        if self.raw_data:
            return self.raw_data[0]
        if self.data is None or self.places is not None:
            return super()._value()
        return format(self.data.normalize(), "f")

    def pre_validate(self, form):
        super().pre_validate(form)
        if self.data is None:
            return
        from wtforms.validators import ValidationError
        from app.decimal_policy import currency_places, exact_units
        name = self.short_name
        is_percent = '%' in self.label.text or 'percent' in name
        is_six = is_percent or 'quantity' in name or 'price' in name or name == 'quote_per_base_amount'
        try:
            places = 6 if is_six else currency_places(_form_currency(form, name))
            exact_units(self.data, places)
            if not is_six:
                exact_units(self.data, 3)
        except (TypeError, ValueError) as error:
            raise ValidationError(str(error)) from error


def _form_currency(form, name):
    """Resolve the same source currency as the business action, for input validation."""
    specific = {
        'annual_spending_amount': 'annual_spending_currency_code',
        'terminal_legacy_target_amount': 'terminal_legacy_target_currency_code',
        'threshold_amount': 'threshold_currency_code',
        'warning_buffer_amount': 'threshold_currency_code',
    }
    for key in (specific.get(name), 'currency_code', 'valuation_currency_code', 'default_currency_code', 'reporting_currency_code'):
        field = getattr(form, key, None) if key else None
        if field is not None and field.data:
            return field.data
    from flask import has_app_context
    if not has_app_context():
        return 'USD'
    from sqlalchemy import select
    from app.extensions import db
    from app.models import Instrument, PositionRegistration, Account, Portfolio
    for key, model in (('instrument_id', Instrument), ('registration_id', PositionRegistration), ('account_id', Account)):
        field = getattr(form, key, None)
        if field is not None and isinstance(field.data, int) and field.data > 0:
            row = db.session.get(model, field.data)
            if row is not None:
                if model is PositionRegistration:
                    return row.instrument.valuation_currency_code
                return row.valuation_currency_code if model is Instrument else row.default_currency_code
    portfolio = db.session.scalar(select(Portfolio).order_by(Portfolio.id).limit(1))
    return portfolio.reporting_currency_code if portfolio else 'USD'
