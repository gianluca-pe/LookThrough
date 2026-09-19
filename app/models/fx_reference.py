"""Dated EUR reference observations, retained when explicitly corrected."""
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.conventions import utc_now
from app.extensions import db
from app.models.types import UTCDateTime


class FxReferenceSet(db.Model):
    __tablename__ = "fx_reference_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    effective_date: Mapped[date] = mapped_column(nullable=False, index=True)
    rates_json: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    source_note: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    __table_args__ = (
        CheckConstraint("source IN ('banca_italia', 'manual')", name="fx_reference_source"),
    )
