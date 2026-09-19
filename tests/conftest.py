"""Shared disposable-database fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient, FlaskCliRunner

from app import create_app
from app.extensions import db


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "lookthrough-test.sqlite3"


@pytest.fixture
def app(database_path: Path, tmp_path: Path) -> Iterator[Flask]:
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database_path}",
            "RESTORE_STAGING_DIRECTORY": str(tmp_path / "restore-staging"),
            "WTF_CSRF_ENABLED": False,
        }
    )

    with application.app_context():
        db.create_all()

    yield application

    with application.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


@pytest.fixture
def unmigrated_app(database_path: Path, tmp_path: Path) -> Flask:
    """Application pointed at a blank database for migration tests."""

    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database_path}",
            "RESTORE_STAGING_DIRECTORY": str(tmp_path / "restore-staging"),
            "WTF_CSRF_ENABLED": False,
        }
    )


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def runner(app: Flask) -> FlaskCliRunner:
    return app.test_cli_runner()
