"""Settings, exact exports, and local backup/restore routes."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import func, select

from app.extensions import db
from app.maintenance_forms import RestoreConfirmForm, RestoreUploadForm
from app.models import Portfolio
from app.services.backup import (
    BackupValidationError,
    TABLE_ORDER,
    discard_staged_backup,
    export_backup,
    load_staged_backup,
    restore_backup,
    stage_backup,
    validate_backup,
)
from app.setup import _current_portfolio
from app.fx_settings import settings_context


maintenance_blueprint = Blueprint("maintenance", __name__)


def _staging_directory() -> Path:
    return Path(current_app.config["RESTORE_STAGING_DIRECTORY"])


def _today() -> date:
    return current_app.config["CURRENT_DATE_PROVIDER"]()


def _table_counts() -> dict[str, int]:
    return {
        name: db.session.scalar(select(func.count()).select_from(db.metadata.tables[name]))
        for name in TABLE_ORDER
    }


@maintenance_blueprint.get("/settings")
def settings() -> str:
    portfolio = _current_portfolio()
    counts = _table_counts()
    from app.services.retirement_plans import adopted_plan, plan_details
    return render_template(
        "maintenance/settings.html",
        portfolio=portfolio,
        retirement_plan=plan_details(adopted_plan(portfolio.id)) if portfolio else None,
        table_counts=counts,
        total_records=sum(counts.values()),
        restore_form=RestoreUploadForm(),
        **settings_context(portfolio),
        portfolio_name=portfolio.name if portfolio else None,
    )


@maintenance_blueprint.get("/backup/export")
def backup_export():
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    filename = f"lookthrough-backup-{_today().isoformat()}.json"
    return current_app.response_class(
        export_backup(),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@maintenance_blueprint.post("/backup/preview")
def backup_preview():
    portfolio = _current_portfolio()
    form = RestoreUploadForm()
    if not form.validate_on_submit():
        counts = _table_counts()
        return render_template(
            "maintenance/settings.html",
            portfolio=portfolio,
            table_counts=counts,
            total_records=sum(counts.values()),
            restore_form=form,
            **settings_context(portfolio),
            portfolio_name=portfolio.name if portfolio else None,
        ), 400
    try:
        validated = validate_backup(form.backup_file.data.read())
        token = stage_backup(validated, _staging_directory())
    except BackupValidationError as error:
        form.backup_file.errors.append(str(error))
        counts = _table_counts()
        return render_template(
            "maintenance/settings.html",
            portfolio=portfolio,
            table_counts=counts,
            total_records=sum(counts.values()),
            restore_form=form,
            **settings_context(portfolio),
            portfolio_name=portfolio.name if portfolio else None,
        ), 400
    confirm_form = RestoreConfirmForm(restore_token=token)
    return render_template(
        "maintenance/restore_preview.html",
        preview=validated.preview,
        form=confirm_form,
        portfolio_name=portfolio.name if portfolio else None,
    )


@maintenance_blueprint.post("/backup/restore")
def backup_restore():
    form = RestoreConfirmForm()
    token = form.restore_token.data or ""
    if request.form.get("cancel") == "1":
        discard_staged_backup(token, _staging_directory())
        flash("Restore cancelled. The staged backup was removed.", "info")
        return redirect(url_for("maintenance.settings"))
    try:
        validated = load_staged_backup(token, _staging_directory())
    except BackupValidationError as error:
        flash(str(error), "error")
        return redirect(url_for("maintenance.settings"))
    if not form.validate_on_submit():
        return render_template(
            "maintenance/restore_preview.html",
            preview=validated.preview,
            form=form,
            portfolio_name=(_current_portfolio().name if _current_portfolio() else None),
        ), 400
    try:
        restore_backup(validated)
    except BackupValidationError as error:
        flash(str(error), "error")
        return redirect(url_for("maintenance.settings"))
    discard_staged_backup(token, _staging_directory())
    flash("Backup restored. The validated local data is now active.", "success")
    return redirect(url_for("maintenance.settings"))
