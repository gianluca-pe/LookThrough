"""Disposable backup browser fixture on 127.0.0.1:5196; CSRF stays enabled."""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from app.extensions import db
from test_backup_safety import classified_payload


if __name__ == "__main__":
    with TemporaryDirectory(prefix="lookthrough-backup-demo-") as directory:
        root = Path(directory)
        app = create_app({
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{root / 'synthetic.sqlite3'}",
            "SECRET_KEY": "synthetic-backup-browser-only",
            "RESTORE_STAGING_DIRECTORY": str(root / "staging"),
        })
        with app.app_context():
            db.create_all()
            portfolio, payload = classified_payload()
            (root / "valid.json").write_text(json.dumps(payload))
            payload["tables"]["instrument_classifications"][0]["weight_decimal"] = "0.5"
            (root / "invalid.json").write_text(json.dumps(payload))
        print(f"BACKUP_REVIEW_DIRECTORY={root}", flush=True)
        app.run(host="127.0.0.1", port=5196, debug=False, use_reloader=False)
