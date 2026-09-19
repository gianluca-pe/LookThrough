"""Server-rendered Buy/Sell preview, post, and success routes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

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
from sqlalchemy import select

from app.activity_forms import ReversalForm, TradeForm
from app.conventions import normalize_currency_code, parse_iso_date
from app.extensions import db
from app.models import Account, Instrument
from app.services.activity import (
    ActivityValidationError,
    InlineInstrumentCommand,
    TradeCommand,
    create_trade_instrument,
    get_replacement_context,
    get_trade_receipt,
    post_trade,
    preview_trade,
)
from app.services.activity_history import (
    ACTIVITY_STATUS_LABELS,
    ACTIVITY_TYPE_LABELS,
    ActivityHistoryFilters,
    ReversalCommand,
    get_activity,
    list_activities,
    preview_reversal,
    reverse_activity,
)
from app.services.exports import activity_csv
from app.setup import _current_portfolio


activity_blueprint = Blueprint("activity", __name__)


def _today() -> date:
    return current_app.config["CURRENT_DATE_PROVIDER"]()


# Known-safe Cancel targets for the activity forms, named by the `return_to`
# query param; anything else falls back to Overview. No open redirects.
RETURN_TO_ENDPOINTS = {
    "holdings": "positions.holdings",
    "activity_history": "activity.history",
}


def activity_return_to() -> str | None:
    """The whitelisted `return_to` key from the query string, or None."""

    key = request.args.get("return_to")
    return key if key in RETURN_TO_ENDPOINTS else None


def activity_cancel_url(return_to: str | None) -> str:
    return url_for(RETURN_TO_ENDPOINTS.get(return_to or "", "overview.show"))


# The activity-type catalogue is defined once here; the switcher nav
# (activity/_switcher.html) and the /activity/new chooser cards both render
# from it, so labels, order, and disabled placeholders cannot drift. The
# chooser forwards the caller's return_to context except for fixed-deposit
# onboarding, which has its own continuation to the terms step
# and deliberately does not.
ACTIVITY_TYPES: tuple[dict[str, object], ...] = (
    {
        "code": "buy",
        "label": "Buy",
        "endpoint": "activity.new",
        "params": {"type": "buy"},
        "hint": "You bought units of an instrument you track — or a new one you add inline.",
    },
    {
        "code": "sell",
        "label": "Sell",
        "endpoint": "activity.new",
        "params": {"type": "sell"},
        "hint": "You sold units and received cash.",
    },
    {
        "code": "dividend",
        "label": "Dividend",
        "endpoint": "dividends.new",
        "params": {},
        "hint": "Record cash received or actual reinvestment.",
        "extra_hint": "Accumulating fund? Nothing to record — retained income is already in the NAV.",
    },
    {
        "code": "fixed_deposit",
        "label": "Fixed deposit",
        "endpoint": "positions.new",
        "params": {"kind": "fixed_deposit"},
        "forwards_return_to": False,
        "hint": "Add an existing bank deposit and continue to its maturity terms. This is not a Buy.",
    },
    {
        "code": "transfer_fx",
        "label": "Transfer / FX",
        "disabled": True,
        "hint": "Move cash between accounts or currencies. Not available yet.",
    },
    {
        "code": "more",
        "label": "More…",
        "disabled": True,
        "hint": "Interest, deposit, withdrawal, fee, and adjustment are not available yet.",
    },
)


def activity_type_links(return_to: str | None = None) -> tuple[dict[str, object], ...]:
    """ACTIVITY_TYPES with urls resolved; the chooser forwards `return_to`."""

    links: list[dict[str, object]] = []
    for item in ACTIVITY_TYPES:
        link = dict(item)
        if not item.get("disabled"):
            params = dict(item["params"])
            if return_to and item.get("forwards_return_to", True):
                params["return_to"] = return_to
            link["url"] = url_for(item["endpoint"], **params)
        links.append(link)
    return tuple(links)


def _activity_records(portfolio_id: int) -> tuple[list[Account], list[Instrument]]:
    accounts = list(
        db.session.scalars(
            select(Account)
            .where(Account.portfolio_id == portfolio_id, Account.is_active.is_(True))
            .order_by(Account.name, Account.id)
        )
    )
    instruments = list(
        db.session.scalars(
            select(Instrument)
            .where(
                Instrument.portfolio_id == portfolio_id,
                Instrument.is_active.is_(True),
                # Buy/Sell post quantities; cash and fixed deposits are never
                # quantity-tracked, so they do not belong in this picker.
                Instrument.instrument_type.notin_(("cash", "fixed_deposit")),
            )
            .order_by(Instrument.name, Instrument.id)
        )
    )
    return accounts, instruments


def _trade_form(
    portfolio_id: int, *, activity_type: str | None = None
) -> TradeForm:
    accounts, instruments = _activity_records(portfolio_id)
    form = TradeForm()
    if form.is_submitted() and "quantity_mode" not in request.form:
        # Submissions without a quantity mode use the explicitly entered quantity.
        form.quantity_mode.data = "entered"
    form.account_id.choices = [
        (row.id, f"{row.institution.name} — {row.name}") for row in accounts
    ]
    instrument_choices = [
        (
            row.id,
            f"{row.name} ({row.ticker_or_isin})" if row.ticker_or_isin else row.name,
        )
        for row in instruments
    ]
    effective_type = form.activity_type.data or activity_type
    form.instrument_id.choices = (
        [(0, "Add a new instrument")] + instrument_choices
        if effective_type == "buy"
        else instrument_choices
    )
    if not form.is_submitted():
        form.activity_type.data = activity_type
        form.effective_date.data = _today()
        form.fee_amount.data = None
        _prefill_trade(form)
    return form


def _prefill_trade(form: TradeForm) -> None:
    if request.args.get("effective_date"):
        try:
            form.effective_date.data = parse_iso_date(request.args["effective_date"])
        except ValueError:
            pass
    for field_name in ("account_id", "instrument_id"):
        raw = request.args.get(field_name, "")
        if raw.isdigit():
            getattr(form, field_name).data = int(raw)
    for field_name in ("quantity", "unit_price", "fee_amount"):
        raw = request.args.get(field_name, "")
        if not raw:
            continue
        try:
            value = Decimal(raw)
        except InvalidOperation:
            continue
        if value.is_finite():
            getattr(form, field_name).data = value


def _command(form: TradeForm, portfolio_id: int) -> TradeCommand:
    replacement_sale_id = (
        int(form.replacement_sale_id.data)
        if form.replacement_sale_id.data
        and form.replacement_sale_id.data.isdigit()
        else None
    )
    return TradeCommand(
        portfolio_id=portfolio_id,
        activity_type=form.activity_type.data,
        effective_date=form.effective_date.data,
        account_id=form.account_id.data,
        instrument_id=form.instrument_id.data,
        quantity=form.quantity.data,
        unit_price=form.unit_price.data,
        fee_amount=form.fee_amount.data or Decimal("0"),
        quantity_mode=form.quantity_mode.data,
        replacement_sale_id=replacement_sale_id,
    )


def _apply_service_error(form: TradeForm, error: ActivityValidationError) -> None:
    field = getattr(form, error.field, form.activity_type)
    field.errors.append(error.message)


def _replacement_for_form(form: TradeForm, portfolio_id: int):
    raw = form.replacement_sale_id.data
    if not raw or not raw.isdigit():
        return None
    return get_replacement_context(int(raw), portfolio_id=portfolio_id)


def _replacement_or_404(form: TradeForm, portfolio_id: int):
    replacement = _replacement_for_form(form, portfolio_id)
    if form.replacement_sale_id.data and replacement is None:
        abort(404)
    return replacement


def _render_trade(
    form: TradeForm,
    *,
    preview=None,
    instrument_form_open: bool = False,
    replacement=None,
) -> str:
    portfolio = _current_portfolio()
    assert portfolio is not None
    replacement = replacement or _replacement_for_form(form, portfolio.id)
    return_to = activity_return_to()
    return render_template(
        "activity/new.html",
        activity_type=form.activity_type.data,
        form=form,
        preview=preview,
        replacement=replacement,
        instrument_form_open=instrument_form_open,
        return_to=return_to,
        cancel_url=activity_cancel_url(return_to),
        portfolio_name=portfolio.name,
    )


def _add_choose_instrument_error(form: TradeForm) -> bool:
    if form.instrument_id.data != 0:
        return False
    form.instrument_id.errors.append(
        "Save the new instrument before recording this Buy."
    )
    return True


def _return_query(form: TradeForm, instrument_id: int) -> dict[str, object]:
    values: dict[str, object] = {"instrument_id": instrument_id}
    if form.effective_date.data is not None:
        values["effective_date"] = form.effective_date.data.isoformat()
    if form.account_id.data:
        values["account_id"] = form.account_id.data
    for field_name in ("quantity", "unit_price", "fee_amount"):
        value = getattr(form, field_name).data
        if isinstance(value, Decimal) and value.is_finite():
            values[field_name] = str(value)
    return values


def _history_filters() -> tuple[ActivityHistoryFilters, dict[str, str]]:
    errors: dict[str, str] = {}

    def parsed_date(name: str) -> date | None:
        raw = request.args.get(name, "").strip()
        if not raw:
            return None
        try:
            return parse_iso_date(raw)
        except (TypeError, ValueError):
            errors[name] = "Enter a date in YYYY-MM-DD format."
            return None

    def parsed_id(name: str) -> int | None:
        raw = request.args.get(name, "").strip()
        if not raw:
            return None
        if not raw.isdigit() or int(raw) <= 0:
            errors[name] = "Choose a valid option."
            return None
        return int(raw)

    currency = request.args.get("currency", "").strip().upper() or None
    if currency is not None:
        try:
            currency = normalize_currency_code(currency)
        except (TypeError, ValueError):
            errors["currency"] = "Enter a three-letter currency code."
            currency = None
    activity_type = request.args.get("type", "").strip() or None
    if activity_type is not None and activity_type not in ACTIVITY_TYPE_LABELS:
        errors["type"] = "Choose a supported activity type."
        activity_type = None
    status = request.args.get("status", "").strip() or None
    if status is not None and status not in ACTIVITY_STATUS_LABELS:
        errors["status"] = "Choose a supported status."
        status = None
    filters = ActivityHistoryFilters(
        date_from=parsed_date("date_from"),
        date_to=parsed_date("date_to"),
        account_id=parsed_id("account_id"),
        instrument_id=parsed_id("instrument_id"),
        activity_type=activity_type,
        currency_code=currency,
        status=status,
    )
    if (
        filters.date_from is not None
        and filters.date_to is not None
        and filters.date_from > filters.date_to
    ):
        errors["date_to"] = "End date must be on or after the start date."
    return filters, errors


@activity_blueprint.get("/activity/new")
def new() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    activity_type = request.args.get("type")
    if activity_type not in {"buy", "sell"}:
        return_to = activity_return_to()
        return render_template(
            "activity/new.html",
            activity_type=None,
            form=None,
            preview=None,
            return_to=return_to,
            chooser_types=activity_type_links(return_to),
            portfolio_name=portfolio.name,
        )
    return _render_trade(_trade_form(portfolio.id, activity_type=activity_type))


@activity_blueprint.post("/activity/preview")
def preview() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    form = _trade_form(portfolio.id)
    _replacement_or_404(form, portfolio.id)
    trade_preview = None
    if form.validate_on_submit():
        if not _add_choose_instrument_error(form):
            try:
                trade_preview = preview_trade(_command(form, portfolio.id))
            except ActivityValidationError as error:
                _apply_service_error(form, error)
    return _render_trade(form, preview=trade_preview)


@activity_blueprint.post("/activity/new")
def post() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    form = _trade_form(portfolio.id)
    _replacement_or_404(form, portfolio.id)
    if form.validate_on_submit():
        if not _add_choose_instrument_error(form):
            try:
                posted = post_trade(_command(form, portfolio.id))
            except ActivityValidationError as error:
                _apply_service_error(form, error)
            else:
                return redirect(
                    url_for("activity.success", transaction_id=posted.transaction_id)
                )
    return _render_trade(form)


@activity_blueprint.post("/activity/instruments")
def create_instrument() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    form = _trade_form(portfolio.id)
    replacement = _replacement_or_404(form, portfolio.id)
    if form.validate_on_submit():
        if replacement is not None and form.account_id.data != replacement.account_id:
            form.account_id.errors.append(
                "Use the same account as the source sale so the proceeds remain "
                "traceable."
            )
        elif (
            replacement is not None
            and form.new_valuation_currency_code.data
            != replacement.settlement_currency
        ):
            form.new_valuation_currency_code.errors.append(
                "Use the sale's settlement currency "
                f"({replacement.settlement_currency}) for this replacement."
            )
        else:
            try:
                created = create_trade_instrument(
                    InlineInstrumentCommand(
                        portfolio_id=portfolio.id,
                        name=form.new_instrument_name.data,
                        ticker_or_isin=form.new_ticker_or_isin.data,
                        valuation_currency_code=(
                            form.new_valuation_currency_code.data
                        ),
                        instrument_type=form.new_instrument_type.data,
                        account_id=form.account_id.data or None,
                    )
                )
            except ActivityValidationError as error:
                _apply_service_error(form, error)
            else:
                flash(
                    f"{created.name} saved. Finish recording the Buy.",
                    "success",
                )
                query = _return_query(form, created.instrument_id)
                if activity_return_to():
                    query["return_to"] = activity_return_to()
                if replacement is not None:
                    return redirect(
                        url_for(
                            "activity.replacement",
                            transaction_id=replacement.sale_transaction_id,
                            **query,
                        )
                    )
                return redirect(url_for("activity.new", type="buy", **query))
    return _render_trade(form, instrument_form_open=True)


@activity_blueprint.get("/activity/<int:transaction_id>/replacement")
def replacement(transaction_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    context = get_replacement_context(transaction_id, portfolio_id=portfolio.id)
    if context is None:
        abort(404)
    form = _trade_form(portfolio.id, activity_type="buy")
    form.replacement_sale_id.data = str(context.sale_transaction_id)
    if not request.args.get("account_id"):
        form.account_id.data = context.account_id
    if not request.args.get("effective_date"):
        form.effective_date.data = context.effective_date
    if not request.args.get("new_valuation_currency_code"):
        form.new_valuation_currency_code.data = context.settlement_currency
    return _render_trade(form, replacement=context)


@activity_blueprint.get("/activity/<int:transaction_id>/success")
def success(transaction_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    receipt = get_trade_receipt(transaction_id, portfolio_id=portfolio.id)
    if receipt is None:
        abort(404)
    return render_template(
        "activity/success.html",
        receipt=receipt,
        replacement=get_replacement_context(
            transaction_id, portfolio_id=portfolio.id
        ),
        portfolio_name=portfolio.name,
    )


@activity_blueprint.get("/activity")
def history() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    filters, filter_errors = _history_filters()
    accounts, instruments = _activity_records(portfolio.id)
    try:
        activities = list_activities(portfolio.id, filters)
    except ActivityValidationError as error:
        filter_errors.setdefault(error.field, error.message)
        activities = ()
    return render_template(
        "activity/history.html",
        activities=activities,
        filters=filters,
        filter_errors=filter_errors,
        # The CSV export and the empty-state copy branch on the same raw
        # filter state; resolved here so the template never reads the request.
        export_url=url_for("activity.export_csv", **request.args.to_dict()),
        filters_active=bool(request.args),
        account_options=tuple(
            {
                "value": row.id,
                "label": f"{row.institution.name} — {row.name}",
            }
            for row in accounts
        ),
        instrument_options=tuple(
            {"value": row.id, "label": row.name} for row in instruments
        ),
        type_options=tuple(ACTIVITY_TYPE_LABELS.items()),
        status_options=tuple(ACTIVITY_STATUS_LABELS.items()),
        portfolio_name=portfolio.name,
    )


@activity_blueprint.get("/activity.csv")
def export_csv():
    """Download the currently filtered Activity history as exact CSV."""

    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    filters, filter_errors = _history_filters()
    if filter_errors:
        abort(400)
    content = activity_csv(portfolio.id, filters)
    filename = f"lookthrough-activity-{_today().isoformat()}.csv"
    return current_app.response_class(
        content,
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@activity_blueprint.get("/activity/<int:transaction_id>")
def detail(transaction_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    activity = get_activity(transaction_id, portfolio_id=portfolio.id)
    if activity is None:
        abort(404)
    return render_template(
        "activity/detail.html",
        activity=activity,
        portfolio_name=portfolio.name,
    )


@activity_blueprint.route(
    "/activity/<int:transaction_id>/reverse", methods=["GET", "POST"]
)
def reverse(transaction_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    activity = get_activity(transaction_id, portfolio_id=portfolio.id)
    if activity is None:
        abort(404)
    form = ReversalForm()
    if form.validate_on_submit():
        try:
            result = reverse_activity(
                ReversalCommand(
                    portfolio_id=portfolio.id,
                    transaction_id=transaction_id,
                    reason=form.reason.data,
                )
            )
        except ActivityValidationError as error:
            if error.field == "reason":
                form.reason.errors.append(error.message)
            else:
                flash(error.message, "warning")
                return redirect(
                    url_for("activity.detail", transaction_id=transaction_id)
                )
        else:
            count = len(result.original_transaction_ids)
            message = (
                "Linked dividend and purchase reversed."
                if count > 1
                else f"{activity.activity_label} reversed."
            )
            flash(message, "success")
            return redirect(
                url_for("activity.detail", transaction_id=transaction_id)
            )
    try:
        reversal_preview = preview_reversal(
            transaction_id, portfolio_id=portfolio.id
        )
    except ActivityValidationError as error:
        flash(error.message, "warning")
        return redirect(url_for("activity.detail", transaction_id=transaction_id))
    return render_template(
        "activity/reverse.html",
        form=form,
        reversal=reversal_preview,
        portfolio_name=portfolio.name,
    ), (400 if form.is_submitted() else 200)
