"""Backend contracts for current positions, manual values, and Holdings."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import select

from app.conventions import normalize_currency_code, parse_iso_date
from app.extensions import db
from app.models import (
    ECONOMIC_ROLE_CODES,
    FIRE_BUCKET_CODES,
    Account, FxRate, Institution, Instrument, PositionRegistration, Posting, Price,
    Transaction, ValuationObservation,
)
from app.position_forms import (
    NEW_INSTRUMENT_SENTINEL,
    FxRateForm,
    OpeningPositionForm,
    PriceForm,
    RoutineUpdateSectionForm,
    StatementValueForm,
)
from app.services.portfolio_summary import build_portfolio_summary
from app.services.exports import holdings_csv
from app.services.fx import (
    apply_same_day_fx_correction,
    assess_fx_change,
    fx_correction_context,
    same_day_fx_rates,
)
from app.services.valuation import (
    StatementValueValidationError,
    record_statement_value,
)
from app.services.routine_updates import (
    RoutineUpdateValidationError,
    build_routine_update_session,
    save_cash_updates,
    save_fx_updates,
    save_price_updates,
    save_statement_updates,
)
from app.setup import _current_portfolio, _setup_progress, _setup_records
from app.setup_view_models import build_setup_view_model


positions_blueprint = Blueprint("positions", __name__)


def _today() -> date:
    return current_app.config["CURRENT_DATE_PROVIDER"]()


def _portfolio_records(portfolio_id: int):
    accounts = list(
        db.session.scalars(
            select(Account)
            .join(Institution)
            .where(Account.portfolio_id == portfolio_id, Account.is_active.is_(True))
            .order_by(Institution.name, Account.name, Account.id)
        )
    )
    instruments = list(
        db.session.scalars(
            select(Instrument)
            .where(
                Instrument.portfolio_id == portfolio_id,
                Instrument.is_active.is_(True),
            )
            .order_by(Instrument.name)
        )
    )
    registrations = list(
        db.session.scalars(
            select(PositionRegistration)
            .join(Instrument)
            .where(Instrument.portfolio_id == portfolio_id)
            .order_by(PositionRegistration.id)
        )
    )
    return accounts, instruments, registrations


def _opening_form(accounts, instruments) -> OpeningPositionForm:
    form = OpeningPositionForm()
    form.account_id.choices = [
        (row.id, f"{row.institution.name} — {row.name}") for row in accounts
    ]
    form.instrument_id.choices = [(NEW_INSTRUMENT_SENTINEL, "Add a new instrument")] + [
        (
            row.id,
            f"{row.name} ({row.ticker_or_isin})" if row.ticker_or_isin else row.name,
        )
        for row in instruments
    ]
    if not form.is_submitted():
        form.effective_date.data = _today()
    return form


def _position_vm(registrations):
    return [
        {
            "id": row.id,
            "instrument_name": row.instrument.name,
            "account_name": row.account.name,
            "tracking_mode": row.tracking_mode,
            "currency_code": row.instrument.valuation_currency_code,
            "opening_date": row.opening_date,
        }
        for row in registrations
    ]


def _save_opening_position(
    *,
    portfolio_id: int,
    form: OpeningPositionForm,
    accounts: list[Account],
    instruments: list[Instrument],
    registrations: list[PositionRegistration],
) -> PositionRegistration | None:
    """Persist one opening position through the shared setup/routine path."""

    account = next(row for row in accounts if row.id == form.account_id.data)
    instrument = next(
        (row for row in instruments if row.id == form.instrument_id.data), None
    )
    if instrument is None:
        instrument = Instrument(
            portfolio_id=portfolio_id,
            name=form.new_instrument_name.data,
            ticker_or_isin=form.ticker_or_isin.data or None,
            instrument_type=form.instrument_type.data,
            valuation_currency_code=form.valuation_currency_code.data,
            is_active=True,
        )
        db.session.add(instrument)
        db.session.flush()
    duplicate = any(
        row.account_id == account.id and row.instrument_id == instrument.id
        for row in registrations
    )
    if duplicate:
        form.instrument_id.errors.append(
            "This position is already registered for the account."
        )
        return None

    registration = PositionRegistration(
        account_id=account.id,
        instrument_id=instrument.id,
        tracking_mode=form.tracking_mode.data,
        opening_date=form.effective_date.data,
    )
    db.session.add(registration)
    db.session.flush()
    if registration.tracking_mode == "transaction_tracked":
        transaction = Transaction(
            portfolio_id=portfolio_id,
            transaction_type="opening_balance",
            effective_date=form.effective_date.data,
            status="posted",
        )
        db.session.add(transaction)
        db.session.flush()
        db.session.add(
            Posting(
                transaction_id=transaction.id,
                account_id=account.id,
                posting_kind="instrument",
                instrument_id=instrument.id,
                currency_code=instrument.valuation_currency_code,
                quantity_delta=form.opening_quantity.data,
            )
        )
    else:
        db.session.add(
            ValuationObservation(
                position_registration_id=registration.id,
                effective_date=form.effective_date.data,
                native_value_amount=form.statement_value.data,
                currency_code=instrument.valuation_currency_code,
            )
        )
    db.session.commit()
    return registration


def _routine_opening_form(
    accounts: list[Account], instruments: list[Instrument], *, kind: str | None
) -> OpeningPositionForm:
    form = _opening_form(accounts, instruments)
    selected_account = None
    try:
        requested_account_id = int(request.args.get("account", ""))
    except ValueError:
        requested_account_id = None
    if requested_account_id is not None:
        selected_account = next(
            (account for account in accounts if account.id == requested_account_id),
            None,
        )
    if not form.is_submitted() and selected_account is not None:
        form.account_id.data = selected_account.id
    if kind == "fixed_deposit":
        # The route, not hidden browser fields, owns these invariants.
        form.instrument_id.data = NEW_INSTRUMENT_SENTINEL
        form.instrument_type.data = "fixed_deposit"
        form.tracking_mode.data = "statement_valued"
        if not form.is_submitted() and selected_account is not None:
            form.valuation_currency_code.data = selected_account.default_currency_code
    return form


@positions_blueprint.get("/setup/positions")
def show_positions() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    institutions, setup_accounts = _setup_records(portfolio)
    _, values_started, cash_complete = _setup_progress(portfolio)
    accounts, instruments, registrations = _portfolio_records(portfolio.id)
    setup = build_setup_view_model(
        current_step="positions", portfolio=portfolio, institutions=institutions,
        accounts=setup_accounts, position_count=len(registrations),
        values_started=values_started,
        cash_complete=cash_complete,
    )
    return render_template(
        "setup/positions.html", setup=setup, form=_opening_form(accounts, instruments),
        positions=_position_vm(registrations), portfolio_name=portfolio.name,
    )


@positions_blueprint.post("/setup/positions")
def add_position() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    institutions, setup_accounts = _setup_records(portfolio)
    _, values_started, cash_complete = _setup_progress(portfolio)
    accounts, instruments, registrations = _portfolio_records(portfolio.id)
    form = _opening_form(accounts, instruments)
    if form.validate_on_submit():
        registration = _save_opening_position(
            portfolio_id=portfolio.id,
            form=form,
            accounts=accounts,
            instruments=instruments,
            registrations=registrations,
        )
        if registration is not None:
            flash(
                f"{registration.instrument.name} added to current positions.",
                "success",
            )
            return redirect(url_for("positions.show_positions"))

    db.session.rollback()
    setup = build_setup_view_model(
        current_step="positions", portfolio=portfolio, institutions=institutions,
        accounts=setup_accounts, position_count=len(registrations),
        values_started=values_started,
        cash_complete=cash_complete,
    )
    return render_template(
        "setup/positions.html", setup=setup, form=form,
        positions=_position_vm(registrations), portfolio_name=portfolio.name,
    )


@positions_blueprint.route("/positions/new", methods=["GET", "POST"])
def new() -> str:
    """Add an existing holding without re-entering initial setup."""

    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    accounts, instruments, registrations = _portfolio_records(portfolio.id)
    if not accounts:
        flash("Add an account before adding a holding.", "info")
        return redirect(url_for("accounts.new"))
    kind = request.args.get("kind")
    if kind not in {None, "fixed_deposit"}:
        kind = None
    form = _routine_opening_form(accounts, instruments, kind=kind)
    if form.validate_on_submit():
        registration = _save_opening_position(
            portfolio_id=portfolio.id,
            form=form,
            accounts=accounts,
            instruments=instruments,
            registrations=registrations,
        )
        if registration is not None:
            if kind == "fixed_deposit":
                flash(
                    f"{registration.instrument.name} added. Now record its terms.",
                    "success",
                )
                return redirect(
                    url_for(
                        "fixed_deposits.edit",
                        registration_id=registration.id,
                    )
                )
            flash(
                f"{registration.instrument.name} added to {registration.account.name}.",
                "success",
            )
            return redirect(
                url_for("accounts.detail", account_id=registration.account_id)
            )

    db.session.rollback()
    account_arg = request.args.get("account")
    return render_template(
        "positions/new.html",
        form=form,
        kind=kind,
        form_action=url_for("positions.new", account=account_arg, kind=kind),
        cancel_url=(
            url_for("accounts.detail", account_id=account_arg)
            if account_arg
            else url_for("positions.holdings")
        ),
        portfolio_name=portfolio.name,
    )


def _value_forms(instruments, registrations, *, bind_request: bool = True):
    form_kwargs = {} if bind_request else {"formdata": None}
    price = PriceForm(**form_kwargs)
    price_instruments = _price_instruments(instruments, registrations)
    price.instrument_id.choices = [
        (
            row.id,
            f"{row.name} ({row.ticker_or_isin})" if row.ticker_or_isin else row.name,
        )
        for row in price_instruments
    ]
    statement = StatementValueForm(**form_kwargs)
    statement.registration_id.choices = [
        (
            row.id,
            f"{row.instrument.name} ({row.instrument.ticker_or_isin}) — {row.account.name}"
            if row.instrument.ticker_or_isin
            else f"{row.instrument.name} — {row.account.name}",
        )
        for row in registrations if row.tracking_mode == "statement_valued"
        and row.closing_date is None
    ]
    fx = FxRateForm(**form_kwargs)
    for form in (price, statement, fx):
        if not form.is_submitted():
            form.effective_date.data = _today()
    return price, statement, fx


def _price_instruments(instruments, registrations) -> list[Instrument]:
    """Return instruments whose current tracking contract actually consumes prices."""

    eligible_ids = {
        row.instrument_id
        for row in registrations
        if row.tracking_mode == "transaction_tracked"
        and row.account.is_active
        and row.instrument.instrument_type not in {"cash", "fixed_deposit"}
    }
    return [row for row in instruments if row.id in eligible_ids]


def _query_currency(name: str) -> str | None:
    raw = request.args.get(name)
    if not raw:
        return None
    try:
        return normalize_currency_code(raw, field_name=name)
    except (TypeError, ValueError):
        return None


def _apply_value_form_context(
    price: PriceForm,
    statement: StatementValueForm,
    fx: FxRateForm,
    *,
    instruments: list[Instrument],
    registrations: list[PositionRegistration],
    reporting_currency: str,
) -> dict[str, dict[str, object]]:
    """Prefill trusted form context from a targeted, no-JavaScript GET link."""

    contexts: dict[str, dict[str, object]] = {}
    price_instruments = _price_instruments(instruments, registrations)
    instrument_id = request.args.get("instrument", type=int)
    instrument = next(
        (row for row in price_instruments if row.id == instrument_id), None
    )
    if instrument is not None:
        price.instrument_id.data = instrument.id
        price.currency_code.data = instrument.valuation_currency_code
        affected_accounts = tuple(
            dict.fromkeys(
                row.account.name
                for row in registrations
                if row.instrument_id == instrument.id
                and row.tracking_mode == "transaction_tracked"
                and row.account.is_active
            )
        )
        contexts["price"] = {
            "instrument_name": instrument.name,
            "currency": instrument.valuation_currency_code,
            "account_names": affected_accounts,
        }

    registration_id = request.args.get("registration", type=int)
    registration = next(
        (
            row
            for row in registrations
            if row.id == registration_id and row.tracking_mode == "statement_valued"
        ),
        None,
    )
    if registration is not None:
        statement.registration_id.data = registration.id
        statement.currency_code.data = registration.instrument.valuation_currency_code
        contexts["statement"] = {
            "instrument_name": registration.instrument.name,
            "account_name": registration.account.name,
            "currency": registration.instrument.valuation_currency_code,
        }

    base_currency = _query_currency("base")
    quote_currency = _query_currency("quote")
    if base_currency and quote_currency is None:
        quote_currency = reporting_currency
    if base_currency and quote_currency and base_currency != quote_currency:
        fx.base_currency_code.data = base_currency
        fx.quote_currency_code.data = quote_currency
        contexts["fx"] = {
            "base_currency": base_currency,
            "quote_currency": quote_currency,
        }
    return contexts


def _values_setup_view_model(portfolio, registrations):
    institutions, setup_accounts = _setup_records(portfolio)
    _, values_started, cash_complete = _setup_progress(portfolio)
    return build_setup_view_model(
        current_step="values",
        portfolio=portfolio,
        institutions=institutions,
        accounts=setup_accounts,
        position_count=len(registrations),
        values_started=values_started,
        cash_complete=cash_complete,
    )


def _value_action_urls(pathway: str) -> dict[str, str]:
    if pathway == "setup":
        return {
            "price": url_for("positions.add_setup_price"),
            "statement": url_for("positions.add_setup_statement_value"),
            "fx": url_for("positions.add_setup_fx"),
        }
    return {
        "price": url_for("positions.add_price"),
        "statement": url_for("positions.add_statement_value"),
        "fx": url_for("positions.add_fx"),
    }


def _routine_date() -> tuple[date, str | None]:
    raw = request.args.get("as_of")
    if not raw:
        return _today(), None
    try:
        return parse_iso_date(raw), None
    except (TypeError, ValueError):
        return _today(), "Enter a valid update date."


def _render_values(
    pathway: str,
    *,
    routine_date: date | None = None,
    routine_errors: dict[str, str] | None = None,
    routine_values: dict[str, str] | None = None,
    routine_fx_warnings: dict[str, object] | None = None,
) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    _, instruments, registrations = _portfolio_records(portfolio.id)
    price_form, statement_form, fx_form = _value_forms(
        instruments,
        registrations,
        bind_request=routine_errors is None,
    )
    value_target_context = _apply_value_form_context(
        price_form,
        statement_form,
        fx_form,
        instruments=instruments,
        registrations=registrations,
        reporting_currency=portfolio.reporting_currency_code,
    )
    setup = (
        _values_setup_view_model(portfolio, registrations)
        if pathway == "setup"
        else None
    )
    routine = None
    routine_date_error = None
    routine_form = None
    if pathway == "maintenance":
        if routine_date is None:
            routine_date, routine_date_error = _routine_date()
        routine = build_routine_update_session(portfolio, routine_date)
        routine_form = RoutineUpdateSectionForm(formdata=None)
    return render_template(
        "values/index.html", setup=setup, price_form=price_form,
        statement_form=statement_form, fx_form=fx_form,
        values_pathway=pathway, value_action_urls=_value_action_urls(pathway),
        value_target_context=value_target_context,
        portfolio_name=portfolio.name,
        routine=routine,
        routine_form=routine_form,
        routine_date_error=routine_date_error,
        routine_errors=routine_errors or {},
        routine_values=routine_values or {},
        routine_fx_warnings=routine_fx_warnings or {},
        fx_change_warning=None,
        fx_correction=None,
        individual_updates_open=bool(value_target_context),
    )


@positions_blueprint.get("/values")
def show_values() -> str:
    return _render_values("maintenance")


_ROUTINE_SECTIONS = {
    "prices": {
        "rows": "price_rows",
        "anchor": "routine-prices-heading",
        "label": "price",
        "allow_negative": False,
        "allow_zero": False,
        "save": save_price_updates,
    },
    "statements": {
        "rows": "statement_rows",
        "anchor": "routine-statements-heading",
        "label": "statement value",
        "allow_negative": False,
        "allow_zero": True,
        "save": save_statement_updates,
    },
    "cash": {
        "rows": "cash_rows",
        "anchor": "routine-cash-heading",
        "label": "cash balance",
        "allow_negative": True,
        "allow_zero": True,
        "save": save_cash_updates,
    },
    "fx": {
        "rows": "fx_rows",
        "anchor": "routine-fx-heading",
        "label": "FX rate",
        "allow_negative": False,
        "allow_zero": False,
        "save": save_fx_updates,
    },
}


def _routine_identity(section: str, row: dict[str, object]):
    if section == "prices":
        return row["instrument_id"]
    if section == "statements":
        return row["registration_id"]
    if section == "cash":
        return row["account_id"], row["currency_code"]
    return row["base_currency_code"], row["quote_currency_code"]


def _routine_amounts(
    section: str, session: dict[str, object]
) -> tuple[dict[object, Decimal], dict[str, str], dict[str, str]]:
    contract = _ROUTINE_SECTIONS[section]
    updates: dict[object, Decimal] = {}
    errors: dict[str, str] = {}
    preserved: dict[str, str] = {}
    for row in session[contract["rows"]]:
        field = row["field_name"]
        raw = request.form.get(field, "")
        preserved[field] = raw
        if section == "fx":
            acknowledgement_field = f"ack_{field}"
            preserved[acknowledgement_field] = request.form.get(
                acknowledgement_field, ""
            )
            replacement_field = f"replace_{field}"
            preserved[replacement_field] = request.form.get(
                replacement_field, ""
            )
        if not raw.strip():
            continue
        try:
            amount = Decimal(raw.strip())
        except (InvalidOperation, ValueError):
            errors[field] = f"Enter a valid {contract['label']}."
            continue
        if not amount.is_finite():
            errors[field] = f"Enter a finite {contract['label']}."
        elif not contract["allow_negative"] and amount < 0:
            errors[field] = f"{contract['label'].capitalize()} must not be negative."
        elif not contract["allow_zero"] and amount == 0:
            errors[field] = f"{contract['label'].capitalize()} must be greater than zero."
        else:
            updates[_routine_identity(section, row)] = amount
    return updates, errors, preserved


def _save_routine_section(section: str) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    contract = _ROUTINE_SECTIONS[section]
    raw_date = request.form.get("effective_date", "")
    try:
        effective_date = parse_iso_date(raw_date)
    except (TypeError, ValueError):
        return _render_values(
            "maintenance",
            routine_date=_today(),
            routine_errors={"effective_date": "Enter a valid update date."},
            routine_values=dict(request.form),
        )

    form = RoutineUpdateSectionForm()
    if not form.validate_on_submit():
        return _render_values(
            "maintenance",
            routine_date=effective_date,
            routine_errors={"effective_date": "The update form could not be verified."},
            routine_values=dict(request.form),
        )

    session = build_routine_update_session(portfolio, effective_date)
    updates, errors, preserved = _routine_amounts(section, session)
    if errors:
        return _render_values(
            "maintenance",
            routine_date=effective_date,
            routine_errors=errors,
            routine_values=preserved,
        )
    if not updates:
        flash(
            f"No {contract['label']} updates were entered; existing values are unchanged.",
            "info",
        )
        return redirect(
            url_for("positions.show_values", as_of=effective_date)
            + f"#{contract['anchor']}"
        )
    try:
        if section == "fx":
            acknowledgements = frozenset(
                _routine_identity(section, row)
                for row in session[contract["rows"]]
                if request.form.get(f"ack_{row['field_name']}")
            )
            replacements = frozenset(
                _routine_identity(section, row)
                for row in session[contract["rows"]]
                if request.form.get(f"replace_{row['field_name']}")
            )
            result = save_fx_updates(
                portfolio,
                effective_date,
                updates,
                acknowledgements=acknowledgements,
                replacements=replacements,
            )
        else:
            result = contract["save"](portfolio, effective_date, updates)
    except RoutineUpdateValidationError as error:
        return _render_values(
            "maintenance",
            routine_date=effective_date,
            routine_errors=error.errors,
            routine_values=preserved,
            routine_fx_warnings=error.fx_warnings,
        )
    flash(
        f"Saved {result.saved_count} {contract['label']} "
        f"update{'s' if result.saved_count != 1 else ''} for {effective_date.isoformat()}.",
        "success",
    )
    for warning in result.warnings:
        flash(warning, "warning")
    return redirect(
        url_for("positions.show_values", as_of=effective_date)
        + f"#{contract['anchor']}"
    )


@positions_blueprint.post("/values/routine/prices")
def save_routine_prices() -> str:
    return _save_routine_section("prices")


@positions_blueprint.post("/values/routine/statements")
def save_routine_statements() -> str:
    return _save_routine_section("statements")


@positions_blueprint.post("/values/routine/cash")
def save_routine_cash() -> str:
    return _save_routine_section("cash")


@positions_blueprint.post("/values/routine/fx")
def save_routine_fx() -> str:
    return _save_routine_section("fx")


@positions_blueprint.get("/setup/values")
def show_setup_values() -> str:
    return _render_values("setup")


def _values_redirect(pathway: str):
    endpoint = (
        "positions.show_setup_values"
        if pathway == "setup"
        else "positions.show_values"
    )
    return redirect(url_for(endpoint))


def _add_price(pathway: str) -> str:
    portfolio = _current_portfolio()
    _, instruments, registrations = (
        _portfolio_records(portfolio.id) if portfolio else ([], [], [])
    )
    form, _, _ = _value_forms(instruments, registrations)
    if portfolio and form.validate_on_submit():
        instrument = next(row for row in instruments if row.id == form.instrument_id.data)
        if form.currency_code.data != instrument.valuation_currency_code:
            form.currency_code.errors.append(
                "Price currency must match the instrument valuation currency."
            )
        else:
            existing = db.session.scalar(
                select(Price).where(
                    Price.instrument_id == instrument.id,
                    Price.effective_date == form.effective_date.data,
                )
            )
            if existing is not None and not form.confirm_same_day_correction.data:
                form.confirm_same_day_correction.errors.append(
                    "Confirm replacement of the existing price for this date."
                )
            else:
                if existing is not None:
                    previous = format(existing.price_amount.normalize(), "f")
                    note = (
                        f"Corrected {existing.effective_date.isoformat()}: previous "
                        f"unit price {previous} {existing.currency_code}."
                    )
                    existing.source_note = (
                        f"{existing.source_note}\n{note}"
                        if existing.source_note
                        else note
                    )
                    existing.price_amount = form.price_amount.data
                else:
                    db.session.add(
                        Price(
                            instrument_id=instrument.id,
                            effective_date=form.effective_date.data,
                            price_amount=form.price_amount.data,
                            currency_code=form.currency_code.data,
                        )
                    )
                db.session.commit()
                flash(
                    (
                        "Price corrected; the previous value remains in the source note."
                        if existing is not None
                        else "Price saved."
                    ),
                    "success",
                )
                return _values_redirect(pathway)
    db.session.rollback()
    return _render_values_with(
        form_kind="price", submitted=form, pathway=pathway
    )


@positions_blueprint.post("/values/price")
def add_price() -> str:
    return _add_price("maintenance")


@positions_blueprint.post("/setup/values/price")
def add_setup_price() -> str:
    return _add_price("setup")


def _add_statement_value(pathway: str) -> str:
    portfolio = _current_portfolio()
    _, instruments, registrations = (
        _portfolio_records(portfolio.id) if portfolio else ([], [], [])
    )
    _, form, _ = _value_forms(instruments, registrations)
    if portfolio and form.validate_on_submit():
        try:
            record_statement_value(
                portfolio_id=portfolio.id,
                registration_id=form.registration_id.data,
                effective_date=form.effective_date.data,
                native_value_amount=form.native_value_amount.data,
                currency_code=form.currency_code.data,
            )
        except StatementValueValidationError as error:
            getattr(form, error.field).errors.append(error.message)
        else:
            flash("Statement value saved.", "success")
            return _values_redirect(pathway)
    db.session.rollback()
    return _render_values_with(
        form_kind="statement", submitted=form, pathway=pathway
    )


@positions_blueprint.post("/values/statement")
def add_statement_value() -> str:
    return _add_statement_value("maintenance")


@positions_blueprint.post("/setup/values/statement")
def add_setup_statement_value() -> str:
    return _add_statement_value("setup")


def _add_fx(pathway: str) -> str:
    portfolio = _current_portfolio()
    _, instruments, registrations = (
        _portfolio_records(portfolio.id) if portfolio else ([], [], [])
    )
    _, _, form = _value_forms(instruments, registrations)
    fx_change_warning = None
    fx_correction = None
    if portfolio and form.validate_on_submit():
        from app.services.fx_reference import latest_set
        if latest_set(form.effective_date.data):
            form.effective_date.errors.append("This date uses a reference set. Use Settings → Enter or correct rates manually; individual pairs cannot override it.")
            return _render_values_with(form_kind="fx", submitted=form, pathway=pathway)
        existing = same_day_fx_rates(
            form.base_currency_code.data,
            form.quote_currency_code.data,
            form.effective_date.data,
        )
        if existing:
            try:
                fx_correction = fx_correction_context(
                    existing,
                    base_currency=form.base_currency_code.data,
                    quote_currency=form.quote_currency_code.data,
                    quote_per_base=form.quote_per_base_amount.data,
                )
            except ValueError as error:
                form.quote_per_base_amount.errors.append(str(error))
                return _render_values_with(form_kind="fx", submitted=form, pathway=pathway)
            if not form.confirm_same_day_correction.data:
                form.confirm_same_day_correction.errors.append(
                    "Confirm replacement of the existing FX source for this date."
                )
        fx_change_warning = assess_fx_change(
            form.base_currency_code.data,
            form.quote_currency_code.data,
            form.effective_date.data,
            form.quote_per_base_amount.data,
        )
        if (
            fx_change_warning.requires_acknowledgement
            and not form.acknowledge_large_change.data
        ):
            form.acknowledge_large_change.errors.append(
                "Check the direction and value, then acknowledge this large change."
            )
        if not form.confirm_same_day_correction.errors and not (
            fx_change_warning.requires_acknowledgement
            and not form.acknowledge_large_change.data
        ):
            if fx_correction is not None:
                apply_same_day_fx_correction(fx_correction)
            else:
                db.session.add(
                    FxRate(
                        effective_date=form.effective_date.data,
                        base_currency_code=form.base_currency_code.data,
                        quote_currency_code=form.quote_currency_code.data,
                        quote_per_base_amount=form.quote_per_base_amount.data,
                    )
                )
            db.session.commit()
            flash(
                (
                    "FX rate corrected; previous values remain in the source note."
                    if fx_correction is not None
                    else "FX rate saved."
                ),
                "success",
            )
            return _values_redirect(pathway)
    db.session.rollback()
    return _render_values_with(
        form_kind="fx",
        submitted=form,
        pathway=pathway,
        fx_change_warning=fx_change_warning,
        fx_correction=fx_correction,
    )


@positions_blueprint.post("/values/fx")
def add_fx() -> str:
    return _add_fx("maintenance")


@positions_blueprint.post("/setup/values/fx")
def add_setup_fx() -> str:
    return _add_fx("setup")


def _render_values_with(
    *,
    form_kind: str,
    submitted,
    pathway: str,
    fx_change_warning=None,
    fx_correction=None,
) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    _, instruments, registrations = _portfolio_records(portfolio.id)
    price, statement, fx = _value_forms(instruments, registrations, bind_request=False)
    if form_kind == "price":
        price = submitted
    elif form_kind == "statement":
        statement = submitted
    else:
        fx = submitted
    setup = (
        _values_setup_view_model(portfolio, registrations)
        if pathway == "setup"
        else None
    )
    routine = None
    routine_form = None
    if pathway == "maintenance":
        routine = build_routine_update_session(portfolio, _today())
        routine_form = RoutineUpdateSectionForm(formdata=None)
    return render_template(
        "values/index.html", setup=setup, price_form=price,
        statement_form=statement, fx_form=fx,
        values_pathway=pathway, value_action_urls=_value_action_urls(pathway),
        value_target_context={},
        portfolio_name=portfolio.name,
        routine=routine,
        routine_form=routine_form,
        routine_date_error=None,
        routine_errors={},
        routine_values={},
        routine_fx_warnings={},
        fx_change_warning=fx_change_warning,
        fx_correction=fx_correction,
        individual_updates_open=pathway == "maintenance",
    )


_ROLE_RANK = {code: index for index, code in enumerate(ECONOMIC_ROLE_CODES)}
_BUCKET_RANK = {code: index for index, code in enumerate(FIRE_BUCKET_CODES)}


def _classification_key(row: dict[str, object]) -> tuple[object, ...]:
    """Canonical role order (as on Overview), then bucket order, then name.
    Mixed holdings rank by their highest-weight role; rows without a bucket
    sort after the bucketed rows of their role. Rows without any role are
    handled by the caller, which keeps them last in both directions."""

    weights = row["role_weights"]
    role_rank = len(_ROLE_RANK)
    if weights:
        primary = max(
            weights,
            key=lambda rw: (
                rw["weight_decimal"],
                -_ROLE_RANK.get(rw["code"], len(_ROLE_RANK)),
            ),
        )
        role_rank = _ROLE_RANK.get(primary["code"], len(_ROLE_RANK))
    bucket_rank = _BUCKET_RANK.get(row["fire_bucket_code"], len(_BUCKET_RANK))
    return (role_rank, bucket_rank, row["instrument_name"], row["account_name"])


def _sorted_holdings(
    rows: list[dict[str, object]], sort_by: str, direction: str
) -> list[dict[str, object]]:
    """Order Holdings rows for display only. Unvalued rows are never treated
    as zero, and unclassified rows never lead a classification sort: both
    always sort last, whichever direction is chosen."""

    reverse = direction == "desc"
    if sort_by == "reporting":
        valued = [row for row in rows if row["reporting_amount"] is not None]
        unvalued = [row for row in rows if row["reporting_amount"] is None]
        valued.sort(key=lambda row: row["reporting_amount"], reverse=reverse)
        return valued + unvalued
    if sort_by == "classification":
        classified = [row for row in rows if row["role_weights"]]
        unclassified = [row for row in rows if not row["role_weights"]]
        classified.sort(key=_classification_key, reverse=reverse)
        unclassified.sort(
            key=lambda row: (row["instrument_name"], row["account_name"])
        )
        return classified + unclassified
    keys = {
        "instrument": lambda row: (row["instrument_name"], row["account_name"]),
        "account": lambda row: (row["account_name"], row["instrument_name"]),
    }[sort_by]
    return sorted(rows, key=keys, reverse=reverse)


def _sorted_cash(
    rows: list[dict[str, object]], sort_by: str, direction: str
) -> list[dict[str, object]]:
    """Cash rows keep their service order unless reporting-value sorting is
    requested; unconverted rows are never treated as zero and sort last."""

    if sort_by != "reporting":
        return rows
    valued = [row for row in rows if row["reporting_amount"] is not None]
    unvalued = [row for row in rows if row["reporting_amount"] is None]
    valued.sort(key=lambda row: row["reporting_amount"], reverse=(direction == "desc"))
    return valued + unvalued


@positions_blueprint.get("/holdings")
def holdings() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    as_of = (
        parse_iso_date(request.args["as_of"])
        if request.args.get("as_of")
        else (portfolio.default_as_of_date or _today())
    )
    sort_by = request.args.get("sort", "instrument")
    if sort_by not in {"instrument", "account", "classification", "reporting"}:
        sort_by = "instrument"
    direction = request.args.get("direction", "asc")
    if direction not in {"asc", "desc"}:
        direction = "asc"
    cash_sort_by = request.args.get("cash_sort", "")
    if cash_sort_by not in {"reporting"}:
        cash_sort_by = ""
    cash_direction = request.args.get("cash_direction", "asc")
    if cash_direction not in {"asc", "desc"}:
        cash_direction = "asc"
    summary = build_portfolio_summary(portfolio, as_of)
    return render_template(
        "holdings/index.html",
        holdings=_sorted_holdings(summary["holdings"], sort_by, direction),
        cash_balances=_sorted_cash(
            summary["cash_balances"], cash_sort_by, cash_direction
        ),
        summary=summary,
        as_of_date=as_of,
        reporting_currency=portfolio.reporting_currency_code,
        portfolio_name=portfolio.name,
        sort_by=sort_by,
        sort_direction=direction,
        cash_sort_by=cash_sort_by,
        cash_direction=cash_direction,
    )


@positions_blueprint.get("/holdings.csv")
def export_holdings_csv():
    """Download current Holdings from the same as-of calculation path."""

    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    try:
        as_of = (
            parse_iso_date(request.args["as_of"])
            if request.args.get("as_of")
            else (portfolio.default_as_of_date or _today())
        )
    except (TypeError, ValueError):
        abort(400)
    content = holdings_csv(portfolio, as_of)
    filename = f"lookthrough-holdings-{as_of.isoformat()}.csv"
    return current_app.response_class(
        content,
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
