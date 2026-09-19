"""Browser routes for explicit frozen portfolio snapshots."""

from __future__ import annotations

from datetime import date

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from app.extensions import db
from app.conventions import parse_iso_date
from app.models import PortfolioSnapshot
from app.services.snapshots import (
    SnapshotValidationError,
    build_snapshot_payload,
    compare_snapshots,
    previous_snapshot,
    save_snapshot,
    snapshot_payload,
    snapshots_for_portfolio,
)
from app.setup import _current_portfolio
from app.snapshot_forms import NOTE_MAX_LENGTH, SnapshotForm


snapshots_blueprint = Blueprint("snapshots", __name__)


def _today() -> date:
    return current_app.config["CURRENT_DATE_PROVIDER"]()


@snapshots_blueprint.get("/snapshots")
def index() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    rows = snapshots_for_portfolio(portfolio.id)
    return render_template(
        "snapshots/index.html",
        snapshots=[(row, snapshot_payload(row)) for row in rows],
        portfolio=portfolio,
        portfolio_name=portfolio.name,
    )


@snapshots_blueprint.route("/snapshots/new", methods=["GET", "POST"])
def new() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    default_date = portfolio.default_as_of_date or _today()
    if request.method == "GET" and request.args.get("as_of"):
        try:
            default_date = parse_iso_date(request.args["as_of"])
        except (TypeError, ValueError):
            pass
    form = SnapshotForm(data={"as_of_date": default_date})
    preview = None
    if form.validate_on_submit():
        try:
            snapshot = save_snapshot(
                portfolio,
                form.as_of_date.data,
                note=form.note.data,
            )
        except SnapshotValidationError as error:
            db.session.rollback()
            getattr(form, error.field, form.as_of_date).errors.append(error.message)
        else:
            flash("Portfolio snapshot saved.", "success")
            return redirect(url_for("snapshots.detail", snapshot_id=snapshot.id))
    preview_date = form.as_of_date.data
    if isinstance(preview_date, date):
        try:
            preview = build_snapshot_payload(portfolio, preview_date)
        except SnapshotValidationError:
            preview = None
    return render_template(
        "snapshots/new.html",
        form=form,
        preview=preview,
        portfolio=portfolio,
        note_max_length=NOTE_MAX_LENGTH,
        portfolio_name=portfolio.name,
    ), (400 if form.is_submitted() and form.errors else 200)


@snapshots_blueprint.get("/snapshots/<int:snapshot_id>")
def detail(snapshot_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    snapshot = db.session.get(PortfolioSnapshot, snapshot_id)
    if snapshot is None or snapshot.portfolio_id != portfolio.id:
        abort(404)
    prior = previous_snapshot(snapshot)
    return render_template(
        "snapshots/detail.html",
        snapshot=snapshot,
        payload=snapshot_payload(snapshot),
        previous=prior,
        comparison=(compare_snapshots(prior, snapshot) if prior else None),
        portfolio=portfolio,
        portfolio_name=portfolio.name,
    )
