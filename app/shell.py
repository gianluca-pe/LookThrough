"""Shared navigation, contextual Guide, template helpers, and error pages."""

from __future__ import annotations

import os

from flask import Blueprint, current_app, redirect, render_template, url_for

from app.classification_forms import ECONOMIC_ROLE_LABELS, FIRE_BUCKET_CHOICES
from app.template_filters import register_filters


shell_blueprint = Blueprint("shell", __name__)
register_filters(shell_blueprint)

FIRE_BUCKET_LABELS = {
    code: label for code, label in FIRE_BUCKET_CHOICES if code
}


@shell_blueprint.app_template_global("known_currency_codes")
def _known_currency_codes() -> list[str]:
    """Distinct currency codes already used in this portfolio, exposed for
    <datalist> suggestions on free-text currency fields.

    Presentation metadata only: entry is never restricted to these codes,
    and this is not a substitute for the backend's format validation.
    Imported lazily and guarded so error pages can never fail on it.
    """
    try:
        from sqlalchemy import select

        from app.extensions import db
        from app.models import Account, FxRate, Instrument, Portfolio

        codes: set[str] = set()
        for column in (
            Portfolio.reporting_currency_code,
            Account.default_currency_code,
            Instrument.valuation_currency_code,
            FxRate.base_currency_code,
            FxRate.quote_currency_code,
        ):
            codes.update(db.session.scalars(select(column)))
        return sorted(codes)
    except Exception:  # suggestions must never break a render
        return []


@shell_blueprint.app_template_global("static_url")
def _static_url(filename: str) -> str:
    """Static asset URL with a `?v=<file mtime>` cache-buster.

    Flask's default 12-hour cache could otherwise serve stale CSS/JS after an
    update is pulled. mtime keeps this localhost-simple: no hashing, no config.
    Guarded so error pages can never fail on it.
    """
    try:
        path = os.path.join(current_app.static_folder, filename)
        version = int(os.path.getmtime(path))
    except Exception:
        return url_for("static", filename=filename)
    return url_for("static", filename=filename, v=version)

# Core destinations plus the contextual Guide.
NAV_ITEMS = [
    {
        "label": "Overview",
        "endpoint": "overview.show",
        "active_endpoints": {"overview.show"},
    },
    {"label": "Retirement", "endpoint": "retirement.index", "active_endpoints": {
        "retirement.index", "retirement.budget", "retirement.details", "retirement.scenarios", "retirement.scenario", "retirement.income", "monte_carlo.index", "monte_carlo.details", "planning.retirement", "planning.targets", "planning.funding_assumptions"}},
    {
        "label": "Holdings",
        "endpoint": "positions.holdings",
        "active_endpoints": {
            "positions.holdings",
            "positions.new",
            "fixed_deposits.edit",
            "fixed_deposits.disposition",
        },
    },
    {
        "label": "Activity",
        "endpoint": "activity.history",
        "active_endpoints": {
            "activity.history", "activity.detail", "activity.reverse",
            "activity.new", "activity.post", "activity.preview",
            "activity.success", "activity.replacement",
            "activity.create_instrument",
            "dividends.new", "dividends.preview", "dividends.post",
            "dividends.success",
        },
    },
    {
        "label": "Instruments",
        "endpoint": "instruments.index",
        "active_endpoints": {"instruments.index", "instruments.edit_classification"},
    },
    {
        "label": "Update values",
        "endpoint": "positions.show_values",
        "active_endpoints": {
            "positions.show_values", "positions.add_price",
            "positions.add_statement_value", "positions.add_fx",
            "positions.save_routine_prices",
            "positions.save_routine_statements",
            "positions.save_routine_cash",
            "positions.save_routine_fx",
            "positions.show_setup_values", "positions.add_setup_price",
            "positions.add_setup_statement_value", "positions.add_setup_fx",
        },
    },
    {
        "label": "Accounts",
        "endpoint": "accounts.index",
        "active_endpoints": {
            "accounts.index",
            "accounts.new",
            "accounts.created",
            "accounts.detail",
            "accounts.edit",
            "accounts.archive",
            "accounts.new_cash_confirmation",
            "accounts.add_cash_confirmation",
            "institutions.new",
        },
    },
    {
        "label": "Settings & backup",
        "endpoint": "maintenance.settings",
        "active_endpoints": {
            "maintenance.settings",
            "maintenance.backup_export",
            "maintenance.backup_preview",
            "maintenance.backup_restore",
            "fx_settings.download", "fx_settings.save", "fx_settings.manual",
            "snapshots.index",
            "snapshots.new",
            "snapshots.detail",
        },
    },
    {"label": "Guide", "endpoint": "shell.guide"},
]


@shell_blueprint.app_context_processor
def inject_shell_context() -> dict:
    from app.activity import activity_type_links

    return {
        "nav_items": NAV_ITEMS,
        # Portfolio name and as-of context are supplied by routes that have them.
        "portfolio_name": None,
        "add_activity_endpoint": "activity.new",
        "as_of_label": None,
        # The single activity-type catalogue (app/activity.py), rendered by
        # the switcher nav and the /activity/new chooser cards.
        "activity_types": activity_type_links(),
        # Canonical classification labels are for presentation;
        # stored codes stay authoritative.
        "economic_role_labels": ECONOMIC_ROLE_LABELS,
        "fire_bucket_labels": FIRE_BUCKET_LABELS,
    }


@shell_blueprint.get("/")
def index() -> redirect:
    """Resume incomplete setup, otherwise open the useful Overview."""

    from app.setup import _current_portfolio, _setup_progress, _setup_records

    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    _, accounts = _setup_records(portfolio)
    if not accounts:
        return redirect(url_for("setup.show", step="accounts"))
    position_count, _, _ = _setup_progress(portfolio)
    if position_count == 0:
        return redirect(url_for("positions.show_positions"))
    return redirect(url_for("overview.show"))


@shell_blueprint.get("/guide")
def guide() -> str:
    """Task-led help with a stable page fallback and no financial writes."""

    from sqlalchemy import select

    from app.extensions import db
    from app.models import Account
    from app.setup import _current_portfolio

    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    accounts = list(
        db.session.scalars(
            select(Account)
            .where(
                Account.portfolio_id == portfolio.id,
                Account.is_active.is_(True),
            )
            .order_by(Account.name, Account.id)
        )
    )
    return render_template(
        "guide.html",
        portfolio_name=portfolio.name,
        accounts=[
            {
                "id": account.id,
                "name": account.name,
                "institution_name": account.institution.name,
            }
            for account in accounts
        ],
    )


@shell_blueprint.app_errorhandler(404)
def not_found(error) -> tuple[str, int]:
    return render_template("errors/404.html"), 404


@shell_blueprint.app_errorhandler(500)
def server_error(error) -> tuple[str, int]:
    return render_template("errors/500.html"), 500
