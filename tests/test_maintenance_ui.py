"""Settings, export, and restore UI tests: page hierarchy,
truthful record counts, export link context, restore error focus, and the
no-JavaScript validate-preview-confirm boundary. Backup validation, staging,
and restore mechanics are backend-tested in test_maintenance.py; these tests
assert only presentation.
"""

from __future__ import annotations

import io
import re
from datetime import date
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient

from app.extensions import db
from app.models import (
    Account,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    ValuationObservation,
)
from app.services.backup import export_backup

from test_overview_ui import assert_no_inline_script


AS_OF = date(2026, 8, 2)


def _portfolio(app: Flask) -> int:
    """Minimal configured portfolio with one valued position."""

    with app.app_context():
        portfolio = Portfolio(
            name="Portfolio", reporting_currency_code="USD",
            annual_spending_amount=Decimal("48000"),
            annual_spending_currency_code="USD",
            default_as_of_date=AS_OF,
        )
        db.session.add(portfolio)
        db.session.flush()
        institution = Institution(portfolio_id=portfolio.id, name="Bank")
        db.session.add(institution)
        db.session.flush()
        account = Account(
            portfolio_id=portfolio.id, institution_id=institution.id,
            name="Brokerage", account_type="brokerage",
            default_currency_code="USD", is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"), present_access_decimal=Decimal("1"),
            relationship_eligible=True, is_active=True,
        )
        db.session.add(account)
        db.session.flush()
        instrument = Instrument(
            portfolio_id=portfolio.id, name="VWCE",
            instrument_type="etf", valuation_currency_code="USD",
            is_active=True,
        )
        db.session.add(instrument)
        db.session.flush()
        registration = PositionRegistration(
            account_id=account.id, instrument_id=instrument.id,
            tracking_mode="statement_valued", opening_date=date(2026, 6, 2),
        )
        db.session.add(registration)
        db.session.flush()
        db.session.add(ValuationObservation(
            position_registration_id=registration.id,
            effective_date=date(2026, 6, 2),
            native_value_amount=Decimal("10000"), currency_code="USD",
        ))
        db.session.commit()
        return portfolio.id


def _valid_backup_upload(app: Flask) -> bytes:
    with app.app_context():
        blob = export_backup()
        return blob if isinstance(blob, bytes) else blob.encode("utf-8")


# --- Settings page ---




# --- Restore validation presentation ---

def test_restore_upload_error_focus_and_truthful_counts(
    client: FlaskClient, app: Flask
) -> None:
    _portfolio(app)
    response = client.post("/backup/preview", data={})
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    summary = re.search(
        r'<div class="error-summary" role="alert">.*?</div>', body, re.S
    ).group(0)
    assert 'href="#backup_file"' in summary
    field = re.search(r'<input[^>]*id="backup_file"[^>]*>', body).group(0)
    assert 'aria-invalid="true"' in field
    assert 'aria-describedby="backup_file-error"' in field
    assert "autofocus" in field
    # A failed upload must not zero the stated database contents.
    assert "6 records across 22 backed-up tables" in body
    assert "0 source records" not in body
    assert_no_inline_script(body)


def _stage_backup(client: FlaskClient, blob: bytes) -> tuple[str, str]:
    response = client.post(
        "/backup/preview",
        data={"backup_file": (io.BytesIO(blob), "lookthrough-backup-2026-08-02.json")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    token = re.search(r'name="restore_token"[^>]*value="([^"]+)"', body).group(1)
    return token, body


def test_restore_preview_counts_and_confirm_controls(
    client: FlaskClient, app: Flask
) -> None:
    _portfolio(app)
    blob = _valid_backup_upload(app)
    token, body = _stage_backup(client, blob)
    assert token
    assert "<h1>Confirm restore</h1>" in body
    assert "Validated backup" in body
    assert "The active database is still unchanged" in body
    assert "<strong>6 source records</strong>" in body
    # Per-table counts are readable, title-cased rows.
    assert "Position Registrations" in body
    # Explicit confirmation is its own labelled checkbox with equal-weight exits.
    assert "I understand this will replace the current local data" in body
    # The checkbox is required backend-side, and the label says so in text.
    checkbox_label = re.search(
        r'<label for="confirm_restore">(.*?)</label>', body, re.S
    ).group(1)
    assert "(required)" in checkbox_label
    assert "Restore this backup" in body
    assert 'name="cancel"' in body
    assert_no_inline_script(body)


def test_restore_requires_explicit_confirmation_with_focus(
    client: FlaskClient, app: Flask
) -> None:
    _portfolio(app)
    blob = _valid_backup_upload(app)
    token, _ = _stage_backup(client, blob)
    response = client.post("/backup/restore", data={"restore_token": token})
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert "Confirm before restoring the backup." in body
    field = re.search(r'<input[^>]*id="confirm_restore"[^>]*>', body).group(0)
    assert 'aria-invalid="true"' in field
    assert "autofocus" in field

    # Cancel exits cleanly back to Settings.
    cancelled = client.post(
        "/backup/restore", data={"restore_token": token, "cancel": "1"}
    )
    assert cancelled.status_code == 302
    assert cancelled.headers["Location"].endswith("/settings")


# --- Contextual CSV entry points ---
