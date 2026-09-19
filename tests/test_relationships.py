"""Relationship-minimum service and route contract."""

from datetime import date
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient

from app.extensions import db
from app.models import (
    Account,
    FxRate,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    RelationshipRule,
    ValuationObservation,
)
from app.services.cash import CashConfirmationCommand, confirm_cash
from app.services.relationships import (
    RelationshipRuleCommand,
    RelationshipValidationError,
    evaluate_relationship_rule,
    relationship_attention_item,
    save_relationship_rule,
)


AS_OF = date(2026, 8, 28)


def _records(app: Flask) -> tuple[Portfolio, Institution, Account, Account]:
    portfolio = Portfolio(
        name="Portfolio",
        reporting_currency_code="EUR",
        annual_spending_amount=Decimal("48000"),
        annual_spending_currency_code="EUR",
        default_as_of_date=AS_OF,
        fx_stale_days=7,
    )
    db.session.add(portfolio)
    db.session.flush()
    institution = Institution(portfolio_id=portfolio.id, name="Example Bank")
    db.session.add(institution)
    db.session.flush()
    eligible = Account(
        portfolio_id=portfolio.id,
        institution_id=institution.id,
        name="Eligible EUR account",
        account_type="cash",
        default_currency_code="EUR",
        is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal("0.5"),
        present_access_decimal=Decimal("0.2"),
        relationship_eligible=True,
        is_active=True,
    )
    excluded = Account(
        portfolio_id=portfolio.id,
        institution_id=institution.id,
        name="Excluded EUR account",
        account_type="cash",
        default_currency_code="EUR",
        is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        relationship_eligible=False,
        is_active=True,
    )
    db.session.add_all([eligible, excluded])
    db.session.flush()
    confirm_cash(
        CashConfirmationCommand(
            portfolio_id=portfolio.id,
            account_id=eligible.id,
            currency_code="EUR",
            effective_date=date(2026, 8, 27),
            confirmed_balance_amount=Decimal("100000"),
        )
    )
    confirm_cash(
        CashConfirmationCommand(
            portfolio_id=portfolio.id,
            account_id=excluded.id,
            currency_code="EUR",
            effective_date=date(2026, 8, 27),
            confirmed_balance_amount=Decimal("50000"),
        )
    )
    db.session.add(
        FxRate(
            effective_date=date(2026, 8, 27),
            base_currency_code="EUR",
            quote_currency_code="GBP",
            quote_per_base_amount=Decimal("0.85573"),
        )
    )
    db.session.commit()
    return portfolio, institution, eligible, excluded


def _command(
    institution: Institution,
    eligible: Account,
    **changes,
) -> RelationshipRuleCommand:
    values = {
        "institution_id": institution.id,
        "name": "Premier relationship",
        "threshold_amount": Decimal("75000"),
        "threshold_currency_code": "GBP",
        "warning_buffer_amount": Decimal("12000"),
        "is_active": True,
        "eligible_account_ids": (eligible.id,),
        "notes": "User-entered commercial minimum",
    }
    values.update(changes)
    return RelationshipRuleCommand(**values)


def test_relationship_uses_gross_eligible_value_and_shared_fx_path(app: Flask) -> None:
    with app.app_context():
        portfolio, institution, eligible, excluded = _records(app)
        rule = save_relationship_rule(portfolio.id, _command(institution, eligible))
        db.session.commit()

        snapshot = evaluate_relationship_rule(portfolio, rule, AS_OF)

        assert snapshot.status == "current"
        assert snapshot.eligible_value_amount == Decimal("85573.00000")
        assert snapshot.buffer_amount == Decimal("10573.00000")
        assert snapshot.buffer_percent == Decimal("10573.00000") / Decimal("75000")
        assert snapshot.state == "within_warning_buffer"
        assert snapshot.preferred_target_amount == Decimal("87000")
        assert snapshot.account_rows[0]["account_id"] == eligible.id
        assert snapshot.account_rows[0]["amount"] == Decimal("85573.00000")
        assert excluded.relationship_eligible is False
        # Share and access would yield GBP 8,557.30. They deliberately do not
        # change the commercial relationship balance.
        assert snapshot.eligible_value_amount != Decimal("8557.300000")
        attention = relationship_attention_item(snapshot)
        assert attention["state"] == "within_warning_buffer"
        assert attention["action_kind"] == "edit_relationship_minimum"


