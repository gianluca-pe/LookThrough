"""Immutable named comparison runs, separate from the adopted spending plan."""

from datetime import date, datetime

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.conventions import utc_now
from app.extensions import db
from app.models.types import UTCDateTime


class RetirementScenario(db.Model):
    __tablename__ = "retirement_scenarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    as_of_date: Mapped[date] = mapped_column()
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
