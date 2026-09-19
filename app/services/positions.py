"""Position registration and quantity replay."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.extensions import db
from app.models import PositionRegistration, Posting, Transaction


def quantity_as_of(registration: PositionRegistration, as_of_date: date) -> Decimal | None:
    """Replay posted instrument quantity effects for a transaction-tracked position."""

    if registration.tracking_mode != "transaction_tracked":
        return None

    quantities = db.session.scalars(
        select(Posting.quantity_delta)
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(
            Posting.account_id == registration.account_id,
            Posting.instrument_id == registration.instrument_id,
            Posting.posting_kind == "instrument",
            Transaction.status == "posted",
            Transaction.reverses_transaction_id.is_(None),
            Transaction.effective_date <= as_of_date,
        )
    )
    return sum((value for value in quantities if value is not None), Decimal("0"))
