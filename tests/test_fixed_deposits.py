"""Fixed-deposit terms, maturity, access, and route contract."""

import re

from datetime import date
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient

from app.extensions import db
from app.models import (
    Account,
    FixedDeposit,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    ValuationObservation,
)
from app.services.fixed_deposits import (
    FixedDepositCommand,
    FixedDepositValidationError,
    maturity_snapshot,
    save_fixed_deposit_terms,
)
from app.services.portfolio_summary import build_portfolio_summary
from app.services.valuation import value_position


def _fd_position(
    *,
    share: str = "1",
    access: str = "1",
    instrument_type: str = "fixed_deposit",
) -> tuple[Portfolio, PositionRegistration]:
    portfolio = Portfolio(
        name="Portfolio",
        reporting_currency_code="USD",
        annual_spending_amount=Decimal("48000"),
        annual_spending_currency_code="USD",
        default_as_of_date=date(2026, 8, 28),
    )
    db.session.add(portfolio)
    db.session.flush()
    institution = Institution(portfolio_id=portfolio.id, name="Bank")
    db.session.add(institution)
    db.session.flush()
    account = Account(
        portfolio_id=portfolio.id,
        institution_id=institution.id,
        name="Fixed Deposits",
        account_type="deposit",
        default_currency_code="USD",
        is_multicurrency=False,
        cash_tracking_mode="included_in_aggregate",
        portfolio_share_decimal=Decimal(share),
        present_access_decimal=Decimal(access),
        relationship_eligible=True,
        is_active=True,
    )
    db.session.add(account)
    db.session.flush()
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name="FD_USD_204" if instrument_type == "fixed_deposit" else "Bond",
        instrument_type=instrument_type,
        valuation_currency_code="USD",
        is_active=True,
    )
    db.session.add(instrument)
    db.session.flush()
    registration = PositionRegistration(
        account_id=account.id,
        instrument_id=instrument.id,
        tracking_mode="statement_valued",
        opening_date=date(2026, 6, 2),
    )
    db.session.add(registration)
    db.session.flush()
    db.session.add(
        ValuationObservation(
            position_registration_id=registration.id,
            effective_date=date(2026, 6, 2),
            native_value_amount=Decimal("85000"),
            currency_code="USD",
        )
    )
    db.session.commit()
    return portfolio, registration


def _save_terms(
    portfolio: Portfolio,
    registration: PositionRegistration,
    *,
    maturity: date = date(2026, 9, 2),
    action: str = "undecided",
    rate: Decimal | None = Decimal("0.0325"),
) -> FixedDeposit:
    terms = save_fixed_deposit_terms(
        portfolio.id,
        FixedDepositCommand(
            registration_id=registration.id,
            currency_code="USD",
            start_date=date(2026, 6, 2),
            maturity_date=maturity,
            annual_rate_decimal=rate,
            maturity_action=action,
            notes="Confirmation 204",
        ),
    )
    db.session.commit()
    return terms


def test_maturity_state_is_as_of_derived_and_rate_can_be_missing(app: Flask) -> None:
    with app.app_context():
        portfolio, registration = _fd_position()
        terms = _save_terms(portfolio, registration, rate=None)

        active = maturity_snapshot(registration, terms, date(2026, 8, 1))
        approaching = maturity_snapshot(registration, terms, date(2026, 8, 28))
        due = maturity_snapshot(registration, terms, date(2026, 9, 2))
        overdue = maturity_snapshot(registration, terms, date(2026, 9, 3))

        assert active.maturity_status == "active"
        assert active.is_term_liquidity is True
        assert approaching.maturity_status == "approaching"
        assert approaching.days_to_maturity == 5
        assert approaching.requires_attention is True
        assert approaching.annual_rate_decimal is None
        assert due.maturity_status == "due"
        assert due.is_term_liquidity is False
        assert overdue.maturity_status == "overdue"
        assert overdue.days_to_maturity == -1


