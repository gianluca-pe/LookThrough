"""Synthetic Overview review:.venv/bin/python tests/funding_demo.py --state surplus.

Uses a disposable database only. No owner's records are opened or changed.
"""
import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from app.extensions import db
from app.services.retirement_plans import save_plan
from test_m5_funding import AS_OF, _portfolio_account, _cash, _position
from test_two_tier_retirement import plan_data


def seed_demo(state):
    portfolio, account = _portfolio_account(spending="30000", inflation="0")
    portfolio.name = "Overview funding — synthetic data"
    save_plan(portfolio.id, plan_data(
        base_date=AS_OF, core_amount=Decimal("20000"), flexible_amount=Decimal("10000"),
        core_inflation_decimal=Decimal('.03'), flexible_inflation_decimal=Decimal('.05'),
        final_age_years=90,
    ), confirmed=True)
    if state != "partial":
        _cash(account, "0")
    for bucket, amount in (("now", "150000"), ("bridge", "240000"), ("growth", "900000")):
        if state == "growth" and bucket != "growth":
            amount = "30000"
        _position(portfolio, account, name=f"{bucket.capitalize()} reserve", amount=amount,
                  bucket=bucket, roles={"equity" if bucket == "growth" else "income": "1"},
                  maturity_date=date(2030, 8, 29) if state == "timing" and bucket == "now" else None)
    db.session.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", choices=["surplus", "growth", "partial", "timing"], default="surplus")
    parser.add_argument("--port", type=int, default=5191)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="lookthrough-funding-demo-") as directory:
        app = create_app({
            "SECRET_KEY": "synthetic-funding-demo",
            "TEMPLATES_AUTO_RELOAD": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{directory}/demo.sqlite3",
            "RESTORE_STAGING_DIRECTORY": f"{directory}/restore",
            "CURRENT_DATE_PROVIDER": lambda: AS_OF,
        })
        with app.app_context():
            db.create_all()
            seed_demo(args.state)
        app.run(host="127.0.0.1", port=args.port, use_reloader=False)