def test_below_minimum_and_met_states_are_derived_not_stored(app: Flask) -> None:
    with app.app_context():
        portfolio, institution, eligible, _ = _records(app)
        below_rule = save_relationship_rule(
            portfolio.id,
            _command(
                institution,
                eligible,
                threshold_amount=Decimal("90000"),
                warning_buffer_amount=None,
            ),
        )
        db.session.commit()
        below = evaluate_relationship_rule(portfolio, below_rule, AS_OF)
        assert below.state == "below_minimum"
        assert below.buffer_amount == Decimal("-4427.00000")

        below_rule.threshold_amount = Decimal("75000")
        db.session.commit()
        met = evaluate_relationship_rule(portfolio, below_rule, AS_OF)
        assert met.state == "minimum_met"
        assert relationship_attention_item(met) is None


def test_missing_eligible_fx_is_cannot_determine_not_below(app: Flask) -> None:
    with app.app_context():
        portfolio, institution, eligible, _ = _records(app)
        missing = Account(
            portfolio_id=portfolio.id,
            institution_id=institution.id,
            name="USD account without GBP FX",
            account_type="cash",
            default_currency_code="USD",
            is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add(missing)
        db.session.flush()
        confirm_cash(
            CashConfirmationCommand(
                portfolio_id=portfolio.id,
                account_id=missing.id,
                currency_code="USD",
                effective_date=date(2026, 8, 27),
                confirmed_balance_amount=Decimal("1000"),
            )
        )
        rule = save_relationship_rule(
            portfolio.id,
            _command(
                institution,
                eligible,
                eligible_account_ids=(eligible.id, missing.id),
            ),
        )
        db.session.commit()

        snapshot = evaluate_relationship_rule(portfolio, rule, AS_OF)
        assert snapshot.status == "cannot_determine"
        assert snapshot.state == "cannot_determine"
        assert snapshot.eligible_value_amount is None
        assert snapshot.buffer_amount is None
        assert snapshot.known_eligible_value_amount == Decimal("85573.00000")
        assert snapshot.missing_account_ids == (missing.id,)
        assert len(snapshot.value_dependencies) == 1
        dependency = snapshot.value_dependencies[0]
        assert dependency["status"] == "missing"
        assert dependency["source"] == "fx"
        assert dependency["base_currency"] == "USD"
        assert dependency["quote_currency"] == "GBP"
        assert dependency["affected_account_ids"] == (missing.id,)
        assert relationship_attention_item(snapshot)["kind"] == "missing"


def test_stale_fx_calculates_but_remains_explicit(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, institution, eligible, _ = _records(app)
        fx = db.session.query(FxRate).one()
        fx.effective_date = date(2026, 8, 1)
        rule = save_relationship_rule(portfolio.id, _command(institution, eligible))
        db.session.commit()

        snapshot = evaluate_relationship_rule(portfolio, rule, AS_OF)
        assert snapshot.status == "stale"
        assert snapshot.eligible_value_amount == Decimal("85573.00000")
        assert snapshot.stale_account_ids == (eligible.id,)
        assert len(snapshot.value_dependencies) == 1
        dependency = snapshot.value_dependencies[0]
        assert dependency["source"] == "fx"
        assert dependency["effective_date"] == date(2026, 8, 1)
        assert relationship_attention_item(snapshot)["kind"] == "stale"
        institution_id = institution.id

    body = client.get(
        f"/institutions/{institution_id}/relationship?as_of=2026-08-28"
    ).get_data(as_text=True)
    assert "separate GBP relationship view" in body
    assert "Stale relationship FX" in body
    assert "EUR → GBP" in body
    assert "source 1 Aug 2026" in body
    assert 'href="/values?base=EUR&amp;quote=GBP#fx-heading"' in body


def test_save_validates_scope_amounts_and_active_eligibility(app: Flask) -> None:
    with app.app_context():
        portfolio, institution, eligible, excluded = _records(app)
        cases = [
            (
                _command(institution, eligible, threshold_amount=Decimal("0")),
                "threshold_amount",
            ),
            (
                _command(
                    institution,
                    eligible,
                    warning_buffer_amount=Decimal("-1"),
                ),
                "warning_buffer_amount",
            ),
            (
                _command(institution, eligible, eligible_account_ids=()),
                "eligible_account_ids",
            ),
            (
                _command(institution, eligible, eligible_account_ids=(9999,)),
                "eligible_account_ids",
            ),
        ]
        for command, field in cases:
            try:
                save_relationship_rule(portfolio.id, command)
            except RelationshipValidationError as exc:
                assert exc.field == field
                db.session.rollback()
            else:
                raise AssertionError(f"Expected validation error for {field}")
        assert db.session.query(RelationshipRule).count() == 0
        assert db.session.get(Account, eligible.id).relationship_eligible is True
        assert db.session.get(Account, excluded.id).relationship_eligible is False


def test_relationship_route_saves_and_round_trips_accounts(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, institution, eligible, excluded = _records(app)
        institution_id = institution.id
        eligible_id = eligible.id
        excluded_id = excluded.id

    page = client.get(f"/institutions/{institution_id}/relationship")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Relationship minimum" in body
    assert f'id="eligible-account-{eligible_id}"' in body
    assert "contractual calculation" in body
    assert "<script>" not in body

    response = client.post(
        f"/institutions/{institution_id}/relationship?as_of=2026-08-28",
        data={
            "name": "Premier relationship",
            "threshold_amount": "75000",
            "threshold_currency_code": "gbp",
            "warning_buffer_amount": "12000",
            "eligible_account_ids": str(eligible_id),
            "is_active": "y",
            "notes": "Bank tariff",
            "save_rule": "Save relationship minimum",
        },
    )
    assert response.status_code == 302
    assert "as_of=2026-08-28" in response.headers["Location"]
    result = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "GBP</span> 85,573.00" in result
    assert "GBP</span> 10,573.00" in result
    assert "Inside preferred buffer" in result

    with app.app_context():
        rule = db.session.query(RelationshipRule).one()
        assert rule.threshold_currency_code == "GBP"
        assert rule.warning_buffer_amount == Decimal("12000")
        assert db.session.get(Account, eligible_id).relationship_eligible is True
        assert db.session.get(Account, excluded_id).relationship_eligible is False


def test_active_route_requires_an_eligible_account_and_is_atomic(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, institution, eligible, _ = _records(app)
        institution_id = institution.id
        eligible_id = eligible.id

    response = client.post(
        f"/institutions/{institution_id}/relationship",
        data={
            "name": "Premier relationship",
            "threshold_amount": "75000",
            "threshold_currency_code": "GBP",
            "is_active": "y",
            "save_rule": "Save relationship minimum",
        },
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Choose at least one eligible account" in body
    assert 'href="#eligible_account_ids"' in body
    with app.app_context():
        assert db.session.query(RelationshipRule).count() == 0
        assert db.session.get(Account, eligible_id).relationship_eligible is True


def test_overview_receives_structured_relationship_attention(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, institution, eligible, excluded = _records(app)
        rule = save_relationship_rule(portfolio.id, _command(institution, eligible))
        instrument = Instrument(
            portfolio_id=portfolio.id,
            name="Excluded statement position",
            instrument_type="other",
            valuation_currency_code="EUR",
            is_active=True,
        )
        db.session.add(instrument)
        db.session.flush()
        registration = PositionRegistration(
            account_id=excluded.id,
            instrument_id=instrument.id,
            tracking_mode="statement_valued",
            opening_date=date(2026, 8, 27),
        )
        db.session.add(registration)
        db.session.flush()
        db.session.add(
            ValuationObservation(
                position_registration_id=registration.id,
                effective_date=date(2026, 8, 27),
                native_value_amount=Decimal("1"),
                currency_code="EUR",
            )
        )
        db.session.commit()
        institution_id = institution.id
        assert rule.id is not None

    response = client.get("/overview?as_of=2026-08-28")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Relationship minimum" in body
    assert "minimum is met, but the eligible value is inside" in body
    assert (
        f'href="/institutions/{institution_id}/relationship?as_of=2026-08-28"'
        in body
    )


def test_relationship_route_rejects_another_portfolio(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _records(app)
    assert client.get("/institutions/999/relationship").status_code == 404