def test_terms_validation_blocks_second_principal_source_and_invalid_dates(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, registration = _fd_position()
        base = dict(
            registration_id=registration.id,
            currency_code="USD",
            start_date=date(2026, 6, 2),
            maturity_date=date(2026, 9, 2),
            annual_rate_decimal=Decimal("0.03"),
            maturity_action="return_to_cash",
        )
        try:
            save_fixed_deposit_terms(
                portfolio.id,
                FixedDepositCommand(**{**base, "currency_code": "EUR"}),
            )
        except FixedDepositValidationError as exc:
            assert exc.field == "currency_code"
        else:
            raise AssertionError("Currency mismatch should fail")

        try:
            save_fixed_deposit_terms(
                portfolio.id,
                FixedDepositCommand(
                    **{**base, "maturity_date": date(2026, 6, 2)}
                ),
            )
        except FixedDepositValidationError as exc:
            assert exc.field == "maturity_date"
        else:
            raise AssertionError("Non-positive term should fail")

        assert db.session.query(FixedDeposit).count() == 0
        observation = db.session.query(ValuationObservation).one()
        assert observation.native_value_amount == Decimal("85000")


def test_fixed_deposit_statement_age_is_not_stale_but_fx_still_can_be(
    app: Flask,
) -> None:
    with app.app_context():
        _, registration = _fd_position()
        valued = value_position(
            registration,
            date(2026, 8, 28),
            reporting_currency="USD",
            price_stale_days=14,
            fx_stale_days=7,
            statement_stale_days=45,
        )

        assert valued.native_amount == Decimal("85000")
        assert valued.status == "current"
        assert valued.stale_sources == ()
        assert valued.value_date == date(2026, 6, 2)


def test_summary_layers_share_access_and_term_liquidity_without_double_counting(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, registration = _fd_position(share="0.8", access="0.5")
        _save_terms(portfolio, registration)

        before = build_portfolio_summary(portfolio, date(2026, 8, 28))
        holding = before["holdings"][0]
        assert holding["reporting_amount"] == Decimal("85000")
        assert holding["included_reporting_amount"] == Decimal("68000.0")
        assert holding["term_liquidity_reporting_amount"] == Decimal("34000.00")
        assert holding["accessible_reporting_amount"] == Decimal("0")
        assert holding["restricted_reporting_amount"] == Decimal("34000.00")
        assert holding["excluded_by_share_reporting_amount"] == Decimal("17000.0")
        assert before["term_liquidity_reporting_amount"] == Decimal("34000.00")
        assert before["accessible_reporting_amount"] == Decimal("0")
        assert before["included_reporting_amount"] == Decimal("68000.0")
        assert before["actionable_attention_items"][0]["maturity_status"] == "approaching"

        on_maturity = build_portfolio_summary(portfolio, date(2026, 9, 2))
        matured_holding = on_maturity["holdings"][0]
        assert matured_holding["term_liquidity_reporting_amount"] == Decimal("0")
        assert matured_holding["accessible_reporting_amount"] == Decimal("34000.00")
        assert on_maturity["term_liquidity_reporting_amount"] == Decimal("0")
        assert on_maturity["accessible_reporting_amount"] == Decimal("34000.00")


def test_missing_terms_are_conservative_and_actionable(app: Flask) -> None:
    with app.app_context():
        portfolio, _ = _fd_position()
        summary = build_portfolio_summary(portfolio, date(2026, 8, 28))

        holding = summary["holdings"][0]
        assert holding["fixed_deposit"]["maturity_status"] == "missing_terms"
        assert holding["term_liquidity_reporting_amount"] == Decimal("85000")
        assert holding["accessible_reporting_amount"] == Decimal("0")
        item = summary["actionable_attention_items"][0]
        assert item["action_kind"] == "fixed_deposit_terms"
        assert item["maturity_status"] == "missing_terms"


def test_decided_approaching_maturity_stays_visible_without_attention(app: Flask) -> None:
    with app.app_context():
        portfolio, registration = _fd_position()
        _save_terms(portfolio, registration, action="return_to_cash")
        summary = build_portfolio_summary(portfolio, date(2026, 8, 28))

        assert summary["holdings"][0]["fixed_deposit"]["maturity_status"] == "approaching"
        assert not any(
            item["action_kind"] == "fixed_deposit_terms"
            for item in summary["actionable_attention_items"]
        )


def test_terms_route_round_trip_and_validation_focus(
    client: FlaskClient, app: Flask
) -> None:
    app.config["CURRENT_DATE_PROVIDER"] = lambda: date(2026, 8, 28)
    with app.app_context():
        _, registration = _fd_position()
        registration_id = registration.id

    page = client.get(f"/positions/{registration_id}/fixed-deposit?as_of=2026-08-28")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "This does not change its" in body
    assert "FD_USD_204" in body
    assert 'value="USD"' in body
    assert 'value="2026-06-02"' in body
    assert "Terms missing" in body

    invalid = client.post(
        f"/positions/{registration_id}/fixed-deposit?as_of=2026-08-28",
        data={
            "currency_code": "USD",
            "start_date": "2026-06-02",
            "maturity_date": "2026-06-01",
            "annual_rate_percent": "3.25",
            "maturity_action": "return_to_cash",
            "notes": "Confirmation 204",
        },
    )
    invalid_body = invalid.get_data(as_text=True)
    assert invalid.status_code == 200
    assert "Maturity date must be after the start date" in invalid_body
    maturity_tag = re.search(r'<input[^>]*id="maturity_date"[^>]*>', invalid_body).group(0)
    assert 'aria-invalid="true"' in maturity_tag
    assert "autofocus" in maturity_tag

    saved = client.post(
        f"/positions/{registration_id}/fixed-deposit?as_of=2026-08-28",
        data={
            "currency_code": "USD",
            "start_date": "2026-06-02",
            "maturity_date": "2026-09-02",
            "annual_rate_percent": "3.25",
            "maturity_action": "return_to_cash",
            "notes": "Confirmation 204",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 302
    assert saved.headers["Location"].endswith("/holdings?as_of=2026-08-28")

    with app.app_context():
        terms = db.session.query(FixedDeposit).one()
        assert terms.position_registration_id == registration_id
        assert terms.annual_rate_decimal == Decimal("0.032500000000")
        assert terms.maturity_action == "return_to_cash"
        observation = db.session.query(ValuationObservation).one()
        assert observation.native_value_amount == Decimal("85000")


def test_terms_route_rejects_non_fixed_deposit(client: FlaskClient, app: Flask) -> None:
    with app.app_context():
        _, registration = _fd_position(instrument_type="bond")
        registration_id = registration.id
    assert client.get(f"/positions/{registration_id}/fixed-deposit").status_code == 404
