"""Account share and present-access calculation contract."""

from datetime import date
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient

from app.extensions import db
from app.models import Account, FxRate, Institution, Instrument, PositionRegistration
from app.services.portfolio_summary import build_portfolio_summary
from test_account_maintenance_routes import _edit_data, _records as _account_records
from test_m3_cash_summary import (
    AS_OF,
    _account,
    _confirm,
    _portfolio,
    _records,
    _statement_value,
)


def _missing_statement_position(portfolio, account) -> None:
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name="Missing statement investment",
        instrument_type="other",
        valuation_currency_code="EUR",
        is_active=True,
    )
    db.session.add(instrument)
    db.session.flush()
    db.session.add(
        PositionRegistration(
            account_id=account.id,
            instrument_id=instrument.id,
            tracking_mode="statement_valued",
            opening_date=date(2026, 8, 1),
        )
    )
    db.session.commit()


def test_joint_account_share_limits_value_even_with_full_single_signature_access(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, _, account = _records()
        account.portfolio_share_decimal = Decimal("0.5")
        account.present_access_decimal = Decimal("1")
        db.session.commit()
        _statement_value(portfolio, account, "80000")
        _confirm(portfolio, account, "20000")

        summary = build_portfolio_summary(portfolio, AS_OF)

        assert summary["gross_reporting_amount"] == Decimal(
            "100000.000000000000"
        )
        assert summary["total_reporting_amount"] == summary[
            "gross_reporting_amount"
        ]
        assert summary["included_reporting_amount"] == Decimal(
            "50000.0000000000000"
        )
        assert summary["accessible_reporting_amount"] == Decimal(
            "50000.0000000000000"
        )
        assert summary["excluded_by_share_reporting_amount"] == Decimal(
            "50000.0000000000000"
        )
        assert summary["restricted_reporting_amount"] == Decimal("0E-13")

        holding = summary["holdings"][0]
        assert holding["reporting_amount"] == Decimal("80000.000000000000")
        assert holding["included_reporting_amount"] == Decimal(
            "40000.0000000000000"
        )
        assert holding["accessible_reporting_amount"] == Decimal(
            "40000.0000000000000"
        )
        cash = summary["cash_balances"][0]
        assert cash["reporting_amount"] == Decimal("20000.000000000000")
        assert cash["included_reporting_amount"] == Decimal(
            "10000.0000000000000"
        )
        assert cash["accessible_reporting_amount"] == Decimal(
            "10000.0000000000000"
        )

        account_summary = summary["account_summaries"][0]
        assert account_summary["portfolio_share_decimal"] == Decimal("0.5")
        assert account_summary["present_access_decimal"] == Decimal("1")
        assert account_summary["gross_reporting_amount"] == Decimal(
            "100000.000000000000"
        )
        assert account_summary["included_reporting_amount"] == Decimal(
            "50000.0000000000000"
        )
        assert account_summary["accessible_reporting_amount"] == Decimal(
            "50000.0000000000000"
        )
        warning = summary["access_attention_items"][0]
        assert warning["action_kind"] == "edit_account"
        assert warning["detail"] == (
            "Only part of this account is included in the portfolio."
        )


def test_access_applies_to_included_share_not_to_the_whole_account(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, _, account = _records()
        account.portfolio_share_decimal = Decimal("0.625")
        account.present_access_decimal = Decimal("0.8")
        account.earliest_access_date = date(2030, 1, 1)
        account.access_note = "Part is unavailable until the bridge period."
        db.session.commit()
        _statement_value(portfolio, account, "1000")

        summary = build_portfolio_summary(portfolio, AS_OF)

        assert summary["gross_reporting_amount"] == Decimal("1000.000000000000")
        assert summary["included_reporting_amount"] == Decimal(
            "625.0000000000000"
        )
        assert summary["accessible_reporting_amount"] == Decimal(
            "500.00000000000000"
        )
        assert summary["excluded_by_share_reporting_amount"] == Decimal(
            "375.0000000000000"
        )
        assert summary["restricted_reporting_amount"] == Decimal(
            "125.00000000000000"
        )
        row = summary["holdings"][0]
        assert row["earliest_access_date"] == date(2030, 1, 1)
        assert row["access_note"] == (
            "Part is unavailable until the bridge period."
        )
        warning = summary["access_attention_items"][0]
        assert "Only part of this account is included" in warning["detail"]
        assert "only part of that included share is accessible" in warning["detail"]
        assert summary["all_attention_items"] == summary[
            "access_attention_items"
        ]


def test_missing_value_remains_missing_in_every_overlay_and_known_subtotal(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, _, account = _records()
        account.portfolio_share_decimal = Decimal("0.5")
        account.present_access_decimal = Decimal("0.5")
        db.session.commit()
        _statement_value(portfolio, account, "1000")
        _missing_statement_position(portfolio, account)

        summary = build_portfolio_summary(portfolio, AS_OF)

        assert summary["total_status"] == "partial"
        assert summary["gross_status"] == "partial"
        assert summary["included_status"] == "partial"
        assert summary["accessible_status"] == "partial"
        assert summary["gross_reporting_amount"] == Decimal("1000.000000000000")
        assert summary["included_reporting_amount"] == Decimal(
            "500.0000000000000"
        )
        assert summary["accessible_reporting_amount"] == Decimal(
            "250.00000000000000"
        )
        missing = next(
            row
            for row in summary["holdings"]
            if row["instrument_name"] == "Missing statement investment"
        )
        assert missing["reporting_amount"] is None
        assert missing["included_reporting_amount"] is None
        assert missing["accessible_reporting_amount"] is None
        account_summary = summary["account_summaries"][0]
        assert account_summary["status"] == "partial"
        assert account_summary["missing_count"] == 1


def test_zero_share_and_zero_access_are_valid_exact_zero_states(app: Flask) -> None:
    with app.app_context():
        portfolio = _portfolio()
        bank = Institution(portfolio_id=portfolio.id, name="Bank")
        db.session.add(bank)
        db.session.flush()
        excluded = _account(portfolio, bank, "Excluded", currency="EUR")
        restricted = _account(portfolio, bank, "Restricted", currency="EUR")
        excluded.portfolio_share_decimal = Decimal("0")
        restricted.present_access_decimal = Decimal("0")
        db.session.commit()
        _statement_value(portfolio, excluded, "1000")
        _confirm(portfolio, restricted, "500")

        summary = build_portfolio_summary(portfolio, AS_OF)

        assert summary["gross_reporting_amount"] == Decimal("1500.000000000000")
        assert summary["included_reporting_amount"] == Decimal("500.000000000000")
        assert summary["accessible_reporting_amount"] == Decimal("0E-12")
        assert summary["excluded_by_share_reporting_amount"] == Decimal(
            "1000.000000000000"
        )
        assert summary["restricted_reporting_amount"] == Decimal(
            "500.000000000000"
        )
        warnings = {row["account_name"]: row for row in summary["access_attention_items"]}
        assert warnings["Excluded"]["detail"] == (
            "This account is excluded from the portfolio value."
        )
        assert "none of that share is accessible" in warnings["Restricted"][
            "detail"
        ]


def test_cash_uses_the_settlement_accounts_own_share_and_access(app: Flask) -> None:
    with app.app_context():
        portfolio = _portfolio()
        bank = Institution(portfolio_id=portfolio.id, name="Example Bank")
        db.session.add(bank)
        db.session.flush()
        wma = _account(portfolio, bank, "Settlement SGD", currency="SGD")
        wma.portfolio_share_decimal = Decimal("0.5")
        wma.present_access_decimal = Decimal("0.8")
        brokerage = _account(
            portfolio,
            bank,
            "Brokerage SGD",
            currency="SGD",
            settlement_account=wma,
        )
        brokerage.portfolio_share_decimal = Decimal("1")
        brokerage.present_access_decimal = Decimal("1")
        db.session.commit()
        _confirm(portfolio, wma, "1000")
        db.session.add(
            FxRate(
                effective_date=date(2026, 8, 7),
                base_currency_code="SGD",
                quote_currency_code="EUR",
                quote_per_base_amount=Decimal("0.7"),
            )
        )
        db.session.commit()

        summary = build_portfolio_summary(portfolio, AS_OF)

        assert len(summary["cash_balances"]) == 1
        cash = summary["cash_balances"][0]
        assert cash["account_name"] == "Settlement SGD"
        assert cash["reporting_amount"] == Decimal("700.0000000000000")
        assert cash["included_reporting_amount"] == Decimal(
            "350.00000000000000"
        )
        assert cash["accessible_reporting_amount"] == Decimal(
            "280.000000000000000"
        )


def test_account_edit_saves_share_and_access_context(
    app: Flask,
    client: FlaskClient,
) -> None:
    with app.app_context():
        _, account = _account_records()
        account_id = account.id

    response = client.post(
        f"/accounts/{account_id}/edit",
        data=_edit_data(
            cash_tracking_mode="separate_cash",
            portfolio_share_percent="50",
            present_access_percent="100",
            earliest_access_date="2030-01-01",
            access_note="Joint account; either holder can sign alone.",
        ),
    )

    assert response.status_code == 302
    with app.app_context():
        account = db.session.get(Account, account_id)
        assert account.portfolio_share_decimal == Decimal("0.50000000")
        assert account.present_access_decimal == Decimal("1.00000000")
        assert account.earliest_access_date == date(2030, 1, 1)
        assert account.access_note == "Joint account; either holder can sign alone."


def test_account_edit_rejects_invalid_or_missing_percentages_atomically(
    app: Flask,
    client: FlaskClient,
) -> None:
    with app.app_context():
        _, account = _account_records()
        account_id = account.id

    response = client.post(
        f"/accounts/{account_id}/edit",
        data=_edit_data(
            cash_tracking_mode="separate_cash",
            portfolio_share_percent="101",
            present_access_percent="",
        ),
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Portfolio share (%) must be between 0 and 100." in body
    assert "Present access to your included share (%) is required." in body
    with app.app_context():
        account = db.session.get(Account, account_id)
        assert account.portfolio_share_decimal == Decimal("1.00000000")
        assert account.present_access_decimal == Decimal("1.00000000")


def test_legacy_edit_post_preserves_existing_share_and_access(
    app: Flask,
    client: FlaskClient,
) -> None:
    with app.app_context():
        _, account = _account_records()
        account.portfolio_share_decimal = Decimal("0.4")
        account.present_access_decimal = Decimal("0.75")
        account.earliest_access_date = date(2035, 1, 1)
        account.access_note = "Existing context"
        db.session.commit()
        account_id = account.id

    response = client.post(
        f"/accounts/{account_id}/edit",
        data=_edit_data(cash_tracking_mode="separate_cash"),
    )

    assert response.status_code == 302
    with app.app_context():
        account = db.session.get(Account, account_id)
        assert account.portfolio_share_decimal == Decimal("0.40000000")
        assert account.present_access_decimal == Decimal("0.75000000")
        assert account.earliest_access_date == date(2035, 1, 1)
        assert account.access_note == "Existing context"
