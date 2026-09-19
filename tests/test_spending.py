"""Annual spending conversion and the non-mutating reporting view."""

from datetime import date
from decimal import Decimal

from flask import Flask, template_rendered

from app.extensions import db
from app.models import (
    Account,
    CashBalanceCheckpoint,
    FxRate,
    Institution,
    Instrument,
    InstrumentClassification,
    Portfolio,
    PositionRegistration,
    ValuationObservation,
)
from app.services.portfolio_summary import build_portfolio_summary
from app.services.spending import build_spending_value


AS_OF = date(2026, 8, 9)


def _portfolio_account(
    *,
    reporting_currency: str = "EUR",
    spending_amount: str = "10000",
    spending_currency: str = "USD",
    share: str = "1",
    access: str = "1",
):
    portfolio = Portfolio(
        name="Portfolio",
        reporting_currency_code=reporting_currency,
        annual_spending_amount=Decimal(spending_amount),
        annual_spending_currency_code=spending_currency,
        default_as_of_date=AS_OF,
    )
    db.session.add(portfolio)
    db.session.flush()
    institution = Institution(portfolio_id=portfolio.id, name="Bank")
    db.session.add(institution)
    db.session.flush()
    account = Account(
        portfolio_id=portfolio.id,
        institution_id=institution.id,
        name="Account",
        account_type="brokerage",
        default_currency_code=reporting_currency,
        is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal(share),
        present_access_decimal=Decimal(access),
        relationship_eligible=True,
        is_active=True,
    )
    db.session.add(account)
    db.session.flush()
    return portfolio, account


def _statement_position(
    portfolio,
    account,
    *,
    amount: str,
    role_weights: dict[str, str] | None,
):
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name="Fund",
        instrument_type="fund",
        valuation_currency_code=portfolio.reporting_currency_code,
        is_active=True,
    )
    db.session.add(instrument)
    db.session.flush()
    registration = PositionRegistration(
        account_id=account.id,
        instrument_id=instrument.id,
        tracking_mode="statement_valued",
        opening_date=date(2026, 8, 1),
    )
    db.session.add(registration)
    db.session.flush()
    db.session.add(
        ValuationObservation(
            position_registration_id=registration.id,
            effective_date=date(2026, 8, 8),
            native_value_amount=Decimal(amount),
            currency_code=portfolio.reporting_currency_code,
        )
    )
    if role_weights:
        for role_code, weight in role_weights.items():
            db.session.add(
                InstrumentClassification(
                    instrument_id=instrument.id,
                    economic_role_code=role_code,
                    weight_decimal=Decimal(weight),
                    effective_date=date(2026, 8, 8),
                )
            )
    return instrument


def _confirm_cash(account, amount: str) -> None:
    db.session.add(
        CashBalanceCheckpoint(
            account_id=account.id,
            currency_code=account.default_currency_code,
            effective_date=date(2026, 8, 8),
            confirmed_balance_amount=Decimal(amount),
        )
    )


def test_spending_conversion_preserves_exact_amount_and_fx_provenance(app: Flask) -> None:
    with app.app_context():
        portfolio, _ = _portfolio_account()
        db.session.add(FxRate(
            effective_date=date(2026, 8, 8),
            base_currency_code="USD",
            quote_currency_code="EUR",
            quote_per_base_amount=Decimal("0.812346"),
        ))
        db.session.commit()

        spending = build_spending_value(portfolio, AS_OF, "EUR")

        assert spending["native_amount"] == Decimal("10000")
        assert spending["native_currency"] == "USD"
        assert spending["reporting_amount"] == Decimal("8123.460000")
        assert spending["fx_rate"] == Decimal("0.812346")
        assert spending["fx_date"] == date(2026, 8, 8)
        assert spending["fx_source_mode"] == "direct"
        assert spending["status"] == "current"


def test_missing_and_stale_spending_fx_remain_explicit(app: Flask) -> None:
    with app.app_context():
        portfolio, _ = _portfolio_account()
        db.session.commit()
        missing = build_spending_value(portfolio, AS_OF, "EUR")
        assert missing["status"] == "missing"
        assert missing["reporting_amount"] is None
        assert missing["missing_reason"]

        db.session.add(FxRate(
            effective_date=date(2026, 7, 1),
            base_currency_code="USD",
            quote_currency_code="EUR",
            quote_per_base_amount=Decimal("0.8"),
        ))
        db.session.commit()
        stale = build_spending_value(portfolio, AS_OF, "EUR")
        assert stale["status"] == "stale"
        assert stale["reporting_amount"] == Decimal("8000")
        assert stale["fx_date"] == date(2026, 7, 1)


def test_reporting_override_revalues_summary_and_spending_without_mutation(
    app: Flask, client
) -> None:
    with app.app_context():
        portfolio, account = _portfolio_account(
            spending_currency="EUR", spending_amount="100", reporting_currency="EUR"
        )
        account.default_currency_code = "USD"
        _statement_position(
            portfolio, account, amount="100", role_weights={"ballast": "1"}
        )
        _confirm_cash(account, "0")
        db.session.add(
            FxRate(
                effective_date=date(2026, 8, 8),
                base_currency_code="EUR",
                quote_currency_code="USD",
                quote_per_base_amount=Decimal("2"),
            )
        )
        db.session.commit()
        portfolio_id = portfolio.id

        summary = build_portfolio_summary(portfolio, AS_OF, reporting_currency="USD")
        spending = build_spending_value(portfolio, AS_OF, "USD")
        assert summary["included_reporting_amount"] == Decimal("200")
        assert spending["reporting_amount"] == Decimal("200")

    response = client.get("/overview?as_of=2026-08-09&ccy=usd")
    assert response.status_code == 200
    assert "reporting in USD" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Portfolio, portfolio_id).reporting_currency_code == "EUR"

    recorded = []
    def capture(sender, template, context, **extra):
        recorded.append(context)

    with template_rendered.connected_to(capture, app):
        invalid = client.get("/overview?ccy=EU")
    assert invalid.status_code == 200
    assert recorded[-1]["summary"]["reporting_currency"] == "EUR"
    assert "three-letter currency" in recorded[-1]["reporting_currency_error"]
    assert recorded[-1]["reporting_currency_input"] == "EU"
