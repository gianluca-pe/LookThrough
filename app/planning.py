"""Allocation targets and compatibility redirects for retired planning pages."""

from __future__ import annotations

from app.template_filters import money_amount

from decimal import Decimal

from datetime import date

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from app.extensions import db
from app.conventions import parse_iso_date
from app.planning_forms import AllocationTargetsForm
from app.services.planning import (
    PlanningValidationError,
    allocation_targets,
    save_allocation_targets,
)
from app.setup import _current_portfolio


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
    return _retired_planning_page()


def _retired_planning_page():
    if _current_portfolio() is None:
        return redirect(url_for("setup.show"))
    if request.method == "POST":
        flash("This older form has been retired. No changes were saved. Use Retirement to review and save your plan.", "warning")
    return redirect(
        url_for("retirement.index", as_of=request.args.get("as_of")),
        code=303 if request.method == "POST" else 302,
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
    return _retired_planning_page()
