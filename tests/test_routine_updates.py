"""One-date routine maintenance."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import re

from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    Account,
    CashBalanceCheckpoint,
    FxRate,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    Posting,
    Price,
    Transaction,
    ValuationObservation,
)


def _records(app: Flask) -> dict[str, int]:
    with app.app_context():
        portfolio = Portfolio(
            name="Example portfolio",
            reporting_currency_code="EUR",
            annual_spending_amount=Decimal("30000"),
            annual_spending_currency_code="USD",
            default_as_of_date=date(2026, 8, 29),
        )
        institution = Institution(portfolio=portfolio, name="Bank")
        db.session.add_all([portfolio, institution])
        db.session.flush()
        first = Account(
            portfolio_id=portfolio.id,
            institution_id=institution.id,
            name="Investment EUR",
            account_type="brokerage",
            default_currency_code="EUR",
            is_multicurrency=True,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        second = Account(
            portfolio_id=portfolio.id,
            institution_id=institution.id,
            name="Investment USD",
            account_type="brokerage",
            default_currency_code="USD",
            is_multicurrency=False,
            cash_tracking_mode="included_in_aggregate",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        fund = Instrument(
            portfolio_id=portfolio.id,
            name="Global fund",
            ticker_or_isin="GLOBAL",
            instrument_type="fund",
            valuation_currency_code="USD",
            is_active=True,
        )
        bond = Instrument(
            portfolio_id=portfolio.id,
            name="Short bond",
            instrument_type="bond",
            valuation_currency_code="EUR",
            is_active=True,
        )
        wrapper = Instrument(
            portfolio_id=portfolio.id,
            name="Opaque wrapper",
            instrument_type="other",
            valuation_currency_code="USD",
            is_active=True,
        )
        deposit = Instrument(
            portfolio_id=portfolio.id,
            name="FD USD 1",
            instrument_type="fixed_deposit",
            valuation_currency_code="USD",
            is_active=True,
        )
        db.session.add_all([first, second, fund, bond, wrapper, deposit])
        db.session.flush()
        registrations = [
            PositionRegistration(
                account_id=first.id,
                instrument_id=fund.id,
                tracking_mode="transaction_tracked",
                opening_date=date(2026, 8, 1),
            ),
            PositionRegistration(
                account_id=second.id,
                instrument_id=fund.id,
                tracking_mode="transaction_tracked",
                opening_date=date(2026, 8, 1),
            ),
            PositionRegistration(
                account_id=first.id,
                instrument_id=bond.id,
                tracking_mode="transaction_tracked",
                opening_date=date(2026, 8, 1),
            ),
            PositionRegistration(
                account_id=second.id,
                instrument_id=wrapper.id,
                tracking_mode="statement_valued",
                opening_date=date(2026, 8, 1),
            ),
            PositionRegistration(
                account_id=second.id,
                instrument_id=deposit.id,
                tracking_mode="statement_valued",
                opening_date=date(2026, 8, 1),
            ),
        ]
        opening = Transaction(
            portfolio_id=portfolio.id,
            transaction_type="opening_balance",
            effective_date=date(2026, 8, 1),
            status="posted",
        )
        db.session.add_all(registrations + [opening])
        db.session.flush()
        db.session.add_all(
            [
                Posting(
                    transaction_id=opening.id,
                    account_id=first.id,
                    posting_kind="instrument",
                    instrument_id=fund.id,
                    currency_code="USD",
                    quantity_delta=Decimal("10"),
                ),
                Posting(
                    transaction_id=opening.id,
                    account_id=second.id,
                    posting_kind="instrument",
                    instrument_id=fund.id,
                    currency_code="USD",
                    quantity_delta=Decimal("5"),
                ),
                Posting(
                    transaction_id=opening.id,
                    account_id=first.id,
                    posting_kind="instrument",
                    instrument_id=bond.id,
                    currency_code="EUR",
                    quantity_delta=Decimal("20"),
                ),
                ValuationObservation(
                    position_registration_id=registrations[3].id,
                    effective_date=date(2026, 8, 20),
                    native_value_amount=Decimal("5000"),
                    currency_code="USD",
                ),
                ValuationObservation(
                    position_registration_id=registrations[4].id,
                    effective_date=date(2026, 8, 20),
                    native_value_amount=Decimal("85000"),
                    currency_code="USD",
                ),
                Price(
                    instrument_id=fund.id,
                    effective_date=date(2026, 8, 20),
                    price_amount=Decimal("12.5"),
                    currency_code="USD",
                ),
                Price(
                    instrument_id=bond.id,
                    effective_date=date(2026, 8, 20),
                    price_amount=Decimal("99"),
                    currency_code="EUR",
                ),
                FxRate(
                    effective_date=date(2026, 8, 20),
                    base_currency_code="USD",
                    quote_currency_code="EUR",
                    quote_per_base_amount=Decimal("0.9"),
                ),
                CashBalanceCheckpoint(
                    account_id=first.id,
                    currency_code="EUR",
                    effective_date=date(2026, 8, 20),
                    confirmed_balance_amount=Decimal("2000"),
                    prior_calculated_balance_amount=Decimal("0"),
                    correction_amount=Decimal("2000"),
                ),
                CashBalanceCheckpoint(
                    account_id=first.id,
                    currency_code="USD",
                    effective_date=date(2026, 8, 20),
                    confirmed_balance_amount=Decimal("1000"),
                    prior_calculated_balance_amount=Decimal("0"),
                    correction_amount=Decimal("1000"),
                ),
            ]
        )
        db.session.commit()
        return {
            "portfolio_id": portfolio.id,
            "fund_id": fund.id,
            "bond_id": bond.id,
            "wrapper_registration_id": registrations[3].id,
            "deposit_registration_id": registrations[4].id,
            "cash_account_id": first.id,
        }


def test_blank_rows_are_untouched_and_completed_prices_save_together(
    client: FlaskClient, app: Flask
) -> None:
    records = _records(app)
    response = client.post(
        "/values/routine/prices",
        data={
            "effective_date": "2026-08-30",
            f"price_{records['fund_id']}": "13.25",
            f"price_{records['bond_id']}": "",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(
        "/values?as_of=2026-08-30#routine-prices-heading"
    )
    with app.app_context():
        rows = list(
            db.session.scalars(
                select(Price).where(Price.effective_date == date(2026, 8, 30))
            )
        )
        assert [(row.instrument_id, row.price_amount) for row in rows] == [
            (records["fund_id"], Decimal("13.250000000000"))
        ]


def test_one_bad_price_blocks_the_section_and_preserves_focus_and_values(
    client: FlaskClient, app: Flask
) -> None:
    records = _records(app)
    body = client.post(
        "/values/routine/prices",
        data={
            "effective_date": "2026-08-30",
            f"price_{records['fund_id']}": "13.25",
            f"price_{records['bond_id']}": "not-a-number",
        },
    ).get_data(as_text=True)
    with app.app_context():
        assert db.session.scalar(
            select(func.count(Price.id)).where(
                Price.effective_date == date(2026, 8, 30)
            )
        ) == 0
    invalid = re.search(
        rf'<input[^>]*id="price_{records["bond_id"]}"[^>]*>', body
    ).group(0)
    valid = re.search(
        rf'<input[^>]*id="price_{records["fund_id"]}"[^>]*>', body
    ).group(0)
    assert 'aria-invalid="true"' in invalid and "autofocus" in invalid
    assert 'value="not-a-number"' in invalid
    assert 'value="13.25"' in valid
    assert f'href="#price_{records["bond_id"]}"' in body


def test_statement_zero_cash_negative_and_fx_use_existing_source_contracts(
    client: FlaskClient, app: Flask
) -> None:
    records = _records(app)
    statement = client.post(
        "/values/routine/statements",
        data={
            "effective_date": "2026-08-30",
            f"statement_{records['wrapper_registration_id']}": "0",
        },
    )
    cash = client.post(
        "/values/routine/cash",
        data={
            "effective_date": "2026-08-30",
            f"cash_{records['cash_account_id']}_EUR": "2500",
            f"cash_{records['cash_account_id']}_USD": "-50",
        },
    )
    fx = client.post(
        "/values/routine/fx",
        data={"effective_date": "2026-08-30", "fx_USD_EUR": "0.91"},
    )
    assert statement.status_code == cash.status_code == fx.status_code == 302

    with app.app_context():
        observation = db.session.scalar(
            select(ValuationObservation).where(
                ValuationObservation.position_registration_id
                == records["wrapper_registration_id"],
                ValuationObservation.effective_date == date(2026, 8, 30),
            )
        )
        assert observation.native_value_amount == Decimal("0")
        checkpoints = list(
            db.session.scalars(
                select(CashBalanceCheckpoint)
                .where(CashBalanceCheckpoint.effective_date == date(2026, 8, 30))
                .order_by(CashBalanceCheckpoint.currency_code)
            )
        )
        assert [(row.currency_code, row.confirmed_balance_amount) for row in checkpoints] == [
            ("EUR", Decimal("2500")),
            ("USD", Decimal("-50")),
        ]
        saved_fx = db.session.scalar(
            select(FxRate).where(FxRate.effective_date == date(2026, 8, 30))
        )
        assert saved_fx.quote_per_base_amount == Decimal("0.91")


def test_same_date_duplicate_blocks_every_price_in_the_submitted_section(
    client: FlaskClient, app: Flask
) -> None:
    records = _records(app)
    with app.app_context():
        db.session.add(
            Price(
                instrument_id=records["fund_id"],
                effective_date=date(2026, 8, 30),
                price_amount=Decimal("14"),
                currency_code="USD",
            )
        )
        db.session.commit()

    body = client.post(
        "/values/routine/prices",
        data={
            "effective_date": "2026-08-30",
            f"price_{records['fund_id']}": "15",
            f"price_{records['bond_id']}": "100",
        },
    ).get_data(as_text=True)
    assert "A price already exists for this instrument and date" in body
    with app.app_context():
        assert db.session.scalar(
            select(func.count(Price.id)).where(
                Price.instrument_id == records["bond_id"],
                Price.effective_date == date(2026, 8, 30),
            )
        ) == 0


def test_routine_same_day_fx_correction_requires_confirmation_and_keeps_context(
    client: FlaskClient, app: Flask
) -> None:
    _records(app)
    first = client.post(
        "/values/routine/fx",
        data={"effective_date": "2026-08-30", "fx_USD_EUR": "0.91"},
    )
    assert first.status_code == 302

    attempted = client.post(
        "/values/routine/fx",
        data={"effective_date": "2026-08-30", "fx_USD_EUR": "0.92"},
    )
    body = attempted.get_data(as_text=True)
    assert attempted.status_code == 200
    # Rendered through the shared routine-checkbox macro, so the apostrophe is
    # HTML-escaped in the source.
    assert "Replace today&#39;s saved rate" in body
    assert "Confirm replacement of the existing FX source" in body
    checkbox = re.search(
        r'<input[^>]*id="replace_fx_USD_EUR"[^>]*>', body
    ).group(0)
    assert 'aria-invalid="true"' in checkbox and "autofocus" in checkbox

    accepted = client.post(
        "/values/routine/fx",
        data={
            "effective_date": "2026-08-30",
            "fx_USD_EUR": "0.92",
            "replace_fx_USD_EUR": "1",
        },
    )
    assert accepted.status_code == 302
    with app.app_context():
        rows = list(
            db.session.scalars(
                select(FxRate)
                .where(FxRate.effective_date == date(2026, 8, 30))
            )
        )
        assert len(rows) == 1
        assert rows[0].quote_per_base_amount == Decimal("0.92")
        assert "previous 1 USD = 0.91 EUR" in rows[0].source_note
