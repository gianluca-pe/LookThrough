"""Disposable synthetic browser fixture for retirement verification.

Run:.venv/bin/python tests/retirement_demo.py --state funded
Never opens or migrates the owner's database. Ctrl-C removes the temporary fixture.
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
from app.models import FxRate
from app.services.retirement_plans import save_income, save_plan
from test_m5_retirement import AS_OF, _portfolio_account, _cash, _position, _assumptions
from test_two_tier_retirement import plan_data, income_data


def seed_demo(state):
    portfolio, account = _portfolio_account(spending="60000", inflation="0.025")
    portfolio.name = "Retirement demo — synthetic data"
    if state == "surplus":
        _cash(account, "1000000")
        save_plan(portfolio.id, plan_data(
            current_age_years=70, withdrawal_start_age_years=70, final_age_years=90,
            core_amount=Decimal("30000"), flexible_amount=Decimal("20000"),
            upper_multiplier_decimal=Decimal("0.5"),
            terminal_legacy_target_amount=Decimal("0"), terminal_legacy_target_currency_code="USD"), confirmed=True)
        db.session.commit()
        return
    _cash(account, "5000" if state == "shortfall" else "80000")
    _position(portfolio, account, name="Mixed retirement fund", amount="5000" if state == "shortfall" else "1950000",
              bucket="growth", roles={"equity": "0.7", "income": "0.3"})
    _assumptions(portfolio)
    if state != "legacy":
        save_plan(portfolio.id, plan_data(core_amount=Decimal("42000"), flexible_amount=Decimal("18000"),
                  core_inflation_decimal=Decimal("0.025"), flexible_inflation_decimal=Decimal("0.025"),
                  equity_return_decimal=Decimal("0.05"), income_return_decimal=Decimal("0.02"),
                  liquidity_return_decimal=Decimal("0.01"), alternatives_return_decimal=Decimal("0.02"),
                  current_age_years=50, withdrawal_start_age_years=52, final_age_years=93,
                  lower_multiplier_decimal=Decimal("1.1"), middle_multiplier_decimal=Decimal("1"),
                  upper_multiplier_decimal=Decimal("0.25"),
                  terminal_legacy_target_amount=Decimal("100000"), terminal_legacy_target_currency_code="USD"), confirmed=True)
        save_income(portfolio.id, income_data(name="Example pension", annual_amount=Decimal("12000"),
                    currency_code="GBP" if state in {"missing", "stale"} else "USD",
                    start_date=date(2043, 8, 30), inflation_decimal=Decimal("0.02")))
    if state == "stale":
        db.session.add(FxRate(base_currency_code="GBP", quote_currency_code="USD",
                             effective_date=date(2026, 8, 1), quote_per_base_amount=Decimal("1.25")))
    if state == "restricted":
        account.present_access_decimal = Decimal("0")
    db.session.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", choices=["funded", "shortfall", "missing", "stale", "restricted", "legacy", "surplus"], default="funded")
    parser.add_argument("--port", type=int, default=5185)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="lookthrough-retirement-demo-") as directory:
        app = create_app({"TESTING": False, "SECRET_KEY": "synthetic-retirement-demo",
                          "SQLALCHEMY_DATABASE_URI": f"sqlite:///{directory}/demo.sqlite3",
                          "RESTORE_STAGING_DIRECTORY": f"{directory}/restore",
                          "CURRENT_DATE_PROVIDER": lambda: AS_OF})
        with app.app_context():
            db.create_all()
            seed_demo(args.state)
        app.run(host="127.0.0.1", port=args.port, use_reloader=False)
