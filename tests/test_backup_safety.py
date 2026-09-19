"""Consistent recovery artifacts and complete classification role sets."""

from datetime import timedelta
from decimal import Decimal
import io
import json
import sqlite3

import pytest
from sqlalchemy import event, select

from app.extensions import db
from app.models import InstrumentClassification, Portfolio
from app.services import activity
from app.services.backup import (
    BackupValidationError, export_backup, load_staged_backup, restore_backup,
    stage_backup, validate_backup,
)
from app.services.classification import classifications_as_of
from app.services.retirement_affordability import prepare_affordability
from test_activity_service import _base_records, _command
from test_m5_retirement import AS_OF, _position
from test_retirement_affordability import inputs
from test_two_tier_retirement import seed


def test_export_keeps_snapshot_when_trade_commits_between_tables(app):
    with app.app_context():
        records = _base_records()
        command = _command(*records, activity_type="buy", quantity="100", price="10")
        activity.post_trade(command)
        engine = db.engine
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar() == "wal"
        before = json.loads(export_backup())["tables"]
        triggered = False

        def concurrent_trade(connection, cursor, statement, parameters, context, executemany):
            nonlocal triggered
            if statement.startswith("SELECT postings.") and not triggered:
                triggered = True
                with app.app_context():
                    activity.post_trade(command)

        event.listen(engine, "before_cursor_execute", concurrent_trade)
        try:
            blob = export_backup()
        finally:
            event.remove(engine, "before_cursor_execute", concurrent_trade)
        assert triggered
        assert json.loads(blob)["tables"] == before
        validate_backup(blob)
        assert len(json.loads(export_backup())["tables"]["transactions"]) == 2


def test_export_blocks_commit_in_rollback_journal_and_releases_reader(app, database_path):
    with app.app_context():
        _base_records()
        engine = db.engine
        attempted = False

        def concurrent_write(connection, cursor, statement, parameters, context, executemany):
            nonlocal attempted
            if statement.startswith("SELECT postings.") and not attempted:
                attempted = True
                with sqlite3.connect(database_path, timeout=0) as writer:
                    writer.execute("UPDATE portfolios SET name='Concurrent save'")
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        writer.commit()
                    writer.rollback()

        event.listen(engine, "before_cursor_execute", concurrent_write)
        try:
            blob = export_backup()
        finally:
            event.remove(engine, "before_cursor_execute", concurrent_write)
        assert attempted
        validate_backup(blob)
        assert json.loads(blob)["tables"]["portfolios"][0]["name"] == "Portfolio"
        with sqlite3.connect(database_path, timeout=0) as writer:
            writer.execute("UPDATE portfolios SET name='Retried save'")
        assert json.loads(export_backup())["tables"]["portfolios"][0]["name"] == "Retried save"


def test_export_does_not_flush_or_rollback_callers_work(app):
    with app.app_context():
        portfolio, _, _ = _base_records()
        portfolio.name = "Pending name"
        assert json.loads(export_backup())["tables"]["portfolios"][0]["name"] == "Portfolio"
        db.session.flush()
        assert json.loads(export_backup())["tables"]["portfolios"][0]["name"] == "Portfolio"
        db.session.commit()
        assert json.loads(export_backup())["tables"]["portfolios"][0]["name"] == "Pending name"


