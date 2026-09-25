"""Database integrity configuration."""

from flask import Flask
from sqlalchemy import text

from app.extensions import db

def test_sqlite_foreign_key_enforcement_is_enabled(app: Flask) -> None:
    with app.app_context():
        enabled = db.session.execute(text("PRAGMA foreign_keys")).scalar_one()

    assert enabled == 1
