"""Application factory and smoke-route tests."""

from pathlib import Path

from flask import Flask
from flask.testing import FlaskClient

from app import create_app


def test_default_factory_discovers_inside_instance_without_binding_a_database() -> None:
    application = create_app({"TESTING": True})
    assert application.extensions['datasets'].folder == Path(application.instance_path)
    assert 'sqlalchemy' not in application.extensions
    assert application.config["WTF_CSRF_ENABLED"] is True


def test_factory_registers_foundation_extensions(app: Flask) -> None:
    assert "sqlalchemy" in app.extensions
    assert "migrate" in app.extensions
    assert "csrf" in app.extensions


def test_factory_uses_disposable_sqlite_database(
    app: Flask, database_path: Path
) -> None:
    assert app.config["SQLALCHEMY_DATABASE_URI"] == f"sqlite:///{database_path}"
    assert database_path.exists()


def test_health_endpoint_is_minimal_plain_text(client: FlaskClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    assert response.get_data(as_text=True) == "ok\n"
