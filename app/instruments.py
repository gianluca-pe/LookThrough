"""Instrument list and classification-maintenance routes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import or_, select

from app.classification_forms import ECONOMIC_ROLE_LABELS, InstrumentClassificationForm
from app.conventions import parse_iso_date
from app.extensions import db
from app.models import ECONOMIC_ROLE_CODES, Instrument, PositionRegistration
from app.services.classification import (
    ClassificationCommand,
    ClassificationSnapshot,
    ClassificationValidationError,
    classifications_as_of,
    current_fire_bucket_code,
    save_classification,
)
from app.services.funding import PERIODS as FUNDING_PERIODS
from app.services.portfolio_summary import build_portfolio_summary
from app.setup import _current_portfolio


instruments_blueprint = Blueprint("instruments", __name__)

INSTRUMENT_VIEW_CODES = ("current", "former", "all")


def _today() -> date:
    return current_app.config["CURRENT_DATE_PROVIDER"]()


def _as_of_date() -> date:
    return parse_iso_date(request.args["as_of"]) if request.args.get("as_of") else _today()


def _instrument(portfolio_id: int, instrument_id: int) -> Instrument:
    instrument = db.session.scalar(
        select(Instrument).where(
            Instrument.id == instrument_id,
            Instrument.portfolio_id == portfolio_id,
            Instrument.is_active.is_(True),
        )
    )
    if instrument is None:
        abort(404)
    return instrument


def _instrument_vm(
    instrument: Instrument,
    snapshot: ClassificationSnapshot | None,
    *,
    holding_status: str | None = None,
) -> dict[str, object]:
    role_weights = [
        {
            "code": role_code,
            "label": ECONOMIC_ROLE_LABELS[role_code],
            "weight_decimal": weight,
        }
        for role_code, weight in snapshot.role_weights
    ] if snapshot else []
    return {
        "id": instrument.id,
        "name": instrument.name,
        "ticker_or_isin": instrument.ticker_or_isin,
        "instrument_type": instrument.instrument_type,
        "valuation_currency_code": instrument.valuation_currency_code,
        "fire_bucket_code": current_fire_bucket_code(instrument.fire_bucket_code),
        "classification_date": snapshot.effective_date if snapshot else None,
        "role_weights": role_weights,
        "primary_role_code": snapshot.primary_role_code if snapshot else None,
        "is_classified": snapshot is not None,
        "holding_status": holding_status,
    }


def _holding_statuses(
    portfolio,
    instruments: list[Instrument],
    as_of_date: date,
) -> dict[int, str]:
    """Classify active instrument identities without changing their audit trail."""

    summary = build_portfolio_summary(portfolio, as_of_date)
    current_ids = {
        holding["instrument_id"] for holding in summary["holdings"]
    }
    registered_ids = set(
        db.session.scalars(
            select(PositionRegistration.instrument_id)
            .join(Instrument)
            .where(
                Instrument.portfolio_id == portfolio.id,
                or_(
                    PositionRegistration.opening_date.is_(None),
                    PositionRegistration.opening_date <= as_of_date,
                ),
            )
            .distinct()
        )
    )
    return {
        instrument.id: (
            "current"
            if instrument.id in current_ids
            else "former"
            if instrument.id in registered_ids
            else "unused"
        )
        for instrument in instruments
    }


@instruments_blueprint.get("/instruments")
def index() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    as_of_date = _as_of_date()
    requested_view = request.args.get("view", "current")
    instrument_view = (
        requested_view if requested_view in INSTRUMENT_VIEW_CODES else "current"
    )
    all_instruments = list(
        db.session.scalars(
            select(Instrument)
            .where(
                Instrument.portfolio_id == portfolio.id,
                Instrument.is_active.is_(True),
            )
            .order_by(Instrument.name, Instrument.id)
        )
    )
    holding_statuses = _holding_statuses(
        portfolio, all_instruments, as_of_date
    )
    instrument_counts = {
        "current": sum(
            status == "current" for status in holding_statuses.values()
        ),
        "former": sum(
            status == "former" for status in holding_statuses.values()
        ),
        "all": len(all_instruments),
    }
    instruments = [
        instrument
        for instrument in all_instruments
        if instrument_view == "all"
        or holding_statuses[instrument.id] == instrument_view
    ]
    snapshots = classifications_as_of(
        [instrument.id for instrument in instruments], as_of_date
    )
    return render_template(
        "instruments/index.html",
        instruments=[
            _instrument_vm(
                instrument,
                snapshots.get(instrument.id),
                holding_status=holding_statuses[instrument.id],
            )
            for instrument in instruments
        ],
        instrument_view=instrument_view,
        instrument_counts=instrument_counts,
        instrument_view_codes=INSTRUMENT_VIEW_CODES,
        as_of_date=as_of_date,
        # True only on the plain default landing (no explicit view or as-of
        # in the query string); the template then keeps classification links
        # bare instead of echoing redundant parameters.
        default_view=not request.args.get("view") and not request.args.get("as_of"),
        portfolio_name=portfolio.name,
    )


def _populate_form(
    form: InstrumentClassificationForm,
    instrument: Instrument,
    snapshot: ClassificationSnapshot | None,
) -> None:
    form.effective_date.data = _today()
    if snapshot is not None:
        if snapshot.primary_role_code:
            form.classification_mode.data = "simple"
            form.primary_role_code.data = snapshot.primary_role_code
        else:
            form.classification_mode.data = "advanced"
            for role_code, weight in snapshot.role_weights:
                getattr(form, f"role_{role_code}_percent").data = (
                    weight * Decimal("100")
                )
        form.source_note.data = snapshot.source_note
    form.fire_bucket_code.data = current_fire_bucket_code(instrument.fire_bucket_code) or ""


@instruments_blueprint.route(
    "/instruments/<int:instrument_id>/classification", methods=["GET", "POST"]
)
def edit_classification(instrument_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    instrument = _instrument(portfolio.id, instrument_id)
    current_snapshot = classifications_as_of([instrument.id], _today()).get(
        instrument.id
    )
    return_view = request.args.get("view")
    if return_view not in INSTRUMENT_VIEW_CODES:
        return_view = None
    return_as_of = request.args.get("as_of")
    try:
        parsed_return_as_of = (
            parse_iso_date(return_as_of) if return_as_of else None
        )
    except (TypeError, ValueError):
        parsed_return_as_of = None
    form = InstrumentClassificationForm()
    if not form.is_submitted():
        _populate_form(form, instrument, current_snapshot)

    if form.validate_on_submit():
        try:
            save_classification(
                portfolio.id,
                ClassificationCommand(
                    instrument_id=instrument.id,
                    effective_date=form.effective_date.data,
                    role_weights=form.role_weights(),
                    source_note=form.source_note.data,
                    fire_bucket_code=form.fire_bucket_code.data,
                    # Metadata absent from this form must survive classification edits.
                    # An explicit metadata-edit workflow would own later changes.
                    preserve_existing_metadata=True,
                    replace_existing=form.replace_existing.data,
                ),
            )
        except ClassificationValidationError as exc:
            db.session.rollback()
            field = getattr(form, exc.field, form.classification_mode)
            field.errors.append(str(exc))
        else:
            db.session.commit()
            flash(f"Classification saved for {instrument.name}.", "success")
            return redirect(
                url_for(
                    "instruments.index",
                    **(
                        {"view": return_view}
                        if return_view is not None
                        else {}
                    ),
                    **(
                        {"as_of": parsed_return_as_of.isoformat()}
                        if parsed_return_as_of is not None
                        else {}
                    ),
                )
            )
    elif form.is_submitted():
        db.session.rollback()

    return render_template(
        "instruments/classification.html",
        instrument=_instrument_vm(instrument, current_snapshot),
        form=form,
        return_view=return_view,
        return_as_of=parsed_return_as_of,
        funding_periods={period["code"]: period for period in FUNDING_PERIODS},
        portfolio_name=portfolio.name,
    )
