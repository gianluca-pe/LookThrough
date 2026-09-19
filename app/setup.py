"""Setup routes for portfolio, institution, and account records."""

from __future__ import annotations

from decimal import Decimal

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    Account,
    FxRate,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    Price,
    ValuationObservation,
)
from app.services.cash import cash_setup_complete
from app.setup_forms import AccountSetupForm, InstitutionSetupForm, PortfolioSetupForm
from app.setup_view_models import build_setup_view_model


setup_blueprint = Blueprint("setup", __name__)


def _current_portfolio() -> Portfolio | None:
    return db.session.scalar(select(Portfolio).order_by(Portfolio.id).limit(1))


def _setup_records(
    portfolio: Portfolio | None,
) -> tuple[list[Institution], list[Account]]:
    if portfolio is None:
        return [], []

    institutions = list(
        db.session.scalars(
            select(Institution)
            .where(Institution.portfolio_id == portfolio.id)
            .order_by(Institution.name, Institution.id)
        )
    )
    accounts = list(
        db.session.scalars(
            select(Account)
            .where(Account.portfolio_id == portfolio.id, Account.is_active.is_(True))
            .order_by(Account.name, Account.id)
        )
    )
    return institutions, accounts


def _setup_progress(portfolio: Portfolio | None) -> tuple[int, bool, bool]:
    if portfolio is None:
        return 0, False, False
    position_count = db.session.scalar(
        select(func.count(PositionRegistration.id))
        .join(Instrument)
        .where(Instrument.portfolio_id == portfolio.id)
    )
    price_count = db.session.scalar(
        select(func.count(Price.id)).join(Instrument).where(
            Instrument.portfolio_id == portfolio.id
        )
    )
    observation_count = db.session.scalar(
        select(func.count(ValuationObservation.id))
        .join(PositionRegistration)
        .join(Instrument)
        .where(Instrument.portfolio_id == portfolio.id)
    )
    fx_count = db.session.scalar(select(func.count(FxRate.id)))
    return (
        position_count,
        bool(price_count or observation_count or fx_count),
        cash_setup_complete(portfolio.id),
    )


def _account_form(institutions: list[Institution]) -> AccountSetupForm:
    form = AccountSetupForm()
    form.institution_id.choices = [
        (institution.id, institution.name) for institution in institutions
    ]
    return form


def _create_institution(portfolio_id: int, form: InstitutionSetupForm) -> Institution:
    """Persist the common setup/routine institution-creation contract."""

    institution = Institution(portfolio_id=portfolio_id, name=form.name.data)
    db.session.add(institution)
    db.session.commit()
    return institution


def _create_account(portfolio_id: int, form: AccountSetupForm) -> Account:
    """Persist the common setup/routine account-creation contract."""

    account = Account(
        portfolio_id=portfolio_id,
        institution_id=form.institution_id.data,
        name=form.name.data,
        reference=form.reference.data or None,
        account_type=form.account_type.data,
        default_currency_code=form.default_currency_code.data,
        is_multicurrency=form.is_multicurrency.data,
        cash_tracking_mode=form.cash_tracking_mode.data,
        portfolio_share_decimal=form.portfolio_share_percent.data / Decimal("100"),
        present_access_decimal=form.present_access_percent.data / Decimal("100"),
        earliest_access_date=form.earliest_access_date.data,
        access_note=form.access_note.data or None,
        relationship_eligible=form.relationship_eligible.data,
        is_active=True,
    )
    db.session.add(account)
    db.session.commit()
    return account


def _render_setup(
    *,
    current_step: str,
    portfolio: Portfolio | None,
    portfolio_form: PortfolioSetupForm | None = None,
    institution_form: InstitutionSetupForm | None = None,
    account_form: AccountSetupForm | None = None,
) -> str:
    institutions, accounts = _setup_records(portfolio)
    position_count, values_started, cash_complete = _setup_progress(portfolio)
    view_model = build_setup_view_model(
        current_step=current_step,
        portfolio=portfolio,
        institutions=institutions,
        accounts=accounts,
        position_count=position_count,
        values_started=values_started,
        cash_complete=cash_complete,
    )

    if current_step == "portfolio":
        if portfolio_form is None:
            portfolio_form = PortfolioSetupForm(
                data=(
                    {
                        "name": portfolio.name,
                        "reporting_currency_code": portfolio.reporting_currency_code,
                        "annual_spending_amount": portfolio.annual_spending_amount,
                        "annual_spending_currency_code": (
                            portfolio.annual_spending_currency_code
                            or portfolio.reporting_currency_code
                        ),
                        "default_as_of_date": portfolio.default_as_of_date,
                    }
                    if portfolio is not None
                    else None
                )
            )
        from app.services.retirement_plans import adopted_plan, plan_details
        return render_template(
            "setup/portfolio.html",
            setup=view_model,
            form=portfolio_form,
            retirement_plan=plan_details(adopted_plan(portfolio.id)) if portfolio else None,
            portfolio_name=portfolio.name if portfolio is not None else None,
        )

    if institution_form is None:
        institution_form = InstitutionSetupForm()
    if account_form is None:
        account_form = _account_form(institutions)
    return render_template(
        "setup/accounts.html",
        setup=view_model,
        institution_form=institution_form,
        account_form=account_form,
        portfolio_name=portfolio.name if portfolio is not None else None,
    )


