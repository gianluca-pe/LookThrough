"""Relationship FX sources remain maintainable through the routine form."""

from datetime import date
from decimal import Decimal
import re

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import (
    Account, CashBalanceCheckpoint, FxRate, Institution, Instrument, Portfolio,
    PositionRegistration, RelationshipRule, ValuationObservation,
)
from app.services.relationships import evaluate_relationship_rule
from app.services.routine_updates import build_routine_update_session


AS_OF = date(2026, 9, 5)


def _bank(portfolio, name, target, *, source="EUR"):
    institution = Institution(portfolio_id=portfolio.id, name=name)
    db.session.add(institution)
    db.session.flush()
    account = Account(
        portfolio_id=portfolio.id, institution_id=institution.id,
        name=f"{name} cash", account_type="cash",
        default_currency_code=source, cash_tracking_mode="separate_cash",
        relationship_eligible=True, is_active=True,
    )
    rule = RelationshipRule(
        institution_id=institution.id, name="Minimum",
        threshold_currency_code=target, threshold_amount=Decimal("100"),
        is_active=True,
    )
    db.session.add_all([account, rule])
    db.session.flush()
    db.session.add(CashBalanceCheckpoint(
        account_id=account.id, currency_code=source, effective_date=AS_OF,
        confirmed_balance_amount=Decimal("1000"),
    ))
    return account, rule


@pytest.fixture
def portfolio_id(app):
    with app.app_context():
        portfolio = Portfolio(
            name="Test", reporting_currency_code="USD",
            annual_spending_amount=Decimal("100"),
            annual_spending_currency_code="USD", default_as_of_date=AS_OF,
        )
        db.session.add(portfolio)
        db.session.flush()
        _bank(portfolio, "UK bank", "GBP")
        _bank(portfolio, "Singapore bank", "SGD")
        for quote, rate in (("GBP", "0.85"), ("SGD", "1.5")):
            db.session.add(FxRate(
                effective_date=date(2026, 8, 1),
                base_currency_code="EUR", quote_currency_code=quote,
                quote_per_base_amount=Decimal(rate),
            ))
        db.session.commit()
        return portfolio.id


def _rows(portfolio):
    return {
        (row["base_currency_code"], row["quote_currency_code"]): row
        for row in build_routine_update_session(portfolio, AS_OF)["fx_rows"]
    }


def test_bulk_relationship_fx_updates_clear_stale_dependencies(app, client, portfolio_id):
    with app.app_context():
        portfolio = db.session.get(Portfolio, portfolio_id)
        rules = list(db.session.scalars(select(RelationshipRule)))
        for rule in rules:
            snapshot = evaluate_relationship_rule(portfolio, rule, AS_OF)
            assert snapshot.status == "stale"
            assert any(
                item["base_currency"] == "EUR"
                and item["quote_currency"] == rule.threshold_currency_code
                for item in snapshot.value_dependencies
            )
        rows = _rows(portfolio)
        assert rows[("EUR", "GBP")]["status"] == "stale"
        assert rows[("EUR", "SGD")]["purposes"] == (
            "Relationship minimum: Singapore bank · Minimum",
        )

    body = client.get("/values?as_of=2026-09-05").get_data(as_text=True)
    for quote in ("GBP", "SGD"):
        assert f"1 EUR = RATE {quote}" in body
        assert f'for="fx_EUR_{quote}"' in body
        assert re.search(rf'<input[^>]*id="fx_EUR_{quote}"[^>]*value=""', body)
    assert "Relationship minimum: UK bank" in body
    response = client.post("/values/routine/fx", data={
        "effective_date": AS_OF.isoformat(),
        "fx_EUR_GBP": "0.86", "fx_EUR_SGD": "1.51",
    })
    assert response.status_code == 302
    assert "as_of=2026-09-05" in response.headers["Location"]
    with app.app_context():
        portfolio = db.session.get(Portfolio, portfolio_id)
        for rule in db.session.scalars(select(RelationshipRule)):
            snapshot = evaluate_relationship_rule(portfolio, rule, AS_OF)
            assert snapshot.status == "current"
            assert snapshot.value_dependencies == ()
            expected = "860" if rule.threshold_currency_code == "GBP" else "1510"
            assert snapshot.eligible_value_amount == Decimal(expected)


