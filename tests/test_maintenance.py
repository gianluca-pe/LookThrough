"""Exact CSV exports and safe full-dataset backup/restore."""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient

from app.extensions import db
from app.models import (
    Account,
    CashBalanceCheckpoint,
    FixedDeposit,
    FxRate,
    Institution,
    Instrument,
    InstrumentClassification,
    Portfolio,
    PortfolioSnapshot,
    RetirementAssumption,
    PositionRegistration,
    Posting,
    Price,
    RelationshipRule,
    Transaction,
    ValuationObservation,
)
from app.services.backup import BackupValidationError, export_backup, validate_backup
from app.services.portfolio_summary import build_portfolio_summary
from app.services.snapshots import save_snapshot
from app.services.retirement import save_retirement_assumption


AS_OF = date(2026, 8, 28)


def _seed_complete_dataset(app: Flask) -> dict[str, int]:
    with app.app_context():
        portfolio = Portfolio(
            name="Exact portfolio",
            reporting_currency_code="EUR",
            annual_spending_amount=Decimal("48000.13"),
            annual_spending_currency_code="EUR",
            annual_inflation_decimal=Decimal("0.03"),
            default_as_of_date=AS_OF,
        )
        db.session.add(portfolio)
        db.session.flush()
        institution = Institution(
            portfolio_id=portfolio.id,
            name="Example Bank",
            country_code="GB",
            notes="Local test",
        )
        db.session.add(institution)
        db.session.flush()
        cash_account = Account(
            portfolio_id=portfolio.id,
            institution_id=institution.id,
            name="Cash account",
            account_type="cash",
            default_currency_code="EUR",
            is_multicurrency=True,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        brokerage = Account(
            portfolio_id=portfolio.id,
            institution_id=institution.id,
            name="Brokerage",
            account_type="brokerage",
            default_currency_code="USD",
            is_multicurrency=True,
            cash_tracking_mode="separate_cash",
            cash_settlement_account=cash_account,
            portfolio_share_decimal=Decimal("0.75"),
            present_access_decimal=Decimal("0.5"),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add_all([cash_account, brokerage])
        db.session.flush()
        fund = Instrument(
            portfolio_id=portfolio.id,
            name="Precise Fund",
            ticker_or_isin="PF-1",
            instrument_type="fund",
            valuation_currency_code="USD",
            fire_bucket_code="growth",
            is_active=True,
        )
        deposit = Instrument(
            portfolio_id=portfolio.id,
            name="FD 1",
            instrument_type="fixed_deposit",
            valuation_currency_code="USD",
            fire_bucket_code="now",
            is_active=True,
        )
        db.session.add_all([fund, deposit])
        db.session.flush()
        db.session.add(
            InstrumentClassification(
                instrument_id=fund.id,
                economic_role_code="growth",
                weight_decimal=Decimal("1"),
                effective_date=date(2026, 8, 1),
                source_note="Owner entered",
            )
        )
        fund_registration = PositionRegistration(
            account_id=brokerage.id,
            instrument_id=fund.id,
            tracking_mode="transaction_tracked",
            opening_date=date(2026, 8, 1),
        )
        fd_registration = PositionRegistration(
            account_id=brokerage.id,
            instrument_id=deposit.id,
            tracking_mode="statement_valued",
            opening_date=date(2026, 8, 2),
        )
        db.session.add_all([fund_registration, fd_registration])
        db.session.flush()
        transaction = Transaction(
            portfolio_id=portfolio.id,
            transaction_type="buy",
            effective_date=date(2026, 8, 3),
            status="posted",
            activity_group_id="group-one",
            note="Exact entry, comma included",
            created_at=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
        )
        db.session.add(transaction)
        db.session.flush()
        db.session.add_all(
            [
                Posting(
                    transaction_id=transaction.id,
                    account_id=brokerage.id,
                    posting_kind="instrument",
                    instrument_id=fund.id,
                    currency_code="USD",
                    quantity_delta=Decimal("3.123457"),
                    unit_price=Decimal("7.123457"),
                    price_currency_code="USD",
                ),
                Posting(
                    transaction_id=transaction.id,
                    account_id=cash_account.id,
                    posting_kind="cash",
                    currency_code="USD",
                    cash_amount_delta=Decimal("-22.25"),
                ),
            ]
        )
        db.session.add_all(
            [
                CashBalanceCheckpoint(
                    account_id=cash_account.id,
                    currency_code="EUR",
                    effective_date=date(2026, 8, 1),
                    confirmed_balance_amount=Decimal("1234.57"),
                    source_note="Statement",
                ),
                Price(
                    instrument_id=fund.id,
                    effective_date=AS_OF,
                    price_amount=Decimal("8.987654"),
                    currency_code="USD",
                    source_note="Manual",
                ),
                ValuationObservation(
                    position_registration_id=fd_registration.id,
                    effective_date=date(2026, 8, 2),
                    native_value_amount=Decimal("85000"),
                    currency_code="USD",
                    source_note="Bank confirmation",
                ),
                FxRate(
                    effective_date=AS_OF,
                    base_currency_code="USD",
                    quote_currency_code="EUR",
                    quote_per_base_amount=Decimal("0.912346"),
                    source_note="Manual",
                ),
                FixedDeposit(
                    position_registration_id=fd_registration.id,
                    currency_code="USD",
                    start_date=date(2026, 8, 2),
                    maturity_date=date(2026, 11, 2),
                    annual_rate_decimal=Decimal("0.0325"),
                    expected_maturity_proceeds_amount=Decimal("85714.66"),
                    maturity_action="return_to_cash",
                    notes="Expected proceeds confirmed",
                ),
                RelationshipRule(
                    institution_id=institution.id,
                    name="Preferred relationship",
                    threshold_amount=Decimal("100000"),
                    threshold_currency_code="EUR",
                    warning_buffer_amount=Decimal("10000"),
                    is_active=True,
                ),
            ]
        )
        db.session.commit()
        save_snapshot(portfolio, AS_OF, note="Complete update")
        save_retirement_assumption(
            portfolio.id,
            current_age_years=50,
            withdrawal_start_age_years=55,
            final_age_years=90,
            role_returns={
                "equity": Decimal("0.06"),
                "income": Decimal("0.03"),
                "liquidity": Decimal("0.01"),
                "alternatives": Decimal("0.04"),
            },
            terminal_legacy_target_amount=Decimal("500000"),
        )
        db.session.commit()
        return {
            "portfolio_id": portfolio.id,
            "fund_id": fund.id,
            "account_id": brokerage.id,
            "transaction_id": transaction.id,
        }


def test_holdings_csv_uses_shared_summary_and_preserves_exact_decimals(
    app: Flask, client: FlaskClient
) -> None:
    ids = _seed_complete_dataset(app)
    response = client.get(f"/holdings.csv?as_of={AS_OF.isoformat()}")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "lookthrough-holdings-2026-08-28.csv" in response.headers[
        "Content-Disposition"
    ]
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
    investment = next(row for row in rows if row["instrument"] == "Precise Fund")
    with app.app_context():
        portfolio = db.session.get(Portfolio, ids["portfolio_id"])
        expected = next(
            row
            for row in build_portfolio_summary(portfolio, AS_OF)["holdings"]
            if row["instrument_name"] == "Precise Fund"
        )
    assert investment["quantity"] == format(expected["quantity"], "f")
    assert investment["reporting_amount"] == format(
        expected["reporting_amount"], "f"
    )
    assert investment["portfolio_share_decimal"] == "0.75000000"
    assert any(row["row_type"] == "cash" for row in rows)


def test_activity_csv_reuses_filters_and_quotes_notes(
    app: Flask, client: FlaskClient
) -> None:
    ids = _seed_complete_dataset(app)
    response = client.get(
        f"/activity.csv?account_id={ids['account_id']}&type=buy&status=posted"
    )
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
    assert len(rows) == 1
    assert rows[0]["transaction_id"] == str(ids["transaction_id"])
    assert rows[0]["quantity_effect"] == "3.123457"
    assert rows[0]["note"] == "Exact entry, comma included"
    assert client.get("/activity.csv?date_from=bad").status_code == 400


def test_backup_validation_rejects_corrupt_and_inconsistent_files(app: Flask) -> None:
    _seed_complete_dataset(app)
    with app.app_context():
        valid_payload = json.loads(export_backup())
        assert validate_backup(json.dumps(valid_payload).encode()).preview.total_records
        bad_version = {**valid_payload, "version": 99}
        try:
            validate_backup(json.dumps(bad_version).encode())
        except BackupValidationError as error:
            assert "not supported" in str(error)
        else:
            raise AssertionError("incompatible backup accepted")
        invalid_fk = json.loads(json.dumps(valid_payload))
        invalid_fk["tables"]["accounts"][0]["institution_id"] = 999999
        try:
            validate_backup(json.dumps(invalid_fk).encode())
        except BackupValidationError as error:
            assert "invalid" in str(error) or "inconsistent" in str(error)
        else:
            raise AssertionError("invalid relationship accepted")


def test_version_one_backup_is_upgraded_with_empty_allocation_targets(app: Flask) -> None:
    _seed_complete_dataset(app)
    with app.app_context():
        payload = json.loads(export_backup())
        payload["version"] = 1
        payload["tables"].pop("decimal_conversions", None)
        payload["tables"].pop("retirement_scenarios")
        payload["tables"].pop("fx_reference_sets")
        payload["tables"].pop("allocation_targets")
        payload["tables"].pop("retirement_assumptions")
        payload["tables"].pop("retirement_plans")
        payload["tables"].pop("retirement_income")
        payload["tables"].pop("portfolio_snapshots")
        for row in payload["tables"]["portfolios"]:
            row.pop("annual_inflation_decimal")
        validated = validate_backup(json.dumps(payload).encode())
        assert validated.preview.version == 1
        assert validated.payload["version"] == 11
        assert validated.decoded_tables["allocation_targets"] == []
        assert validated.decoded_tables["retirement_assumptions"] == []
        assert validated.decoded_tables["portfolio_snapshots"] == []
        assert validated.decoded_tables["portfolios"][0][
            "annual_inflation_decimal"
        ] is None


def test_version_two_backup_is_upgraded_with_missing_inflation(app: Flask) -> None:
    _seed_complete_dataset(app)
    with app.app_context():
        payload = json.loads(export_backup())
        payload["version"] = 2
        payload["tables"].pop("decimal_conversions", None)
        payload["tables"].pop("retirement_scenarios")
        payload["tables"].pop("fx_reference_sets")
        payload["tables"].pop("retirement_assumptions")
        payload["tables"].pop("retirement_plans")
        payload["tables"].pop("retirement_income")
        payload["tables"].pop("portfolio_snapshots")
        for row in payload["tables"]["portfolios"]:
            row.pop("annual_inflation_decimal")
        validated = validate_backup(json.dumps(payload).encode())

    assert validated.preview.version == 2
    assert validated.payload["version"] == 11
    assert validated.decoded_tables["portfolios"][0][
        "annual_inflation_decimal"
    ] is None


def test_version_three_backup_is_upgraded_with_empty_snapshots(app: Flask) -> None:
    _seed_complete_dataset(app)
    with app.app_context():
        payload = json.loads(export_backup())
        payload["version"] = 3
        payload["tables"].pop("decimal_conversions", None)
        payload["tables"].pop("retirement_scenarios")
        payload["tables"].pop("fx_reference_sets")
        payload["tables"].pop("retirement_assumptions")
        payload["tables"].pop("retirement_plans")
        payload["tables"].pop("retirement_income")
        payload["tables"].pop("portfolio_snapshots")
        validated = validate_backup(json.dumps(payload).encode())

    assert validated.preview.version == 3
    assert validated.payload["version"] == 11
    assert validated.decoded_tables["portfolio_snapshots"] == []
    assert validated.decoded_tables["retirement_assumptions"] == []


def test_version_four_backup_is_upgraded_with_empty_retirement_assumptions(
    app: Flask,
) -> None:
    _seed_complete_dataset(app)
    with app.app_context():
        payload = json.loads(export_backup())
        payload["version"] = 4
        payload["tables"].pop("decimal_conversions", None)
        payload["tables"].pop("retirement_scenarios")
        payload["tables"].pop("fx_reference_sets")
        payload["tables"].pop("retirement_assumptions")
        payload["tables"].pop("retirement_plans")
        payload["tables"].pop("retirement_income")
        validated = validate_backup(json.dumps(payload).encode())

    assert validated.preview.version == 4
    assert validated.payload["version"] == 11
    assert validated.decoded_tables["retirement_assumptions"] == []


def test_restore_preview_requires_confirmation_and_round_trips_all_sources(
    app: Flask, client: FlaskClient
) -> None:
    ids = _seed_complete_dataset(app)
    with app.app_context():
        original_summary = build_portfolio_summary(
            db.session.get(Portfolio, ids["portfolio_id"]), AS_OF
        )
    exported = client.get("/backup/export")
    assert exported.status_code == 200
    original_tables = json.loads(exported.data)["tables"]

    with app.app_context():
        db.drop_all()
        db.create_all()
    clean_settings = client.get("/settings")
    assert clean_settings.status_code == 200
    assert "No portfolio is configured" in clean_settings.get_data(as_text=True)

    preview = client.post(
        "/backup/preview",
        data={"backup_file": (io.BytesIO(exported.data), "backup.json")},
        content_type="multipart/form-data",
    )
    assert preview.status_code == 200
    page = preview.get_data(as_text=True)
    assert "The active database is still unchanged" in page
    token = re.search(r'name="restore_token"[^>]*value="([0-9a-f]{32})"', page)
    assert token is not None

    refused = client.post(
        "/backup/restore", data={"restore_token": token.group(1)}
    )
    assert refused.status_code == 400
    with app.app_context():
        assert db.session.get(Portfolio, ids["portfolio_id"]) is None
        assert db.session.query(Price).count() == 0

    restored = client.post(
        "/backup/restore",
        data={"restore_token": token.group(1), "confirm_restore": "y"},
        follow_redirects=True,
    )
    assert restored.status_code == 200
    assert "Backup restored" in restored.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Portfolio, ids["portfolio_id"]).name == "Exact portfolio"
        assert db.session.query(Price).count() == 1
        assert db.session.query(PortfolioSnapshot).count() == 1
        assert db.session.query(RetirementAssumption).count() == 1
        restored_tables = json.loads(export_backup())["tables"]
        assert restored_tables == original_tables
        restored_summary = build_portfolio_summary(
            db.session.get(Portfolio, ids["portfolio_id"]), AS_OF
        )
        assert restored_summary == original_summary


def test_settings_allows_clean_restore_while_exports_require_a_portfolio(
    client: FlaskClient,
) -> None:
    assert client.get("/settings").status_code == 200
    for path in ("/backup/export", "/holdings.csv", "/activity.csv"):
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/setup")
