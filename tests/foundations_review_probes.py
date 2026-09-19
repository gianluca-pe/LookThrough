"""Exact quantity persistence probe complementing test_exact_decimals.py.

Run explicitly: .venv/bin/python -m pytest -q tests/foundations_review_probes.py
All databases are disposable pytest fixtures. This standalone reproduction is
retained alongside the broader ordinary ledger, backup and Host regression tests."""

from decimal import Decimal

from sqlalchemy import select

from app.extensions import db
from app.models import Posting
from app.services import activity
from test_activity_service import _base_records, _command


def test_f04_decimal_quantity_survives_storage_exactly(app):
    with app.app_context():
        portfolio, account, instrument = _base_records()
        activity.post_trade(_command(portfolio, account, instrument,
                                     activity_type="buy", quantity="100000.01", price="1"))
        quantity = db.session.scalar(select(Posting.quantity_delta).where(
            Posting.posting_kind == "instrument"))
        assert quantity == Decimal("100000.01"), f"Storage changed the quantity to {quantity}"
        # Selling the exact stored quantity must not raise a false oversell error.
        activity.preview_trade(_command(portfolio, account, instrument,
                                        activity_type="sell", quantity="100000.01", price="1"))
