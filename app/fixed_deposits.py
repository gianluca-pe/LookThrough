"""Maintenance routes for fixed-deposit terms."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app.conventions import parse_iso_date
from app.extensions import db
from app.fixed_deposit_forms import FixedDepositTermsForm, MaturityDispositionForm
from app.models import Account, FixedDeposit, Instrument, PositionRegistration
from app.services.fixed_deposits import (
    FixedDepositCommand,
    FixedDepositValidationError,
    MaturityDispositionCommand,
    latest_positive_principal,
    maturity_snapshot,
    post_maturity_disposition,
    preview_maturity_disposition,
    save_fixed_deposit_terms,
)
from app.setup import _current_portfolio


fixed_deposits_blueprint = Blueprint("fixed_deposits", __name__)


def _today() -> date:
    return current_app.config["CURRENT_DATE_PROVIDER"]()


def _return_as_of() -> date | None:
    raw = request.args.get("as_of")
    if not raw:
        return None
    try:
        return parse_iso_date(raw)
    except (TypeError, ValueError):
        return None


def _registration(portfolio_id: int, registration_id: int) -> PositionRegistration:
    registration = db.session.scalar(
        select(PositionRegistration)
        .join(Instrument)
        .where(
            PositionRegistration.id == registration_id,
            Instrument.portfolio_id == portfolio_id,
            Instrument.instrument_type == "fixed_deposit",
        )
    )
    if registration is None:
        abort(404)
    return registration


def _populate_form(
    form: FixedDepositTermsForm,
    registration: PositionRegistration,
    terms: FixedDeposit | None,
) -> None:
    form.currency_code.data = (
        terms.currency_code
        if terms is not None
        else registration.instrument.valuation_currency_code
    )
    form.start_date.data = (
        terms.start_date
        if terms is not None
        else (registration.opening_date or _today())
    )
    form.maturity_date.data = terms.maturity_date if terms is not None else None
    form.annual_rate_percent.data = (
        terms.annual_rate_decimal * Decimal("100")
        if terms is not None and terms.annual_rate_decimal is not None
        else None
    )
    form.expected_maturity_proceeds_amount.data = (
        terms.expected_maturity_proceeds_amount if terms is not None else None
    )
    form.maturity_action.data = (
        terms.maturity_action if terms is not None else "undecided"
    )
    form.notes.data = terms.notes if terms is not None else None


@fixed_deposits_blueprint.route(
    "/positions/<int:registration_id>/fixed-deposit", methods=["GET", "POST"]
)
def edit(registration_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    registration = _registration(portfolio.id, registration_id)
    terms = db.session.scalar(
        select(FixedDeposit).where(
            FixedDeposit.position_registration_id == registration.id
        )
    )
    return_as_of = _return_as_of()
    form = FixedDepositTermsForm()
    if not form.is_submitted():
        _populate_form(form, registration, terms)

    if form.validate_on_submit():
        try:
            saved = save_fixed_deposit_terms(
                portfolio.id,
                FixedDepositCommand(
                    registration_id=registration.id,
                    currency_code=form.currency_code.data,
                    start_date=form.start_date.data,
                    maturity_date=form.maturity_date.data,
                    annual_rate_decimal=(
                        form.annual_rate_percent.data / Decimal("100")
                        if form.annual_rate_percent.data is not None
                        else None
                    ),
                    expected_maturity_proceeds_amount=(
                        form.expected_maturity_proceeds_amount.data
                    ),
                    maturity_action=form.maturity_action.data,
                    notes=form.notes.data,
                ),
            )
        except FixedDepositValidationError as exc:
            db.session.rollback()
            getattr(form, exc.field, form.maturity_date).errors.append(str(exc))
        else:
            db.session.commit()
            flash(
                f"Fixed deposit terms saved for {registration.instrument.name}.",
                "success",
            )
            return redirect(
                url_for(
                    "positions.holdings",
                    **(
                        {"as_of": return_as_of.isoformat()}
                        if return_as_of is not None
                        else {}
                    ),
                )
            )
    elif form.is_submitted():
        db.session.rollback()

    as_of_date = return_as_of or portfolio.default_as_of_date or _today()
    return render_template(
        "fixed_deposits/edit.html",
        form=form,
        registration={
            "id": registration.id,
            "instrument_name": registration.instrument.name,
            "account_name": registration.account.name,
            "institution_name": registration.account.institution.name,
            "currency_code": registration.instrument.valuation_currency_code,
            "opening_date": registration.opening_date,
        },
        maturity=maturity_snapshot(registration, terms, as_of_date),
        return_as_of=return_as_of,
        portfolio_name=portfolio.name,
    )


def _cash_account_choices(portfolio_id: int, currency_code: str):
    accounts = list(
        db.session.scalars(
            select(Account)
            .where(
                Account.portfolio_id == portfolio_id,
                Account.is_active.is_(True),
                Account.cash_tracking_mode == "separate_cash",
                Account.cash_settlement_account_id.is_(None),
            )
            .order_by(Account.name, Account.id)
        )
    )
    return [(0, "Choose a cash account")] + [
        (
            account.id,
            f"{account.institution.name} — {account.name}",
        )
        for account in accounts
        if account.is_multicurrency or account.default_currency_code == currency_code
    ]


def _disposition_command(
    form: MaturityDispositionForm,
    *,
    portfolio_id: int,
    registration_id: int,
) -> MaturityDispositionCommand:
    return MaturityDispositionCommand(
        portfolio_id=portfolio_id,
        registration_id=registration_id,
        disposition_type=form.disposition_type.data,
        effective_date=form.effective_date.data,
        confirmed_principal_amount=form.confirmed_principal_amount.data,
        confirmed_interest_amount=form.confirmed_interest_amount.data,
        cash_account_id=form.cash_account_id.data or None,
        note=form.note.data,
        successor_instrument_name=form.successor_instrument_name.data,
        successor_reference=form.successor_reference.data,
        successor_maturity_date=form.successor_maturity_date.data,
        successor_annual_rate_decimal=(
            form.successor_annual_rate_percent.data / Decimal("100")
            if form.successor_annual_rate_percent.data is not None
            else None
        ),
        successor_expected_proceeds_amount=(
            form.successor_expected_proceeds_amount.data
        ),
        successor_maturity_action=form.successor_maturity_action.data,
    )


@fixed_deposits_blueprint.route(
    "/positions/<int:registration_id>/fixed-deposit/disposition",
    methods=["GET", "POST"],
)
def disposition(registration_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    registration = _registration(portfolio.id, registration_id)
    terms = registration.fixed_deposit
    if terms is None:
        return redirect(
            url_for("fixed_deposits.edit", registration_id=registration.id)
        )
    return_as_of = _return_as_of()
    as_of_date = return_as_of or portfolio.default_as_of_date or _today()
    maturity = maturity_snapshot(registration, terms, as_of_date)
    if maturity.maturity_status not in {"due", "overdue"}:
        if maturity.maturity_status == "disposed":
            flash("This fixed deposit already has a maturity disposition.", "info")
            return redirect(url_for("activity.history"))
        abort(404)

    form = MaturityDispositionForm()
    form.cash_account_id.choices = _cash_account_choices(
        portfolio.id, terms.currency_code
    )
    if not form.is_submitted():
        form.effective_date.data = max(_today(), terms.maturity_date)
        principal = latest_positive_principal(
            registration.id, on_or_before=terms.maturity_date
        )
        form.confirmed_principal_amount.data = principal
        form.confirmed_interest_amount.data = (
            terms.expected_maturity_proceeds_amount - principal
            if principal is not None
            and terms.expected_maturity_proceeds_amount is not None
            and terms.expected_maturity_proceeds_amount >= principal
            else Decimal("0")
        )
        form.successor_maturity_action.data = "undecided"

    preview = None
    if form.validate_on_submit():
        try:
            command = _disposition_command(
                form,
                portfolio_id=portfolio.id,
                registration_id=registration.id,
            )
            preview = preview_maturity_disposition(command)
            if form.confirm_disposition.data:
                posted = post_maturity_disposition(command)
                flash(
                    f"Maturity disposition recorded for {registration.instrument.name}.",
                    "success",
                )
                return redirect(
                    url_for(
                        "activity.detail",
                        transaction_id=posted.transaction_id,
                    )
                )
        except FixedDepositValidationError as exc:
            db.session.rollback()
            getattr(form, exc.field, form.disposition_type).errors.append(str(exc))
    elif form.is_submitted():
        db.session.rollback()

    return render_template(
        "fixed_deposits/disposition.html",
        form=form,
        preview=preview,
        registration={
            "id": registration.id,
            "instrument_name": registration.instrument.name,
            "account_name": registration.account.name,
            "institution_name": registration.account.institution.name,
            "currency_code": terms.currency_code,
        },
        terms=terms,
        maturity=maturity,
        return_as_of=return_as_of,
        portfolio_name=portfolio.name,
    )
