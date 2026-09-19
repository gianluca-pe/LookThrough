"""Explicit FX download, review, local save and offline manual recovery."""
from datetime import date
from hashlib import sha256
import json

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_wtf import FlaskForm
from itsdangerous import URLSafeTimedSerializer, BadSignature
from wtforms import DateField, StringField, HiddenField, BooleanField, TextAreaField
from wtforms.validators import DataRequired, Length

from app.extensions import db
from app.setup import _current_portfolio
from app.services.fx_reference import (
    FxReferenceError, ReferenceData, download_reference_rates, currency_uses,
    latest_set, set_rates, state_id, rate_text, validate_rates, missing_required,
    changes_for, save_set, state_digest,
)

fx_settings_blueprint = Blueprint("fx_settings", __name__)


class ReviewForm(FlaskForm):
    token = HiddenField(validators=[DataRequired()])
    replace = BooleanField("Replace this date’s rates and retain the previous edition")
    acknowledge = BooleanField("I checked the changes greater than 10%")


def _today():
    return current_app.config["CURRENT_DATE_PROVIDER"]()


def settings_context(portfolio):
    current = latest_set(_today())
    uses = currency_uses(portfolio, _today())
    rates = set_rates(current) if current else {}
    return {"fx_current": current, "fx_uses": uses,
            "fx_missing": sorted(set(uses) - rates.keys()) if current else [],
            "fx_rates": rates, "fx_download_form": FlaskForm(),
            "fx_stale": bool(current and (_today() - current.effective_date).days > portfolio.fx_stale_days) if portfolio else False}


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="fx-reference-review-v1")


def _scope():
    return sha256(str(db.engine.url).encode()).hexdigest()


def _preview(data, *, source, note, token=None, form=None, error=None, unavailable=()):
    portfolio = _current_portfolio()
    missing = missing_required(data.rates, portfolio, _today())
    previous = latest_set(data.effective_date)
    same_day = bool(previous and previous.effective_date == data.effective_date)
    changes = changes_for(data)
    if token is None:
        token = _serializer().dumps({"date": data.effective_date.isoformat(), "rates": data.rates,
                                     "source": source, "note": note, "state": state_id(),
                                     "digest": state_digest(), "scope": _scope(),
                                     "unavailable": list(unavailable)})
    form = form or ReviewForm(token=token)
    return render_template("maintenance/fx_review.html", form=form, data=data,
                           portfolio_name=portfolio.name, changes=changes, missing=missing,
                           same_day=same_day, previous=previous,
                           large=any(c["large"] for c in changes), error=error,
                           uses=currency_uses(portfolio, _today()), source=source,
                           unavailable=unavailable,
                           stale=(_today()-data.effective_date).days > portfolio.fx_stale_days)


@fx_settings_blueprint.post("/settings/fx/download")
def download():
    if _current_portfolio() is None:
        return redirect(url_for("maintenance.settings"))
    form = FlaskForm()
    if not form.validate_on_submit():
        flash("The update form expired. Try Update FX rates again.", "error")
        return redirect(url_for("maintenance.settings", _anchor="fx-settings"))
    try:
        data = download_reference_rates(_today())
    except FxReferenceError as exc:
        return render_template("maintenance/fx_failure.html", reason=str(exc),
                               portfolio_name=_current_portfolio().name), 502
    # One bulk request; only currencies with an actual local use are adopted.
    required = set(currency_uses(_current_portfolio(), _today())) | {"EUR"}
    data = ReferenceData(data.effective_date, {c:v for c,v in data.rates.items() if c in required},
                         tuple(c for c in data.unavailable if c in required))
    current = latest_set()
    if current and data.effective_date < current.effective_date:
        return render_template("maintenance/fx_failure.html",
                               reason=f"The provider returned {data.effective_date}, older than your latest saved set ({current.effective_date}).",
                               portfolio_name=_current_portfolio().name), 409
    if current and current.effective_date == data.effective_date and set_rates(current) == data.rates:
        flash(f"Rates for {data.effective_date} are already saved. No duplicates were created.", "info")
        return redirect(url_for("maintenance.settings", _anchor="fx-settings"))
    return _preview(data, source="banca_italia", note="Banca d’Italia public EUR reference rates.", unavailable=data.unavailable)


