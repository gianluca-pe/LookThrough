"""Database types shared by current domain models."""

from __future__ import annotations

from datetime import UTC, datetime

from decimal import Decimal

from sqlalchemy import BigInteger, DateTime
from app.decimal_policy import exact_units
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Persist aware audit timestamps as UTC and restore UTC awareness.

    SQLite stores datetimes without timezone metadata. Normalizing to naive UTC on
    write and restoring the UTC marker on read keeps the application contract
    explicit without treating a local timezone as authoritative.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("audit timestamps must be timezone-aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class ScaledDecimal(TypeDecorator[Decimal]):
    """Exact fixed-scale Decimal backed by a checked signed 64-bit integer."""

    impl = BigInteger
    cache_ok = True

    def __init__(self, places):
        super().__init__()
        self.places = places

    @property
    def python_type(self):
        return Decimal

    def process_bind_param(self, value, dialect):
        return None if value is None else exact_units(value, self.places)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if type(value) is not int:
            raise ValueError('Expected exact integer storage; this database needs an upgrade.')
        return Decimal(value).scaleb(-self.places)
