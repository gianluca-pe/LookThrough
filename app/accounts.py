"""Account cash summaries and dated confirmation routes."""

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
from sqlalchemy import func, select

from app.account_forms import AccountMaintenanceForm, EmptyAccountArchiveForm
from app.cash_forms import CashConfirmationForm
from app.conventions import parse_iso_date, utc_now
from app.extensions import db
from app.models import (
    Account,
    CashBalanceCheckpoint,
    Institution,
    PositionRegistration,
    Posting,
)
from app.services.cash import (
    CashConfirmationCommand,
    CashValidationError,
    cash_currencies,
    confirm_cash,
    resolve_cash,
)
from app.services.portfolio_summary import build_portfolio_summary
from app.services.relationships import (
    evaluate_relationship_rule,
    relationship_rule_for_institution,
)
from app.services.settlement import (
    SettlementAccountError,
    validate_cash_settlement_account,
)
from app.setup import (
    _create_account,
    _current_portfolio,
    _setup_progress,
    _setup_records,
)
from app.setup_forms import AccountSetupForm
from app.setup_view_models import build_setup_view_model


accounts_blueprint = Blueprint("accounts", __name__)


def _today() -> date:
    return current_app.config["CURRENT_DATE_PROVIDER"]()


def _as_of_date() -> date:
    return parse_iso_date(request.args["as_of"]) if request.args.get("as_of") else _today()


def _account(portfolio_id: int, account_id: int) -> Account:
    account = db.session.scalar(
        select(Account).where(
            Account.id == account_id,
            Account.portfolio_id == portfolio_id,
            Account.is_active.is_(True),
        )
    )
    if account is None:
        abort(404)
    return account


def _cash_rows(portfolio_id: int, account: Account, as_of_date: date):
    return [
        resolve_cash(portfolio_id, account.id, currency, as_of_date)
        for currency in cash_currencies(portfolio_id, account.id)
    ]


def _account_vm(
    portfolio_id: int,
    account: Account,
    as_of_date: date,
    *,
    value_summary: dict[str, object] | None = None,
) -> dict:
    return {
        "id": account.id,
        "name": account.name,
        "reference": account.reference,
        "institution_id": account.institution_id,
        "institution_name": account.institution.name,
        "account_type": account.account_type,
        "default_currency_code": account.default_currency_code,
        "cash_tracking_mode": account.cash_tracking_mode,
        "cash_settlement_account_id": account.cash_settlement_account_id,
        "cash_settlement_account_name": (
            account.cash_settlement_account.name
            if account.cash_settlement_account is not None
            else None
        ),
        "is_multicurrency": account.is_multicurrency,
        "portfolio_share_percent": account.portfolio_share_decimal * Decimal("100"),
        "present_access_percent": account.present_access_decimal * Decimal("100"),
        "earliest_access_date": account.earliest_access_date,
        "access_note": account.access_note,
        "relationship_eligible": account.relationship_eligible,
        "value_summary": value_summary,
        "cash": _cash_rows(portfolio_id, account, as_of_date),
    }


def _creation_form(institutions: list[Institution]) -> AccountSetupForm:
    form = AccountSetupForm()
    form.institution_id.choices = [
        (institution.id, institution.name) for institution in institutions
    ]
    if not form.is_submitted():
        try:
            requested_id = int(request.args.get("institution", ""))
        except ValueError:
            requested_id = None
        if requested_id in {institution.id for institution in institutions}:
            form.institution_id.data = requested_id
    return form


def _active_institutions(portfolio_id: int) -> list[Institution]:
    return list(
        db.session.scalars(
            select(Institution)
            .where(Institution.portfolio_id == portfolio_id)
            .order_by(Institution.name, Institution.id)
        )
    )


def _archive_blockers(account: Account) -> tuple[str, ...]:
    """Return source/dependency categories that make cleanup unsafe."""

    blockers: list[str] = []
    if db.session.scalar(
        select(PositionRegistration.id)
        .where(PositionRegistration.account_id == account.id)
        .limit(1)
    ) is not None:
        blockers.append("positions")
    if db.session.scalar(
        select(Posting.id).where(Posting.account_id == account.id).limit(1)
    ) is not None:
        blockers.append("activity history")
    checkpoints = list(
        db.session.scalars(
            select(CashBalanceCheckpoint).where(
                CashBalanceCheckpoint.account_id == account.id
            )
        )
    )
    if any(
        (checkpoint.confirmed_balance_amount or Decimal("0")) != 0
        or (checkpoint.prior_calculated_balance_amount or Decimal("0")) != 0
        or (checkpoint.correction_amount or Decimal("0")) != 0
        for checkpoint in checkpoints
    ):
        blockers.append("cash confirmations")
    if account.cash_settlement_account_id is not None or db.session.scalar(
        select(Account.id)
        .where(Account.cash_settlement_account_id == account.id)
        .limit(1)
    ) is not None:
        blockers.append("cash settlement routing")
    return tuple(blockers)


