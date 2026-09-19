"""Server-rendered cash and reinvested dividend routes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from flask import Blueprint, abort, current_app, redirect, render_template, url_for
from sqlalchemy import select

from app.activity import activity_cancel_url, activity_return_to
from app.dividend_forms import DividendForm
from app.extensions import db
from app.models import Account, Instrument
from app.services.activity import (
    ActivityValidationError,
    TRADE_INELIGIBLE_INSTRUMENT_TYPES,
)
from app.services.dividends import (
    DividendCommand,
    get_dividend_receipt,
    post_dividend,
    preview_dividend,
)
from app.setup import _current_portfolio


dividends_blueprint = Blueprint("dividends", __name__)


def _today() -> date:
    return current_app.config["CURRENT_DATE_PROVIDER"]()


def _records(portfolio_id: int) -> tuple[list[Account], list[Instrument]]:
    accounts = list(
        db.session.scalars(
            select(Account)
            .where(
                Account.portfolio_id == portfolio_id,
                Account.is_active.is_(True),
            )
            .order_by(Account.name, Account.id)
        )
    )
    instruments = list(
        db.session.scalars(
            select(Instrument)
            .where(
                Instrument.portfolio_id == portfolio_id,
                Instrument.is_active.is_(True),
                Instrument.instrument_type.notin_(
                    TRADE_INELIGIBLE_INSTRUMENT_TYPES
                ),
            )
            .order_by(Instrument.name, Instrument.id)
        )
    )
    return accounts, instruments


def _form(portfolio_id: int) -> DividendForm:
    accounts, instruments = _records(portfolio_id)
    form = DividendForm()
    form.account_id.choices = [
        (row.id, f"{row.institution.name} — {row.name}") for row in accounts
    ]
    form.instrument_id.choices = [
        (
            row.id,
            f"{row.name} ({row.ticker_or_isin})" if row.ticker_or_isin else row.name,
        )
        for row in instruments
    ]
    form.reinvestment_instrument_id.choices = [(0, "Choose an instrument")] + [
        (
            row.id,
            f"{row.name} ({row.ticker_or_isin})" if row.ticker_or_isin else row.name,
        )
        for row in instruments
    ]
    if not form.is_submitted():
        form.effective_date.data = _today()
        form.reinvestment_fee_amount.data = None
    return form


def _command(form: DividendForm, portfolio_id: int) -> DividendCommand:
    return DividendCommand(
        portfolio_id=portfolio_id,
        effective_date=form.effective_date.data,
        account_id=form.account_id.data,
        instrument_id=form.instrument_id.data,
        currency_code=form.currency_code.data,
        net_amount=form.net_amount.data,
        outcome=form.outcome.data,
        gross_amount=form.gross_amount.data,
        withholding_amount=form.withholding_amount.data,
        reinvestment_instrument_id=(
            form.reinvestment_instrument_id.data or None
        ),
        reinvestment_quantity=form.reinvestment_quantity.data,
        reinvestment_unit_price=form.reinvestment_unit_price.data,
        reinvestment_purchase_amount=form.reinvestment_purchase_amount.data,
        reinvestment_fee_amount=(
            form.reinvestment_fee_amount.data or Decimal("0")
        ),
    )


def _apply_error(form: DividendForm, error: ActivityValidationError) -> None:
    field = getattr(form, error.field, form.outcome)
    field.errors.append(error.message)


def _render(form: DividendForm, *, preview=None) -> str:
    portfolio = _current_portfolio()
    assert portfolio is not None
    return_to = activity_return_to()
    return render_template(
        "activity/dividend.html",
        form=form,
        preview=preview,
        return_to=return_to,
        cancel_url=activity_cancel_url(return_to),
        portfolio_name=portfolio.name,
    )


@dividends_blueprint.get("/activity/dividend/new")
def new() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    return _render(_form(portfolio.id))


@dividends_blueprint.post("/activity/dividend/preview")
def preview() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    form = _form(portfolio.id)
    dividend_preview = None
    if form.validate_on_submit():
        try:
            dividend_preview = preview_dividend(_command(form, portfolio.id))
        except ActivityValidationError as error:
            _apply_error(form, error)
    return _render(form, preview=dividend_preview)


@dividends_blueprint.post("/activity/dividend")
def post() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    form = _form(portfolio.id)
    if form.validate_on_submit():
        try:
            posted = post_dividend(_command(form, portfolio.id))
        except ActivityValidationError as error:
            _apply_error(form, error)
        else:
            return redirect(
                url_for(
                    "dividends.success",
                    transaction_id=posted.dividend_transaction_id,
                )
            )
    return _render(form)


@dividends_blueprint.get("/activity/dividend/<int:transaction_id>/success")
def success(transaction_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    receipt = get_dividend_receipt(transaction_id, portfolio_id=portfolio.id)
    if receipt is None:
        abort(404)
    return render_template(
        "activity/dividend_success.html",
        receipt=receipt,
        portfolio_name=portfolio.name,
    )
