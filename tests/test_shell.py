"""Recovery-page and missing-value presentation regressions."""

from flask import Flask, render_template_string
from flask.testing import FlaskClient


def test_500_renders_inside_shell(app: Flask, client: FlaskClient) -> None:
    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("test failure")

    app.config["PROPAGATE_EXCEPTIONS"] = False
    response = client.get("/boom")
    body = response.get_data(as_text=True)
    assert response.status_code == 500
    assert "Something went wrong" in body
    assert 'class="sidebar"' in body


def test_money_macro_shows_missing_reason(app: Flask) -> None:
    with app.test_request_context():
        page = render_template_string(
            '{% from "_macros.html" import money_missing %}'
            '{{ money_missing(reason) }}',
            reason="no price since 12 May 2026",
        )
    assert "— no price since 12 May 2026" in page