def _index_accounts(portfolio_id: int) -> tuple[list[Account], str, str]:
    sort_by = request.args.get("sort", "institution")
    if sort_by not in {"institution", "account", "cash"}:
        sort_by = "institution"
    direction = request.args.get("direction", "asc")
    if direction not in {"asc", "desc"}:
        direction = "asc"

    columns = {
        "institution": (Institution.name, Account.name),
        "account": (Account.name, Institution.name),
        "cash": (Account.cash_tracking_mode, Institution.name, Account.name),
    }[sort_by]
    ordered = [
        column.desc() if direction == "desc" else column.asc()
        for column in columns
    ]
    accounts = list(
        db.session.scalars(
            select(Account)
            .join(Institution)
            .where(
                Account.portfolio_id == portfolio_id,
                Account.is_active.is_(True),
            )
            .order_by(*ordered, Account.id.asc())
        )
    )
    return accounts, sort_by, direction


@accounts_blueprint.get("/accounts")
def index() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    as_of = _as_of_date()
    accounts, sort_by, direction = _index_accounts(portfolio.id)
    portfolio_summary = build_portfolio_summary(portfolio, as_of)
    summaries_by_account = {
        row["account_id"]: row
        for row in portfolio_summary["account_summaries"]
    }
    institutions = list(
        db.session.scalars(
            select(Institution)
            .where(Institution.portfolio_id == portfolio.id)
            .order_by(Institution.name, Institution.id)
        )
    )
    relationship_rows = []
    for institution in institutions:
        rule = relationship_rule_for_institution(portfolio.id, institution.id)
        relationship_rows.append(
            {
                "institution": institution,
                "rule": rule,
                "snapshot": (
                    evaluate_relationship_rule(portfolio, rule, as_of)
                    if rule is not None
                    else None
                ),
            }
        )
    return render_template(
        "accounts/index.html",
        accounts=[
            _account_vm(
                portfolio.id,
                row,
                as_of,
                value_summary=summaries_by_account[row.id],
            )
            for row in accounts
        ],
        as_of_date=as_of,
        sort_by=sort_by,
        sort_direction=direction,
        portfolio_name=portfolio.name,
        reporting_currency=portfolio.reporting_currency_code,
        relationship_rows=relationship_rows,
    )


@accounts_blueprint.route("/accounts/new", methods=["GET", "POST"])
def new() -> str:
    """Create an account in ordinary-use context, outside the setup rail."""

    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    institutions = _active_institutions(portfolio.id)
    if not institutions:
        flash("Add an institution before adding an account.", "info")
        return redirect(url_for("setup.show", step="accounts"))
    form = _creation_form(institutions)
    if form.validate_on_submit():
        duplicate = db.session.scalar(
            select(Account.id).where(
                Account.portfolio_id == portfolio.id,
                Account.institution_id == form.institution_id.data,
                Account.is_active.is_(True),
                func.lower(Account.name) == form.name.data.lower(),
            )
        )
        if duplicate is not None:
            form.name.errors.append(
                "An active account at this institution already uses this name."
            )
        else:
            account = _create_account(portfolio.id, form)
            flash(f"{account.name} added.", "success")
            return redirect(
                url_for("accounts.created", account_id=account.id)
            )
    return render_template(
        "accounts/new.html",
        form=form,
        institutions=institutions,
        portfolio_name=portfolio.name,
    )


