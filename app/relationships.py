"""Institution relationship-minimum routes."""

from __future__ import annotations

from datetime import date

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

from app.conventions import parse_iso_date
from app.extensions import db
from app.models import Account, Institution
from app.relationship_forms import RelationshipRuleForm
from app.services.relationships import (
    RelationshipRuleCommand,
    RelationshipValidationError,
    evaluate_relationship_rule,
    relationship_rule_for_institution,
    save_relationship_rule,
)
from app.setup import _current_portfolio


relationships_blueprint = Blueprint("relationships", __name__)


def _today() -> date:
    return current_app.config["CURRENT_DATE_PROVIDER"]()


def _as_of_date() -> date:
    return (
        parse_iso_date(request.args["as_of"])
        if request.args.get("as_of")
        else _today()
    )


def _institution(portfolio_id: int, institution_id: int) -> Institution:
    institution = db.session.scalar(
        select(Institution).where(
            Institution.id == institution_id,
            Institution.portfolio_id == portfolio_id,
        )
    )
    if institution is None:
        abort(404)
    return institution


def _active_accounts(portfolio_id: int, institution_id: int) -> list[Account]:
    return list(
        db.session.scalars(
            select(Account)
            .where(
                Account.portfolio_id == portfolio_id,
                Account.institution_id == institution_id,
                Account.is_active.is_(True),
            )
            .order_by(Account.name, Account.id)
        )
    )


def _relationship_form(
    portfolio_id: int,
    institution: Institution,
    accounts: list[Account],
) -> RelationshipRuleForm:
    rule = relationship_rule_for_institution(
        portfolio_id, institution.id, active_only=False
    )
    if request.method == "POST":
        form = RelationshipRuleForm()
    elif rule is None:
        form = RelationshipRuleForm(
            data={
                "name": f"{institution.name} relationship minimum",
                "threshold_currency_code": institution.portfolio.reporting_currency_code,
                "eligible_account_ids": [
                    account.id for account in accounts if account.relationship_eligible
                ],
                "is_active": True,
            }
        )
    else:
        form = RelationshipRuleForm(
            data={
                "name": rule.name,
                "threshold_amount": rule.threshold_amount,
                "threshold_currency_code": rule.threshold_currency_code,
                "warning_buffer_amount": rule.warning_buffer_amount,
                "eligible_account_ids": [
                    account.id for account in accounts if account.relationship_eligible
                ],
                "is_active": rule.is_active,
                "notes": rule.notes,
            }
        )
    form.eligible_account_ids.choices = [
        (account.id, account.name) for account in accounts
    ]
    return form


def _render(
    institution: Institution,
    accounts: list[Account],
    form: RelationshipRuleForm,
    as_of_date: date,
) -> str:
    rule = relationship_rule_for_institution(
        institution.portfolio_id, institution.id, active_only=False
    )
    snapshot = (
        evaluate_relationship_rule(institution.portfolio, rule, as_of_date)
        if rule is not None and rule.is_active
        else None
    )
    return render_template(
        "relationships/edit.html",
        institution=institution,
        accounts=accounts,
        form=form,
        rule=rule,
        snapshot=snapshot,
        as_of_date=as_of_date,
        portfolio_name=institution.portfolio.name,
    )


@relationships_blueprint.route(
    "/institutions/<int:institution_id>/relationship", methods=["GET", "POST"]
)
def edit(institution_id: int) -> str:
    portfolio = _current_portfolio()
    if portfolio is None:
        return redirect(url_for("setup.show"))
    institution = _institution(portfolio.id, institution_id)
    accounts = _active_accounts(portfolio.id, institution.id)
    form = _relationship_form(portfolio.id, institution, accounts)
    as_of = _as_of_date()
    if request.method == "GET":
        return _render(institution, accounts, form, as_of)

    valid = form.validate_on_submit()
    if form.is_active.data and not form.eligible_account_ids.data:
        form.eligible_account_ids.errors.append(
            "Choose at least one eligible account while the minimum is active."
        )
        valid = False
    if not valid:
        return _render(institution, accounts, form, as_of)

    try:
        save_relationship_rule(
            portfolio.id,
            RelationshipRuleCommand(
                institution_id=institution.id,
                name=form.name.data,
                threshold_amount=form.threshold_amount.data,
                threshold_currency_code=form.threshold_currency_code.data,
                warning_buffer_amount=form.warning_buffer_amount.data,
                is_active=form.is_active.data,
                eligible_account_ids=tuple(form.eligible_account_ids.data),
                notes=form.notes.data,
            ),
        )
    except RelationshipValidationError as error:
        field = getattr(form, error.field, form.name)
        field.errors.append(error.message)
        db.session.rollback()
        return _render(institution, accounts, form, as_of)
    db.session.commit()
    flash("Relationship minimum saved.", "success")
    return redirect(
        url_for(
            "relationships.edit",
            institution_id=institution.id,
            as_of=as_of.isoformat(),
        )
    )
