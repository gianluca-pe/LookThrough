"""Allocation target maintenance routes."""

from __future__ import annotations

from app.template_filters import money_amount

from decimal import Decimal

from datetime import date

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from app.extensions import db
from app.conventions import parse_iso_date
from app.planning_forms import (
    AllocationTargetsForm,
    FundingAssumptionsForm,
    RetirementProjectionForm,
)
from app.services.planning import (
    PlanningValidationError,
    allocation_targets,
    save_annual_inflation,
    save_allocation_targets,
)
from app.setup import _current_portfolio
from app.services.portfolio_summary import build_portfolio_summary
from app.services.retirement_plans import adopted_plan
from app.services.retirement import (
    RetirementValidationError,
    build_retirement_projection,
    retirement_assumption,
    save_retirement_assumption,
)


planning_blueprint = Blueprint("planning", __name__)


def _today() -> date:
    return current_app.config["CURRENT_DATE_PROVIDER"]()


def _as_of_date(portfolio) -> tuple[date, str | None]:
    raw = request.args.get("as_of")
    if not raw:
        return portfolio.default_as_of_date or _today(), None
    try:
        return parse_iso_date(raw), None
    except (TypeError, ValueError):
        return portfolio.default_as_of_date or _today(), "Enter a valid as-of date."


@planning_blueprint.route("/planning/targets", methods=["GET", "POST"])
def targets() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    form = AllocationTargetsForm()
    existing = allocation_targets(portfolio.id)
    if not form.is_submitted() and existing:
        for role_code, target in existing.items():
            getattr(form, f"{role_code}_minimum_percent").data = (
                target.minimum_decimal * Decimal("100")
            )
            getattr(form, f"{role_code}_maximum_percent").data = (
                target.maximum_decimal * Decimal("100")
            )
    if form.validate_on_submit():
        try:
            save_allocation_targets(portfolio.id, form.ranges_decimal())
        except PlanningValidationError as exc:
            db.session.rollback()
            field = getattr(form, exc.field, form.equity_minimum_percent)
            field.errors.append(str(exc))
        else:
            db.session.commit()
            flash("Allocation target ranges saved.", "success")
            return redirect(url_for("planning.targets"))
    return render_template(
        "planning/targets.html",
        form=form,
        portfolio=portfolio,
        portfolio_name=portfolio.name,
    )


@planning_blueprint.route("/planning/funding", methods=["GET", "POST"])
def funding_assumptions() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    if adopted_plan(portfolio.id):
        return redirect(url_for("retirement.index"))
    form = FundingAssumptionsForm()
    if not form.is_submitted() and portfolio.annual_inflation_decimal is not None:
        form.annual_inflation_percent.data = (
            portfolio.annual_inflation_decimal * Decimal("100")
        )
    if form.validate_on_submit():
        try:
            save_annual_inflation(
                portfolio.id,
                form.annual_inflation_percent.data / Decimal("100"),
            )
        except PlanningValidationError as exc:
            db.session.rollback()
            field = getattr(form, exc.field, form.annual_inflation_percent)
            field.errors.append(str(exc))
        else:
            db.session.commit()
            flash("Funding assumptions saved.", "success")
            return redirect(url_for("planning.funding_assumptions"))
    return render_template(
        "planning/funding.html",
        form=form,
        portfolio=portfolio,
        portfolio_name=portfolio.name,
    )


def _projection_chart(projection) -> dict[str, object] | None:
    """Presentation payload for the annual-path line chart.

    Maps the projection's annual rows to display values; computes nothing.
    The table beside the chart is the equivalent — the chart never carries a
    figure the table does not show.
    """

    if not projection["calculation_complete"] or not projection["years"]:
        return None
    return {
        "labels": [str(year["age"]) for year in projection["years"]],
        "ending_values": [
            float(money_amount(year["ending_value_amount"], currency=projection["reporting_currency"]).replace(",", ""))
            for year in projection["years"]
        ],
        "currency": projection["reporting_currency"],
    }


@planning_blueprint.route("/planning/retirement", methods=["GET", "POST"])
def retirement() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    if adopted_plan(portfolio.id):
        return redirect(url_for("retirement.index", as_of=request.args.get("as_of")))
    as_of_date, as_of_error = _as_of_date(portfolio)
    form = RetirementProjectionForm()
    existing = retirement_assumption(portfolio.id)
    if not form.is_submitted() and existing is not None:
        form.current_age_years.data = existing.current_age_years
        form.withdrawal_start_age_years.data = existing.withdrawal_start_age_years
        form.final_age_years.data = existing.final_age_years
        for role in ("equity", "income", "liquidity", "alternatives"):
            getattr(form, f"{role}_return_percent").data = (
                getattr(existing, f"{role}_return_decimal") * Decimal("100")
            )
        form.terminal_legacy_target_amount.data = (
            existing.terminal_legacy_target_amount
        )
    if form.validate_on_submit():
        try:
            save_retirement_assumption(
                portfolio.id,
                current_age_years=form.current_age_years.data,
                withdrawal_start_age_years=(
                    form.withdrawal_start_age_years.data
                ),
                final_age_years=form.final_age_years.data,
                role_returns=form.role_returns_decimal(),
                terminal_legacy_target_amount=(
                    form.terminal_legacy_target_amount.data
                ),
            )
        except RetirementValidationError as error:
            db.session.rollback()
            getattr(form, error.field, form.current_age_years).errors.append(
                error.message
            )
        else:
            db.session.commit()
            flash("Retirement projection assumptions saved.", "success")
            return redirect(
                url_for("planning.retirement", as_of=as_of_date.isoformat())
            )
    summary = build_portfolio_summary(portfolio, as_of_date)
    projection = build_retirement_projection(
        portfolio,
        summary,
        as_of_date,
        reporting_currency=portfolio.reporting_currency_code,
    )
    return render_template(
        "planning/retirement.html",
        form=form,
        portfolio=portfolio,
        summary=summary,
        projection=projection,
        projection_chart=_projection_chart(projection),
        as_of_date=as_of_date,
        as_of_error=as_of_error,
        portfolio_name=portfolio.name,
    )