@accounts_blueprint.get("/accounts/<int:account_id>/created")
def created(account_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    account = _account(portfolio.id, account_id)
    return render_template(
        "accounts/created.html",
        account=_account_vm(portfolio.id, account, _today()),
        portfolio_name=portfolio.name,
    )


@accounts_blueprint.get("/accounts/<int:account_id>")
def detail(account_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    account = _account(portfolio.id, account_id)
    as_of = _as_of_date()
    portfolio_summary = build_portfolio_summary(portfolio, as_of)
    value_summary = next(
        row
        for row in portfolio_summary["account_summaries"]
        if row["account_id"] == account.id
    )
    relationship_rule = relationship_rule_for_institution(
        portfolio.id, account.institution_id
    )
    return render_template(
        "accounts/detail.html",
        account=_account_vm(
            portfolio.id,
            account,
            as_of,
            value_summary=value_summary,
        ),
        as_of_date=as_of,
        portfolio_name=portfolio.name,
        reporting_currency=portfolio.reporting_currency_code,
        relationship_snapshot=(
            evaluate_relationship_rule(portfolio, relationship_rule, as_of)
            if relationship_rule is not None
            else None
        ),
        archive_blockers=_archive_blockers(account),
    )


@accounts_blueprint.route(
    "/accounts/<int:account_id>/archive", methods=["GET", "POST"]
)
def archive(account_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    account = _account(portfolio.id, account_id)
    form = EmptyAccountArchiveForm()
    blockers = _archive_blockers(account)
    if form.validate_on_submit():
        # Re-resolve immediately before the write so a stale confirmation page
        # can never archive an account that acquired financial history.
        blockers = _archive_blockers(account)
        if not blockers:
            account.is_active = False
            db.session.commit()
            flash(f"{account.name} archived.", "success")
            return redirect(url_for("accounts.index"))
    return render_template(
        "accounts/archive.html",
        account={
            "id": account.id,
            "name": account.name,
            "institution_name": account.institution.name,
        },
        blockers=blockers,
        form=form,
        portfolio_name=portfolio.name,
    )


def _settlement_choices(account: Account) -> list[tuple[int, str]]:
    candidates = list(
        db.session.scalars(
            select(Account)
            .where(
                Account.portfolio_id == account.portfolio_id,
                Account.institution_id == account.institution_id,
                Account.id != account.id,
                Account.is_active.is_(True),
                Account.is_multicurrency.is_(False),
                Account.default_currency_code == account.default_currency_code,
                Account.cash_tracking_mode == "separate_cash",
                Account.cash_settlement_account_id.is_(None),
            )
            .order_by(Account.name, Account.id)
        )
    )
    return [(0, "Cash stays in this account")] + [
        (row.id, f"{row.name} ({row.default_currency_code})") for row in candidates
    ]


def _maintenance_form(account: Account) -> AccountMaintenanceForm:
    if request.method == "POST":
        form = AccountMaintenanceForm()
        if "cash_settlement_account_id" not in request.form:
            # An omitted settlement control preserves the relationship.
            # An explicitly submitted 0 clears it.
            form.cash_settlement_account_id.data = (
                account.cash_settlement_account_id or 0
            )
        # Omitted factors preserve existing values. Explicit blank required fields
        # are errors; blank optional access context clears those values.
        legacy_values = {
            "portfolio_share_percent": (
                account.portfolio_share_decimal * Decimal("100")
            ),
            "present_access_percent": (
                account.present_access_decimal * Decimal("100")
            ),
            "earliest_access_date": account.earliest_access_date,
            "access_note": account.access_note,
        }
        for field_name, value in legacy_values.items():
            if field_name not in request.form:
                getattr(form, field_name).data = value
    else:
        form = AccountMaintenanceForm(
            data={
                "name": account.name,
                "reference": account.reference,
                "cash_tracking_mode": account.cash_tracking_mode,
                "cash_settlement_account_id": (
                    account.cash_settlement_account_id or 0
                ),
                "portfolio_share_percent": (
                    account.portfolio_share_decimal * Decimal("100")
                ),
                "present_access_percent": (
                    account.present_access_decimal * Decimal("100")
                ),
                "earliest_access_date": account.earliest_access_date,
                "access_note": account.access_note,
                "currency_code": account.default_currency_code,
                "effective_date": _today(),
            }
        )
    form.cash_settlement_account_id.choices = _settlement_choices(account)
    return form


def _requires_fresh_confirmation(
    account: Account, form: AccountMaintenanceForm
) -> bool:
    return (
        account.cash_tracking_mode == "included_in_aggregate"
        and form.cash_tracking_mode.data == "separate_cash"
    )


def _validate_maintenance(
    portfolio_id: int, account: Account, form: AccountMaintenanceForm
) -> bool:
    valid = form.validate_on_submit()
    duplicate = db.session.scalar(
        select(Account.id).where(
            Account.portfolio_id == portfolio_id,
            Account.institution_id == account.institution_id,
            Account.id != account.id,
            Account.is_active.is_(True),
            func.lower(Account.name) == form.name.data.lower(),
        )
    ) if form.name.data else None
    if duplicate is not None:
        form.name.errors.append(
            "Another active account at this institution already uses this name."
        )
        valid = False

    target_id = form.cash_settlement_account_id.data or 0
    target = db.session.get(Account, target_id) if target_id else None
    if target is not None and form.cash_tracking_mode.data != "separate_cash":
        form.cash_settlement_account_id.errors.append(
            "Use separate cash tracking when activity cash settles in another account."
        )
        valid = False
    elif target is not None:
        original_mode = account.cash_tracking_mode
        account.cash_tracking_mode = form.cash_tracking_mode.data
        try:
            validate_cash_settlement_account(account, target)
        except SettlementAccountError as error:
            form.cash_settlement_account_id.errors.append(str(error))
            valid = False
        finally:
            account.cash_tracking_mode = original_mode

    has_incoming_links = db.session.scalar(
        select(Account.id).where(Account.cash_settlement_account_id == account.id).limit(1)
    ) is not None
    if has_incoming_links and form.cash_tracking_mode.data != "separate_cash":
        form.cash_tracking_mode.errors.append(
            "This account receives activity cash from another account and must "
            "keep separate cash tracking."
        )
        valid = False
    if has_incoming_links and target is not None:
        form.cash_settlement_account_id.errors.append(
            "This account already receives activity cash and cannot route it again."
        )
        valid = False

    if _requires_fresh_confirmation(account, form):
        required = (
            (form.currency_code, "Fresh cash currency is required."),
            (form.confirmed_balance_amount, "Fresh confirmed balance is required."),
            (form.effective_date, "Confirmation date is required."),
        )
        for field, message in required:
            if field.data is None or field.data == "":
                field.errors.append(message)
                valid = False
    return valid


def _render_maintenance(
    portfolio_id: int, account: Account, form: AccountMaintenanceForm
) -> str:
    return render_template(
        "accounts/edit.html",
        account=_account_vm(portfolio_id, account, _today()),
        form=form,
        requires_fresh_confirmation=(
            account.cash_tracking_mode == "included_in_aggregate"
        ),
        portfolio_name=account.portfolio.name,
    )


@accounts_blueprint.route("/accounts/<int:account_id>/edit", methods=["GET", "POST"])
def edit(account_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    account = _account(portfolio.id, account_id)
    form = _maintenance_form(account)
    if request.method == "GET":
        return _render_maintenance(portfolio.id, account, form)
    if not _validate_maintenance(portfolio.id, account, form):
        return _render_maintenance(portfolio.id, account, form)

    old_mode = account.cash_tracking_mode
    account.name = form.name.data
    account.reference = form.reference.data or None
    account.cash_tracking_mode = form.cash_tracking_mode.data
    account.cash_settlement_account_id = (
        form.cash_settlement_account_id.data or None
    )
    account.portfolio_share_decimal = (
        form.portfolio_share_percent.data / Decimal("100")
    )
    account.present_access_decimal = (
        form.present_access_percent.data / Decimal("100")
    )
    account.earliest_access_date = form.earliest_access_date.data
    account.access_note = form.access_note.data or None

    if old_mode == "included_in_aggregate" and account.cash_tracking_mode == "separate_cash":
        changed_at = utc_now()
        old_checkpoints = list(
            db.session.scalars(
                select(CashBalanceCheckpoint).where(
                    CashBalanceCheckpoint.account_id == account.id,
                    CashBalanceCheckpoint.superseded_at.is_(None),
                )
            )
        )
        for checkpoint in old_checkpoints:
            checkpoint.superseded_at = changed_at
        db.session.flush()
        try:
            result = confirm_cash(
                CashConfirmationCommand(
                    portfolio_id=portfolio.id,
                    account_id=account.id,
                    currency_code=form.currency_code.data,
                    effective_date=form.effective_date.data,
                    confirmed_balance_amount=form.confirmed_balance_amount.data,
                    source_note=form.source_note.data,
                )
            )
        except CashValidationError as error:
            db.session.rollback()
            field = getattr(form, error.field, form.currency_code)
            field.errors.append(error.message)
            account = _account(portfolio.id, account_id)
            return _render_maintenance(portfolio.id, account, form)
        for warning in result.warnings:
            flash(warning, "warning")
        flash(
            "Account saved with a fresh cash confirmation. Older confirmations "
            "remain in the audit trail but cannot reappear as current cash.",
            "success",
        )
    else:
        db.session.commit()
        if old_mode == "separate_cash" and account.cash_tracking_mode == "included_in_aggregate":
            flash(
                "Account saved. Existing cash confirmations are preserved but no "
                "longer count while cash is included in the aggregate value.",
                "success",
            )
        else:
            flash("Account saved.", "success")
    return redirect(url_for("accounts.detail", account_id=account.id))


@accounts_blueprint.get("/setup/cash")
def setup_cash() -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    institutions, accounts = _setup_records(portfolio)
    position_count, values_started, cash_complete = _setup_progress(portfolio)
    setup = build_setup_view_model(
        current_step="cash",
        portfolio=portfolio,
        institutions=institutions,
        accounts=accounts,
        position_count=position_count,
        values_started=values_started,
        cash_complete=cash_complete,
    )
    as_of = _as_of_date()
    return render_template(
        "setup/cash.html",
        setup=setup,
        accounts=[_account_vm(portfolio.id, row, as_of) for row in accounts],
        as_of_date=as_of,
        portfolio_name=portfolio.name,
    )


def _confirmation_form(account: Account) -> CashConfirmationForm:
    form = CashConfirmationForm()
    if not form.is_submitted():
        form.currency_code.data = request.args.get(
            "currency", account.default_currency_code
        )
        form.effective_date.data = _today()
        form.return_to.data = request.args.get("return_to")
    return form


def _render_confirmation(account: Account, form: CashConfirmationForm) -> str:
    portfolio = _current_portfolio()
    assert portfolio is not None
    currency = (
        form.currency_code.data
        if isinstance(form.currency_code.data, str) and len(form.currency_code.data) == 3
        else account.default_currency_code
    )
    try:
        calculated = resolve_cash(
            portfolio.id,
            account.id,
            currency,
            form.effective_date.data or _today(),
        )
    except CashValidationError:
        calculated = resolve_cash(
            portfolio.id,
            account.id,
            account.default_currency_code,
            form.effective_date.data or _today(),
        )
    return render_template(
        "accounts/cash_confirmation.html",
        account={
            "id": account.id,
            "name": account.name,
            "institution_name": account.institution.name,
            "cash_tracking_mode": account.cash_tracking_mode,
        },
        form=form,
        calculated=calculated,
        portfolio_name=portfolio.name,
    )


@accounts_blueprint.get("/accounts/<int:account_id>/cash-confirmations/new")
def new_cash_confirmation(account_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    account = _account(portfolio.id, account_id)
    if account.cash_tracking_mode != "separate_cash":
        flash(
            "Cash is included in this account's aggregate value; a separate "
            "confirmation would count it twice.",
            "warning",
        )
        return redirect(url_for("accounts.detail", account_id=account.id))
    return _render_confirmation(account, _confirmation_form(account))


@accounts_blueprint.post("/accounts/<int:account_id>/cash-confirmations")
def add_cash_confirmation(account_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    account = _account(portfolio.id, account_id)
    form = _confirmation_form(account)
    if form.validate_on_submit():
        try:
            result = confirm_cash(
                CashConfirmationCommand(
                    portfolio_id=portfolio.id,
                    account_id=account.id,
                    currency_code=form.currency_code.data,
                    effective_date=form.effective_date.data,
                    confirmed_balance_amount=form.confirmed_balance_amount.data,
                    source_note=form.source_note.data,
                )
            )
        except CashValidationError as error:
            field = getattr(form, error.field, form.currency_code)
            field.errors.append(error.message)
        else:
            flash(
                "Cash confirmation saved. Its correction is reconciliation only; "
                "no deposit, withdrawal, income, or expense was created.",
                "success",
            )
            for warning in result.warnings:
                flash(warning, "warning")
            if form.return_to.data == "setup_accounts":
                return redirect(url_for("setup.show", step="accounts"))
            return redirect(
                url_for(
                    "accounts.detail",
                    account_id=account.id,
                    currency=form.currency_code.data,
                )
            )
    return _render_confirmation(account, form)
