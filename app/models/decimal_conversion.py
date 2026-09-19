"""Immutable source-value conversion evidence, retained in full backups."""
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.extensions import db
from app.models.types import UTCDateTime
from app.conventions import utc_now
from datetime import datetime


class DecimalConversion(db.Model):
    __tablename__ = 'decimal_conversions'

    id: Mapped[int] = mapped_column(primary_key=True)
    source_table: Mapped[str] = mapped_column(String(80))
    source_record_id: Mapped[int] = mapped_column()
    field_name: Mapped[str] = mapped_column(String(80))
    before_value: Mapped[str] = mapped_column(Text)
    after_value: Mapped[str] = mapped_column(Text)
    currency_code: Mapped[str | None] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