@setup_blueprint.get("/setup")
def show() -> str:
    portfolio = _current_portfolio()
    requested_step = request.args.get("step")

    if portfolio is not None and requested_step == "positions":
        return redirect(url_for("positions.show_positions"))
    if portfolio is not None and requested_step == "values":
        return redirect(url_for("positions.show_setup_values"))
    if portfolio is None:
        current_step = "portfolio"
    elif requested_step in {"portfolio", "accounts"}:
        current_step = requested_step
    else:
        current_step = "accounts"

    return _render_setup(current_step=current_step, portfolio=portfolio)


@setup_blueprint.post("/setup/portfolio")
def save_portfolio() -> str:
    portfolio = _current_portfolio()
    form = PortfolioSetupForm()
    from app.services.retirement_plans import adopted_plan
    plan = adopted_plan(portfolio.id) if portfolio else None
    if plan:
        if ("annual_spending_amount" in request.form or "annual_spending_currency_code" in request.form):
            flash("Spending is now managed in Retirement. Review the adopted plan to change it.", "info")
        # The retired legacy fields are retained for older backups, not editable.
        form.annual_spending_amount.data = portfolio.annual_spending_amount
        form.annual_spending_currency_code.data = portfolio.annual_spending_currency_code
    if "annual_spending_currency_code" not in request.form and not plan:
        # An omitted currency must preserve the existing amount's currency.
        # This also keeps older local form submissions safe.
        form.annual_spending_currency_code.data = (
            portfolio.annual_spending_currency_code
            if portfolio is not None and portfolio.annual_spending_currency_code
            else form.reporting_currency_code.data
        )

    if not form.validate_on_submit():
        return _render_setup(
            current_step="portfolio", portfolio=portfolio, portfolio_form=form
        )

    if portfolio is None:
        portfolio = Portfolio()
        db.session.add(portfolio)

    portfolio.name = form.name.data
    portfolio.reporting_currency_code = form.reporting_currency_code.data
    portfolio.annual_spending_amount = form.annual_spending_amount.data
    portfolio.annual_spending_currency_code = (
        form.annual_spending_currency_code.data
    )
    portfolio.default_as_of_date = form.default_as_of_date.data
    db.session.commit()

    flash("Portfolio details saved.", "success")
    next_step = "portfolio" if form.save_and_finish_later.data else "accounts"
    return redirect(url_for("setup.show", step=next_step))


@setup_blueprint.post("/setup/institutions")
def add_institution() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show", step="portfolio"))

    form = InstitutionSetupForm()
    institutions, _ = _setup_records(portfolio)
    is_valid = form.validate_on_submit()
    if is_valid and any(
        institution.name.casefold() == form.name.data.casefold()
        for institution in institutions
    ):
        form.name.errors.append("This institution is already in the portfolio.")
        is_valid = False

    if not is_valid:
        return _render_setup(
            current_step="accounts", portfolio=portfolio, institution_form=form
        )

    institution = _create_institution(portfolio.id, form)

    flash(f"{institution.name} added.", "success")
    return redirect(url_for("setup.show", step="accounts"))


@setup_blueprint.post("/setup/accounts")
def add_account() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show", step="portfolio"))

    institutions, _ = _setup_records(portfolio)
    form = _account_form(institutions)

    if not form.validate_on_submit():
        return _render_setup(
            current_step="accounts", portfolio=portfolio, account_form=form
        )

    account = _create_account(portfolio.id, form)

    if account.cash_tracking_mode == "separate_cash":
        flash(f"{account.name} added.", "success")
        return redirect(
            url_for(
                "accounts.new_cash_confirmation",
                account_id=account.id,
                currency=account.default_currency_code,
                return_to="setup_accounts",
            )
        )
    flash(
        f"{account.name} added. Cash is inside its aggregate value — "
        "no separate opening cash balance is needed.",
        "success",
    )
    return redirect(url_for("setup.show", step="accounts"))
