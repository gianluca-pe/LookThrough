"""Immutable derived portfolio snapshots for honest point-in-time comparison."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.conventions import utc_now
from app.extensions import db
from app.models.types import UTCDateTime


SNAPSHOT_STATUS_CODES = ("current", "stale", "partial", "missing", "empty")


class PortfolioSnapshot(db.Model):
    """A frozen shared-summary result, not an editable valuation source."""

    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id"), nullable=False, index=True
    )
    as_of_date: Mapped[date] = mapped_column(nullable=False, index=True)
    reporting_currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    summary_status: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    portfolio: Mapped["Portfolio"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "portfolio_id", "as_of_date", name="uq_snapshot_portfolio_as_of"
        ),
        CheckConstraint(
            "summary_status IN ('current','stale','partial','missing','empty')",
            name="snapshot_status_supported",
        ),
    )
