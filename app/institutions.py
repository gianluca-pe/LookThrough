"""Routine institution creation outside the initial setup checklist."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, url_for
from sqlalchemy import func, select

from app.extensions import db
from app.models import Institution
from app.setup import _create_institution, _current_portfolio
from app.setup_forms import InstitutionSetupForm


institutions_blueprint = Blueprint("institutions", __name__)


@institutions_blueprint.route("/institutions/new", methods=["GET", "POST"])
def new() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))

    form = InstitutionSetupForm()
    if form.validate_on_submit():
        duplicate = db.session.scalar(
            select(Institution.id).where(
                Institution.portfolio_id == portfolio.id,
                func.lower(Institution.name) == form.name.data.lower(),
            )
        )
        if duplicate is not None:
            form.name.errors.append("This institution is already in the portfolio.")
        else:
            institution = _create_institution(portfolio.id, form)
            flash(f"{institution.name} added. Now add its account.", "success")
            return redirect(
                url_for("accounts.new", institution=institution.id)
            )

    return render_template(
        "institutions/new.html",
        form=form,
        portfolio_name=portfolio.name,
    )
