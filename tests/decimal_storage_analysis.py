"""Disposable SQLite numeric-versus-integer representation experiments.

Run explicitly: .venv/bin/python -m pytest -q tests/decimal_storage_analysis.py
These test the precision rationale; they never migrate application or user data."""

from decimal import Decimal
import json
import sqlite3

import pytest
from sqlalchemy import Column, Integer, MetaData, Numeric, Table, create_engine, func, select


@pytest.fixture
def legacy():
    engine = create_engine("sqlite://")
    table = Table("samples", MetaData(), Column("id", Integer, primary_key=True),
                  Column("amount", Numeric(28, 12)))
    table.metadata.create_all(engine)
    with engine.begin() as connection:
        yield connection, table
    engine.dispose()


def test_old_reader_and_sql_cast_are_different_migration_policies(legacy):
    connection, table = legacy
    connection.execute(table.insert(), {"amount": Decimal("100000.01")})
    old_reader = connection.scalar(select(table.c.amount))
    raw, storage, cast = connection.exec_driver_sql(
        "SELECT amount, typeof(amount), CAST(amount AS TEXT) FROM samples").one()
    assert storage == "real"
    assert old_reader == Decimal("100000.009999999995")
    assert Decimal(cast) == Decimal("100000.01")
    assert Decimal.from_float(raw) == Decimal("100000.009999999994761310517787933349609375")
    assert len({old_reader, Decimal(cast), Decimal.from_float(raw)}) == 3


def test_decimal_replay_changes_sql_sum_without_changing_sources(legacy):
    connection, table = legacy
    connection.execute(table.insert(), [{"amount": Decimal("100000.01")} for _ in range(3)])
    values = list(connection.scalars(select(table.c.amount)))
    sql_sum = connection.scalar(select(func.sum(table.c.amount)))
    decimal_sum = sum(values, Decimal("0"))
    assert sql_sum == Decimal("300000.029999999970")
    assert decimal_sum == Decimal("300000.029999999985")
    assert sql_sum != decimal_sum


@pytest.mark.parametrize("text", [
    "100000.01", "0.123456789123", "-100000.009999999995",
    "9999999999999999.999999999999", "0.0000000000001",
])
def test_text_candidate_and_json_preserve_decimal_values(text):
    value = Decimal(text)
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE candidate (amount TEXT NOT NULL)")
        connection.execute("INSERT INTO candidate VALUES (?)", (format(value, "f"),))
        stored, storage = connection.execute("SELECT amount, typeof(amount) FROM candidate").fetchone()
        assert storage == "text"
        assert Decimal(stored) == value
        assert Decimal(json.loads(json.dumps({"amount": stored}))["amount"]) == value


def test_preserving_legacy_reader_values_needs_no_guessing(legacy):
    connection, table = legacy
    connection.execute(table.insert(), {"amount": Decimal("100000.01")})
    before = connection.scalar(select(table.c.amount))
    connection.exec_driver_sql("CREATE TABLE candidate (amount TEXT)")
    connection.exec_driver_sql("INSERT INTO candidate VALUES (?)", (format(before, "f"),))
    after = Decimal(connection.exec_driver_sql("SELECT amount FROM candidate").scalar_one())
    assert after == before
    assert after != Decimal("100000.01")


def test_plain_text_comparison_is_not_a_financial_constraint():
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE candidate (amount TEXT)")
        connection.execute("INSERT INTO candidate VALUES ('10')")
        assert connection.execute("SELECT amount < '2' FROM candidate").fetchone()[0] == 1
        assert not Decimal("10") < Decimal("2")


def test_scaled_int64_cannot_hold_existing_declared_money_range():
    maximum = Decimal("9999999999999999.999999999999")
    scaled = int(maximum * Decimal("1000000000000"))
    assert scaled > 2**63 - 1
    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(OverflowError):
            connection.execute("SELECT ?", (scaled,))


@pytest.mark.parametrize("raw,places,expected", [
    ("100000.01", 2, "100000.01"),
    ("100000.001", 3, "100000.001"),
    ("123.4567", 6, "123.456700"),
    ("0.123456", 6, "0.123456"),
    ("100000.01", 6, "100000.010000"),
    ("1.005", 2, "1.01"),
])
def test_proposed_business_rounding_and_scaled_integer_storage(legacy, raw, places, expected):
    from decimal import ROUND_HALF_UP

    connection, table = legacy
    connection.execute(table.insert(), {"amount": Decimal(raw)})
    before = connection.scalar(select(table.c.amount))
    quantum = Decimal(1).scaleb(-places)
    rounded = before.quantize(quantum, rounding=ROUND_HALF_UP)
    units = int(rounded.scaleb(places))
    connection.exec_driver_sql("CREATE TABLE candidate (amount INTEGER NOT NULL)")
    connection.exec_driver_sql("INSERT INTO candidate VALUES (?)", (units,))
    stored, storage = connection.exec_driver_sql("SELECT amount, typeof(amount) FROM candidate").one()
    assert storage == "integer"
    assert Decimal(stored).scaleb(-places) == Decimal(expected)