def test_relationship_pairs_deduplicate_inverses_and_keep_reporting_direction(app, portfolio_id):
    with app.app_context():
        portfolio = db.session.get(Portfolio, portfolio_id)
        _bank(portfolio, "Another UK bank", "GBP")
        _bank(portfolio, "Reverse minimum", "EUR", source="GBP")
        _bank(portfolio, "Reporting overlap", "USD")
        db.session.commit()
        rows = _rows(portfolio)
        assert ("GBP", "EUR") not in rows
        assert len(rows[("EUR", "GBP")]["purposes"]) == 3
        assert ("USD", "EUR") not in rows
        assert rows[("EUR", "USD")]["purposes"] == (
            "Portfolio reporting", "Relationship minimum: Reporting overlap · Minimum",
        )
        assert build_routine_update_session(portfolio, AS_OF)["reporting_currency"] == "USD"


@pytest.mark.parametrize("exclusion", ["ineligible", "archived", "inactive_rule", "other_portfolio"])
def test_relationship_pairs_exclude_unrelated_accounts_and_rules(app, portfolio_id, exclusion):
    with app.app_context():
        portfolio = db.session.get(Portfolio, portfolio_id)
        owner = portfolio
        if exclusion == "other_portfolio":
            owner = Portfolio(
                name="Other", reporting_currency_code="USD",
                annual_spending_amount=Decimal("100"),
            )
            db.session.add(owner)
            db.session.flush()
        account, rule = _bank(owner, "Excluded", "CAD", source="JPY")
        if exclusion == "ineligible":
            account.relationship_eligible = False
        elif exclusion == "archived":
            account.is_active = False
        elif exclusion == "inactive_rule":
            rule.is_active = False
        db.session.commit()
        rows = _rows(portfolio)
        assert ("JPY", "CAD") not in rows and ("CAD", "JPY") not in rows


def test_statement_relationship_pairs_follow_effective_dates_and_missing_sources(app, portfolio_id):
    with app.app_context():
        portfolio = db.session.get(Portfolio, portfolio_id)
        account = db.session.scalar(select(Account).where(Account.name == "UK bank cash"))
        instrument = Instrument(
            portfolio_id=portfolio.id, name="Wrapper",
            instrument_type="other", valuation_currency_code="JPY",
        )
        db.session.add(instrument)
        db.session.flush()
        registration = PositionRegistration(
            account_id=account.id, instrument_id=instrument.id,
            tracking_mode="statement_valued", opening_date=date(2026, 9, 6),
        )
        db.session.add(registration)
        db.session.commit()
        assert ("JPY", "GBP") not in _rows(portfolio)
        registration.opening_date = AS_OF
        db.session.commit()
        assert _rows(portfolio)[("JPY", "GBP")]["status"] == "missing"
        db.session.add(ValuationObservation(
            position_registration_id=registration.id, effective_date=AS_OF,
            native_value_amount=Decimal("100"), currency_code="JPY",
        ))
        registration.closing_date = AS_OF
        db.session.commit()
        assert ("JPY", "GBP") not in _rows(portfolio)


def test_new_relationship_fx_rows_keep_atomic_error_recovery_and_confirmation(app, client, portfolio_id):
    data = {
        "effective_date": AS_OF.isoformat(),
        "fx_EUR_GBP": "0.86", "fx_EUR_SGD": "3",
    }
    failed = client.post("/values/routine/fx", data=data)
    body = failed.get_data(as_text=True)
    assert failed.status_code == 200
    assert 'href="#fx_EUR_SGD"' in body
    field = re.search(r'<input[^>]*id="fx_EUR_SGD"[^>]*>', body).group()
    assert 'value="3"' in field and 'aria-invalid="true"' in field and "autofocus" in field
    with app.app_context():
        assert db.session.scalar(select(FxRate.id).where(FxRate.effective_date == AS_OF)) is None
    data["ack_fx_EUR_SGD"] = "1"
    assert client.post("/values/routine/fx", data=data).status_code == 302
    data["fx_EUR_GBP"] = "0.87"
    data["fx_EUR_SGD"] = ""
    failed = client.post("/values/routine/fx", data=data)
    assert "Confirm replacement of the existing FX source" in failed.get_data(as_text=True)
    data["replace_fx_EUR_GBP"] = "1"
    assert client.post("/values/routine/fx", data=data).status_code == 302
    with app.app_context():
        rate = db.session.scalar(select(FxRate).where(
            FxRate.effective_date == AS_OF, FxRate.quote_currency_code == "GBP",
        ))
        assert rate.quote_per_base_amount == Decimal("0.87")
        assert "previous 1 EUR = 0.86 GBP" in rate.source_note