@fx_settings_blueprint.post("/settings/fx/save")
def save():
    if _current_portfolio() is None:
        return redirect(url_for("maintenance.settings"))
    form = ReviewForm()
    try:
        payload = _serializer().loads(form.token.data or "", max_age=3600)
        if payload["scope"] != _scope():
            raise FxReferenceError("This review belongs to another database. Start the update in this database’s Settings.")
        if payload["digest"] != state_digest():
            raise FxReferenceError("Saved rates changed after this review. Start again from Settings.")
        data = ReferenceData(date.fromisoformat(payload["date"]), validate_rates(payload["rates"]))
        if data.effective_date > _today():
            raise FxReferenceError("The reference date is in the future. Start again from Settings.")
    except (BadSignature, KeyError, TypeError, ValueError) as exc:
        flash(str(exc) if isinstance(exc, FxReferenceError) else "This FX review expired or is invalid. Start again from Settings.", "error")
        return redirect(url_for("maintenance.settings", _anchor="fx-settings"))
    missing = missing_required(data.rates, _current_portfolio(), _today())
    error = None
    if missing:
        error = "Required rates are missing: " + ", ".join(missing) + ". Use manual entry to provide a complete dated set."
    elif form.validate_on_submit():
        try:
            changed = save_set(data, source=payload["source"], note=payload["note"],
                               expected_state=payload["state"], replace=form.replace.data,
                               acknowledge=form.acknowledge.data, expected_digest=payload["digest"])
        except FxReferenceError as exc:
            error = str(exc)
        else:
            flash(f"FX rates for {data.effective_date} saved in this database." if changed else "These rates are already saved. Nothing changed.", "success")
            return redirect(url_for("maintenance.settings", _anchor="fx-settings"))
    return _preview(data, source=payload["source"], note=payload["note"], token=form.token.data,
                    form=form, error=error, unavailable=payload.get("unavailable", ())), 400


@fx_settings_blueprint.route("/settings/fx/manual", methods=["GET", "POST"])
def manual():
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("maintenance.settings"))
    uses = currency_uses(portfolio, _today())
    fields = {
        "effective_date": DateField("Reference date", validators=[DataRequired()]),
        "source_note": TextAreaField("Source or correction reason", validators=[DataRequired(), Length(max=1800)]),
        "state": HiddenField(),
    }
    for code in uses:
        if code != "EUR":
            fields[f"rate_{code}"] = StringField(f"1 EUR in {code}", validators=[Length(max=64)])
    form_class = type("ManualReferenceForm", (FlaskForm,), fields)
    form = form_class(data={"effective_date": _today(), "state": str(state_id())})
    current = latest_set(_today())
    if request.method == "POST" and form.validate_on_submit():
        if form.effective_date.data > _today():
            form.effective_date.errors.append("Choose today or an earlier reference date.")
        if form.state.data != str(state_id()):
            form.source_note.errors.append("Saved rates changed in another tab. Reload this page before entering a correction.")
        target = latest_set(form.effective_date.data)
        same_day = target is not None and target.effective_date == form.effective_date.data
        rates = set_rates(target) if same_day else {"EUR": "1"}
        entered = False
        for code in uses:
            if code == "EUR":
                continue
            field = form[f"rate_{code}"]
            if field.data and field.data.strip():
                entered = True
                try:
                    rates[code] = rate_text(field.data.strip(), manual=True)
                except FxReferenceError as exc:
                    field.errors.append(str(exc))
            elif code not in rates:
                field.errors.append("Enter this rate for a complete set on the chosen date.")
        if not entered and not form.errors:
            form.source_note.errors.append("Enter at least one rate to review.")
        if not form.errors:
            note = form.source_note.data.strip()
            if same_day:
                note += f" Unchanged observations retained from edition {target.id} ({target.source})."
            return _preview(ReferenceData(form.effective_date.data, validate_rates(rates)), source="manual", note=note)
    visual_order = ["effective_date", *(f"rate_{c}" for c in uses if c != "EUR"), "source_note", "state", "csrf_token"]
    first_error = next((name for name in visual_order if name in form.errors), None)
    return render_template("maintenance/fx_manual.html", form=form, uses=uses,
                           current=current, rates=set_rates(current) if current else {},
                           first_error=first_error, portfolio_name=portfolio.name), (400 if form.errors else 200)
