"""Reject foreign authorities before local data or request hooks are reached."""

import pytest
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Response

from app import create_app
from app.services.local_databases import fingerprint
from test_local_databases import choose, inputs, seed


@pytest.fixture(params=["launcher", "portfolio"])
def entry(request, app, tmp_path):
    if request.param == "portfolio":
        return app
    return create_app({"TESTING": True, "LOCAL_DATABASE_DIRECTORY": str(tmp_path)})


@pytest.mark.parametrize("host", [
    "localhost", "localhost:5001", "LOCALHOST:80", "127.0.0.1", "127.0.0.1:5194",
    "localhost:1", "127.0.0.1:65535",
])
def test_supported_local_authorities_reach_application(entry, host):
    response = entry.test_client().get("/health", headers={"Host": host})
    assert response.status_code == 200
    assert response.data == b"ok\n"


@pytest.mark.parametrize("host", [
    "untrusted.example:5001", "localhost.untrusted.example", "127.0.0.1.example",
    "sub.localhost", "localhost.", "192.168.1.2", "0.0.0.0", "127.0.0.2",
    "[::1]", "localhost@untrusted.example", "untrusted.example@localhost",
    "localhost,untrusted.example", "localhost:5001:80", "localhost:evil",
    "localhost:", "localhost:0", "localhost:65536", "localhost:123456",
    "localhost:１２３", "localhost/", " localhost", "localhost ", "",
])
def test_other_authorities_are_rejected_before_flask(entry, host):
    reached = []

    @entry.before_request
    def must_not_run():
        reached.append(True)

    # Send the raw WSGI authority: the test client itself refuses invalid ports.
    environ = EnvironBuilder(path="/health").get_environ()
    environ["HTTP_HOST"] = host
    response = Response.from_app(entry.wsgi_app, environ)
    assert response.status_code == 400
    assert not reached
    assert b"This address is not supported" in response.data
    assert response.headers["Cache-Control"] == "no-store"
    assert "Set-Cookie" not in response.headers


def test_missing_host_does_not_fall_back_to_server_name(entry):
    environ = EnvironBuilder(path="/", base_url="http://localhost").get_environ()
    environ.pop("HTTP_HOST")
    response = Response.from_app(entry.wsgi_app, environ)
    assert response.status_code == 400


def test_forwarded_headers_neither_bypass_guard_nor_override_local_host(entry):
    client = entry.test_client()
    assert client.get("/health", headers={"Host": "untrusted.example", "X-Forwarded-Host": "localhost",
        "Forwarded": "host=127.0.0.1"}).status_code == 400
    assert client.get("/health", headers={"Host": "localhost", "X-Forwarded-Host": "untrusted.example",
        "Forwarded": "host=untrusted.example"}).status_code == 200


def test_rejection_precedes_chooser_discovery_and_dataset_dispatch(tmp_path, monkeypatch):
    import app.databases as databases

    application = create_app({"TESTING": True, "LOCAL_DATABASE_DIRECTORY": str(tmp_path)})

    def forbidden(*args, **kwargs):
        pytest.fail("An untrusted request reached discovery or dataset dispatch")

    monkeypatch.setattr(databases, "discover", forbidden)
    monkeypatch.setattr(databases.DatasetDispatcher, "__call__", forbidden)
    client = application.test_client()
    for path in ("/", "/d/invalid/backup/export", "/static/app.css", "/health"):
        response = client.get(path, headers={"Host": "untrusted.example"})
        assert response.status_code == 400
    assert list(tmp_path.iterdir()) == []


def test_valid_dataset_token_cannot_bypass_guard_or_create_child_app(tmp_path):
    seed(tmp_path, "Audit.sqlite3", "Synthetic audit portfolio")
    application = create_app({"TESTING": True, "LOCAL_DATABASE_DIRECTORY": str(tmp_path)})
    client = application.test_client()
    opened = choose(client, "Audit.sqlite3")
    assert opened.status_code == 303
    prefix = opened.location
    dispatcher = application.extensions["datasets"]
    assert not dispatcher.apps
    before = fingerprint(tmp_path / "Audit.sqlite3")
    for path in (prefix, prefix + "backup/export", prefix + "holdings.csv"):
        response = client.get(path, headers={"Host": "untrusted.example:5001"})
        assert response.status_code == 400
        assert b"Synthetic audit portfolio" not in response.data
    assert not dispatcher.apps
    assert fingerprint(tmp_path / "Audit.sqlite3") == before
    exported = client.get(prefix + "backup/export")
    assert exported.status_code == 200
    assert b"Synthetic audit portfolio" in exported.data
    # The same protection holds when the dataset application is already cached.
    assert client.get(prefix + "backup/export", headers={"Host": "untrusted.example"}).status_code == 400
    assert fingerprint(tmp_path / "Audit.sqlite3") == before


def test_host_guard_rejects_writes_with_genuine_csrf_and_preserves_local_csrf(tmp_path):
    application = create_app({"TESTING": True, "LOCAL_DATABASE_DIRECTORY": str(tmp_path)})
    client = application.test_client()
    data = inputs(client.get("/databases/new")) | {"name": "Practice"}
    preview = client.post("/databases/new", data=data)
    confirmed = inputs(preview) | {"name": "Practice", "confirm": "yes"}
    rejected = client.post("/databases/new", data=confirmed, headers={"Host": "untrusted.example"})
    assert rejected.status_code == 400
    assert b"This address is not supported" in rejected.data
    assert not list(tmp_path.iterdir())
    assert client.post("/databases/new", data={"name": "Practice"}).status_code == 400
    assert not list(tmp_path.iterdir())
    assert client.post("/databases/new", data=confirmed).status_code == 303
    assert (tmp_path / "Practice.sqlite3").exists()
