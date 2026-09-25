"""Historical quantity invariants and concurrent ledger commands."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import date
from decimal import Decimal
import sqlite3
from threading import Event, Lock

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from app.extensions import db
from app.models import PositionRegistration, Transaction
from app.services.activity import ActivityValidationError, post_trade, preview_trade
from app.services.activity_history import ReversalCommand, reverse_activity
from app.services.cash import resolve_cash
from app.services.dividends import post_dividend
from app.services.fixed_deposits import FixedDepositValidationError, post_maturity_disposition
from app.services.ledger_writes import BUSY_MESSAGE
from app.services.positions import quantity_as_of
from test_activity_service import _base_records, _command, TRADE_DATE
from test_activity_routes import _trade_data
from test_dividend_service import _records as dividend_records, _command as dividend_command
from test_fixed_deposit_dispositions import _records as fd_records, _command as fd_command


JAN = date(2026, 1, 1)
FEB = date(2026, 2, 1)
MAR = date(2026, 3, 1)
APR = date(2026, 4, 1)


def trade(records, kind, units, day=TRADE_DATE):
    return replace(_command(*records, activity_type=kind, quantity=units, price="10"),
                   effective_date=day)


@pytest.mark.parametrize("mode", ["entered", "entire_holding"])
@pytest.mark.parametrize("later_recovery", [False, True])
def test_backdated_sale_checks_every_later_effect_without_partial_writes(app, mode, later_recovery):
    with app.app_context():
        records = _base_records()
        buy = post_trade(trade(records, "buy", "100", JAN))
        post_trade(trade(records, "sell", "80", MAR))
        if later_recovery:
            post_trade(trade(records, "buy", "100", APR))
        count = db.session.scalar(select(func.count(Transaction.id)))
        cash = resolve_cash(records[0].id, records[1].id, "EUR", APR).amount
        command = replace(trade(records, "sell", "50", FEB), quantity_mode=mode,
                          quantity=None if mode == "entire_holding" else Decimal("50"))
        for action in (preview_trade, post_trade):
            with pytest.raises(ActivityValidationError, match="negative holding on 2026-03-01") as error:
                action(command)
            assert error.value.field == ("quantity_mode" if mode == "entire_holding" else "quantity")
        assert db.session.scalar(select(func.count(Transaction.id))) == count
        registration = db.session.get(PositionRegistration, buy.registration_id)
        assert quantity_as_of(registration, MAR) == Decimal("20")
        assert quantity_as_of(registration, APR) == Decimal("120" if later_recovery else "20")
        assert resolve_cash(records[0].id, records[1].id, "EUR", APR).amount == cash


def test_backdated_sale_allows_valid_later_history_and_ignores_reversals(app):
    with app.app_context():
        records = _base_records()
        buy = post_trade(trade(records, "buy", "100", JAN))
        sale = post_trade(trade(records, "sell", "80", MAR))
        reverse_activity(ReversalCommand(records[0].id, sale.transaction_id, "Correction"))
        post_trade(trade(records, "sell", "60", MAR))
        post_trade(trade(records, "sell", "40", FEB))
        assert quantity_as_of(db.session.get(PositionRegistration, buy.registration_id), MAR) == 0


def test_new_sale_follows_existing_same_day_entries_and_respects_later_sales(app):
    with app.app_context():
        records = _base_records()
        buy = post_trade(trade(records, "buy", "100", JAN))
        post_trade(trade(records, "sell", "80", FEB))
        post_trade(trade(records, "buy", "30", FEB))
        post_trade(trade(records, "sell", "40", MAR))
        with pytest.raises(ActivityValidationError, match="later recorded sales"):
            post_trade(trade(records, "sell", "20", FEB))
        post_trade(trade(records, "sell", "10", FEB))
        assert quantity_as_of(db.session.get(PositionRegistration, buy.registration_id), MAR) == 0


def concurrent_commands(app, action, other=None):
    """Start a second connection while the first holds its write reservation."""
    second_attempt = Event()
    counter_lock = Lock()
    attempts = 0
    with app.app_context():
        engine = db.engine

    def before(connection, cursor, statement, parameters, context, executemany):
        nonlocal attempts
        if statement == "BEGIN IMMEDIATE":
            with counter_lock:
                attempts += 1
                connection.info["ledger_test_attempt"] = attempts
                if attempts == 2:
                    second_attempt.set()

    def after(connection, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE" and connection.info["ledger_test_attempt"] == 1:
            assert second_attempt.wait(5), "Contending request never attempted its reservation"

    def run(command):
        with app.app_context():
            try:
                return command()
            except (ActivityValidationError, FixedDepositValidationError) as error:
                return error

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = executor.submit(run, action), executor.submit(run, other or action)
            results = first.result(timeout=15), second.result(timeout=15)
    finally:
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)
    assert attempts == 2
    return results


@pytest.mark.parametrize("mode", ["entered", "entire_holding"])
def test_concurrent_sales_revalidate_after_other_commit(app, mode):
    with app.app_context():
        records = _base_records()
        buy = post_trade(trade(records, "buy", "100"))
        command = replace(trade(records, "sell", "80"), quantity_mode=mode,
                          quantity=None if mode == "entire_holding" else Decimal("80"))
        registration_id = buy.registration_id
    results = concurrent_commands(app, lambda: post_trade(command))
    assert sum(isinstance(result, ActivityValidationError) for result in results) == 1
    with app.app_context():
        assert db.session.scalar(select(func.count(Transaction.id))) == 2
        assert quantity_as_of(db.session.get(PositionRegistration, registration_id), TRADE_DATE) == (
            Decimal("20") if mode == "entered" else Decimal("0"))


def test_concurrent_reversals_create_only_one_audit_reversal(app):
    with app.app_context():
        records = _base_records()
        buy = post_trade(trade(records, "buy", "100"))
        command = ReversalCommand(records[0].id, buy.transaction_id, "Correction")
    results = concurrent_commands(app, lambda: reverse_activity(command))
    errors = [result for result in results if isinstance(result, ActivityValidationError)]
    assert len(errors) == 1 and "already been reversed" in str(errors[0])
    with app.app_context():
        assert db.session.scalar(select(func.count(Transaction.id))) == 2


def test_sale_and_reversal_of_its_source_cannot_both_commit(app):
    with app.app_context():
        records = _base_records()
        buy = post_trade(trade(records, "buy", "100"))
        sale = trade(records, "sell", "80")
        reversal = ReversalCommand(records[0].id, buy.transaction_id, "Correction")
    results = concurrent_commands(app, lambda: post_trade(sale), lambda: reverse_activity(reversal))
    assert sum(isinstance(result, ActivityValidationError) for result in results) == 1
    with app.app_context():
        assert db.session.scalar(select(func.count(Transaction.id))) == 2
        assert quantity_as_of(db.session.get(PositionRegistration, buy.registration_id), TRADE_DATE) in {
            Decimal("0"), Decimal("20")}


def test_concurrent_reversals_of_linked_dividend_members_keep_group_atomic(app):
    with app.app_context():
        records = dividend_records()
        posted = post_dividend(dividend_command(*records[:3], outcome="reinvest_same",
                               reinvestment_quantity=Decimal("10"),
                               reinvestment_unit_price=Decimal("10")))
        first = ReversalCommand(records[0].id, posted.dividend_transaction_id, "Correction")
        second = replace(first, transaction_id=posted.buy_transaction_id)
    results = concurrent_commands(app, lambda: reverse_activity(first), lambda: reverse_activity(second))
    assert sum(isinstance(result, ActivityValidationError) for result in results) == 1
    with app.app_context():
        assert db.session.scalar(select(func.count(Transaction.id))) == 4
        assert db.session.get(Transaction, posted.dividend_transaction_id).status == "reversed"
        assert db.session.get(Transaction, posted.buy_transaction_id).status == "reversed"


def test_concurrent_maturity_dispositions_do_not_credit_principal_twice(app):
    with app.app_context():
        portfolio, registration, cash_account = fd_records()
        command = fd_command(portfolio, registration, cash_account)
    results = concurrent_commands(app, lambda: post_maturity_disposition(command))
    assert sum(isinstance(result, FixedDepositValidationError) for result in results) == 1
    with app.app_context():
        assert db.session.scalar(select(func.count(Transaction.id))) == 1


def test_reversal_refreshes_object_preloaded_before_another_request_commits(app):
    with app.app_context():
        records = _base_records()
        buy = post_trade(trade(records, "buy", "100"))
        command = ReversalCommand(records[0].id, buy.transaction_id, "Correction")
        cached = db.session.get(Transaction, buy.transaction_id)
        with app.app_context():
            reverse_activity(command)
        assert cached.status == "posted"  # The outer identity map is stale.
        with pytest.raises(ActivityValidationError, match="already been reversed"):
            reverse_activity(command)
        assert db.session.scalar(select(func.count(Transaction.id))) == 2


@pytest.mark.parametrize("operation", ["trade", "reversal", "dividend", "maturity"])
def test_busy_database_gives_recoverable_service_error_without_writes(app, database_path, operation):
    with app.app_context():
        if operation == "maturity":
            records = fd_records()
            command = fd_command(*records)
            action, error_type = post_maturity_disposition, FixedDepositValidationError
        elif operation == "dividend":
            records = dividend_records()
            command = dividend_command(*records[:3], outcome="reinvest_same",
                                       reinvestment_quantity=Decimal("10"),
                                       reinvestment_unit_price=Decimal("10"))
            action, error_type = post_dividend, ActivityValidationError
        else:
            records = _base_records()
            bought = post_trade(trade(records, "buy", "100"))
            command = (ReversalCommand(records[0].id, bought.transaction_id, "Correction")
                       if operation == "reversal" else trade(records, "sell", "80"))
            action, error_type = (reverse_activity if operation == "reversal" else post_trade), ActivityValidationError
        count = db.session.scalar(select(func.count(Transaction.id)))
        db.session.connection().exec_driver_sql("PRAGMA busy_timeout = 0")
        with closing(sqlite3.connect(database_path)) as external:
            external.execute("BEGIN IMMEDIATE")
            with pytest.raises(error_type, match="Another save is using this database"):
                action(command)
            external.rollback()
        assert db.session.scalar(select(func.count(Transaction.id))) == count
        action(command)  # Same entered action is usable after the other writer finishes.


def test_existing_pending_work_is_preserved_inside_ledger_commit(app):
    with app.app_context():
        records = _base_records()
        records[1].name = "Updated account name"
        db.session.flush()  # A caller already has a real SQLite transaction.
        result = post_trade(trade(records, "buy", "10"))
        assert result.preview.account_name == "Updated account name"
        db.session.expire_all()
        assert records[1].name == "Updated account name"


def test_non_lock_database_errors_are_not_reported_as_busy(app, monkeypatch):
    with app.app_context():
        records = _base_records()
        command = trade(records, "buy", "10")
        def fail():
            raise OperationalError("INSERT", {}, sqlite3.OperationalError("synthetic disk failure"))
        monkeypatch.setattr(db.session, "commit", fail)
        with pytest.raises(OperationalError, match="synthetic disk failure"):
            post_trade(command)
        assert db.session.scalar(select(func.count(Transaction.id))) == 0


def test_busy_route_keeps_form_and_successful_retry(app, client, database_path):
    with app.app_context():
        records = _base_records()
        post_trade(trade(records, "buy", "100"))
        data = _trade_data(records[1].id, records[2].id, activity_type="sell",
                           quantity="80", unit_price="10", fee_amount="0")
        engine = db.engine
    def no_wait(connection, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE":
            cursor.execute("PRAGMA busy_timeout = 0")
    event.listen(engine, "before_cursor_execute", no_wait)
    try:
        with closing(sqlite3.connect(database_path)) as external:
            external.execute("BEGIN IMMEDIATE")
            response = client.post("/activity/new", data=data)
            assert response.status_code == 200
            html = response.get_data(as_text=True)
            assert BUSY_MESSAGE in html and 'href="#account_id"' in html
            assert 'value="80"' in html
            external.rollback()
        assert client.post("/activity/new", data=data).status_code == 302
    finally:
        event.remove(engine, "before_cursor_execute", no_wait)