def test_failed_export_releases_snapshot(app, database_path):
    with app.app_context():
        _base_records()
        engine = db.engine

        def fail(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("SELECT postings."):
                raise RuntimeError("Synthetic read failure")

        event.listen(engine, "before_cursor_execute", fail)
        try:
            with pytest.raises(RuntimeError, match="Synthetic read failure"):
                export_backup()
        finally:
            event.remove(engine, "before_cursor_execute", fail)
        with sqlite3.connect(database_path, timeout=0) as writer:
            writer.execute("UPDATE portfolios SET name='After failure'")
        validate_backup(export_backup())


def classified_payload():
    portfolio, account, _ = seed("0")
    _position(portfolio, account, name="Synthetic equity", amount="1000",
              bucket="growth", roles={"equity": "1"})
    db.session.commit()
    return portfolio, json.loads(export_backup())


@pytest.mark.parametrize("weight", ["0.5", "0.999998", "1.000002"])
@pytest.mark.parametrize("historical", [False, True])
def test_restore_rejects_invalid_dated_sets_without_changing_data(app, weight, historical):
    with app.app_context():
        _, payload = classified_payload()
        original = export_backup()
        row = payload["tables"]["instrument_classifications"][0]
        if historical:
            row = dict(row, id=row["id"] + 1,
                       effective_date=(AS_OF - timedelta(days=1)).isoformat())
            payload["tables"]["instrument_classifications"].append(row)
        row["weight_decimal"] = weight
        with pytest.raises(BackupValidationError, match="classification dated"):
            validate_backup(json.dumps(payload).encode())
        assert json.loads(export_backup())["tables"] == json.loads(original)["tables"]


@pytest.mark.parametrize("second_weight", ["0.4", "0.6"])
def test_restore_rejects_split_totals_even_when_each_row_is_valid(app, second_weight):
    with app.app_context():
        _, payload = classified_payload()
        row = payload["tables"]["instrument_classifications"][0]
        row["weight_decimal"] = "0.5"
        payload["tables"]["instrument_classifications"].append(
            dict(row, id=row["id"] + 1, economic_role_code="income", weight_decimal=second_weight))
        with pytest.raises(BackupValidationError, match="weights must total 100%"):
            validate_backup(json.dumps(payload).encode())


@pytest.mark.parametrize("roles", [
    {"growth": "0.5", "opportunistic": "0.25", "ballast": "0.25"},
    {"equity": "0.999999"}, {"equity": "0.5", "income": "0.500001"}, {},
])
def test_restore_preserves_legacy_tolerance_and_unclassified_sources(app, roles):
    with app.app_context():
        portfolio, payload = classified_payload()
        portfolio_id = portfolio.id
        template = payload["tables"]["instrument_classifications"][0]
        payload["tables"]["instrument_classifications"] = [
            dict(template, id=index + 1, economic_role_code=role, weight_decimal=weight)
            for index, (role, weight) in enumerate(roles.items())
        ]
        validated = validate_backup(json.dumps(payload).encode())
        restore_backup(validated)
        restored_roles = {
            row.economic_role_code: row.weight_decimal
            for row in db.session.scalars(select(InstrumentClassification))
        }
        assert restored_roles == {role: Decimal(weight) for role, weight in roles.items()}
        basis, _ = prepare_affordability(db.session.get(Portfolio, portfolio_id), inputs())
        assert basis["calculation_complete"] == bool(roles)


@pytest.mark.parametrize("weight", ["0.5", "0.999998"])
def test_invalid_latest_stored_set_withholds_complete_retirement_result(app, weight):
    with app.app_context():
        portfolio, _ = classified_payload()
        row = db.session.scalar(select(InstrumentClassification))
        db.session.add(InstrumentClassification(
            instrument_id=row.instrument_id, economic_role_code="equity",
            weight_decimal=Decimal("1"), effective_date=AS_OF - timedelta(days=1)))
        row.weight_decimal = Decimal(weight)
        db.session.commit()
        assert classifications_as_of([row.instrument_id], AS_OF) == {}
        assert classifications_as_of([row.instrument_id], AS_OF - timedelta(days=1))
        basis, summary = prepare_affordability(portfolio, inputs())
        assert basis["calculation_complete"] is False
        assert summary["holdings"][0]["included_reporting_amount"] == Decimal("1000")
        assert not summary["holdings"][0]["role_weights"]


def test_invalid_upload_is_recoverable_and_never_staged(app, client, tmp_path):
    with app.app_context():
        _, payload = classified_payload()
        original = json.loads(export_backup())["tables"]
    payload["tables"]["instrument_classifications"][0]["weight_decimal"] = "0.5"
    response = client.post("/backup/preview", data={
        "backup_file": (io.BytesIO(json.dumps(payload).encode()), "invalid.json")})
    assert response.status_code == 400
    assert b"Allocation role weights must total 100%" in response.data
    assert b'href="#backup_file"' in response.data
    assert b'aria-invalid="true"' in response.data
    assert not list((tmp_path / "restore-staging").glob("*"))
    with app.app_context():
        assert json.loads(export_backup())["tables"] == original


def test_staged_backup_is_revalidated_before_restore(app, tmp_path):
    with app.app_context():
        _, payload = classified_payload()
        directory = tmp_path / "restore-staging"
        token = stage_backup(validate_backup(json.dumps(payload).encode()), directory)
        payload["tables"]["instrument_classifications"][0]["weight_decimal"] = "0.5"
        (directory / f"{token}.json").write_text(json.dumps(payload))
        with pytest.raises(BackupValidationError, match="classification dated"):
            load_staged_backup(token, directory)
