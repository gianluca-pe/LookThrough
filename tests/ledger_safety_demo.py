"""Disposable ledger browser fixture on 127.0.0.1:5195; CSRF stays enabled."""

from datetime import date
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from app.extensions import db
from app.services.activity import post_trade
from test_activity_service import _base_records
from test_ledger_safety import trade


if __name__ == "__main__":
    with TemporaryDirectory(prefix="lookthrough-ledger-demo-") as directory:
        path = Path(directory) / "synthetic.sqlite3"
        app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}",
                          "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"timeout": 0.2}},
                          "SECRET_KEY": "synthetic-ledger-browser-only",
                          "RESTORE_STAGING_DIRECTORY": str(Path(directory) / "staging")})
        with app.app_context():
            db.create_all()
            records = _base_records()
            post_trade(trade(records, "buy", "100", date(2026, 1, 1)))
            post_trade(trade(records, "sell", "80", date(2026, 3, 1)))
        print(f"LEDGER_REVIEW_DATABASE={path}", flush=True)
        app.run(host="127.0.0.1", port=5195, debug=False, use_reloader=False)
