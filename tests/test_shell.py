"""Shared shell structure and accessibility on the routine Values page:
skip link, single h1, aria-current, labelled fields, visible focus and error pages."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal



def assert_no_inline_script(body: str) -> None:
    """Only the deferred enhancement file may appear; no inline or
    behaviour-critical JavaScript."""
    scripts = [re.sub(r"\?v=\d+", "", tag) for tag in re.findall(r"<script[^>]*>", body)]
    assert scripts == ['<script src="/static/js/enhance.js" defer>']

import pytest
from flask import Flask, render_template_string
from flask.testing import FlaskClient

from app.extensions import db
from app.models import Portfolio
from app.template_filters import (
    display_date,
    money_amount,
    percentage_amount,
)


@pytest.fixture
def page(client: FlaskClient, app: Flask) -> str:
    with app.app_context():
        db.session.add(Portfolio(
            name="P", reporting_currency_code="EUR",
            annual_spending_amount=Decimal("48000"),
        ))
        db.session.commit()
    response = client.get("/values")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_root_redirects_to_setup(client: FlaskClient) -> None:
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/setup")


def test_page_document_basics(page: str) -> None:
    assert page.startswith("<!DOCTYPE html>")
    assert '<html lang="en">' in page
    assert '<meta name="viewport"' in page
    assert "<title>Update values — LookThrough</title>" in page
    # Declared icon: browsers stop probing /favicon.ico and apple-touch-icon.
    # static_url appends a ?v=<mtime> cache-buster to every static asset.
    assert re.search(r'<link rel="icon" href="/static/favicon\.svg\?v=\d+" type="image/svg\+xml">', page)
    assert re.search(r'<link rel="alternate icon" href="/static/favicon\.png\?v=\d+">', page)
    assert page.count("<h1") == 1


def test_skip_link_is_first_focusable_element(page: str) -> None:
    body = page.split("<body>", 1)[1]
    first_tag = body.lstrip().split(">", 1)[0]
    assert first_tag.startswith('<a class="skip-link" href="#main"')
    assert 'id="main"' in page


def test_no_positive_tabindex(page: str) -> None:
    assert re.search(r'tabindex="[1-9]', page) is None


def _nav_regions(page: str) -> tuple[str, str]:
    """Return (sidebar nav, mobile details-menu nav) markup."""

    sidebar = page.split('<aside class="sidebar">', 1)[1].split("</aside>", 1)[0]
    mobile = page.split('<details class="nav-menu" name="header-disclosures">', 1)[1].split("</details>", 1)[0]
    return sidebar, mobile


def test_navigation_current_and_disabled(page: str) -> None:
    sidebar, mobile = _nav_regions(page)
    # The two landmarks have distinct accessible names.
    assert '<nav aria-label="Primary navigation">' in sidebar
    assert 'aria-label="Mobile navigation"' in mobile
    # All core destinations plus the contextual Guide are present in both landmarks.
    for label in ("Overview", "Holdings", "Activity", "Instruments",
                  "Update values", "Accounts", "Settings &amp; backup", "Guide"):
        assert label in sidebar
        assert label in mobile
    # Exactly one aria-current inside each <nav>, not globally.
    assert sidebar.count('aria-current="page"') == 1
    assert mobile.count('aria-current="page"') == 1
    # Every displayed destination is implemented and linked.
    assert sidebar.count('class="nav-disabled" aria-disabled="true"') == 0
    assert mobile.count('class="nav-disabled" aria-disabled="true"') == 0
    # Both current links resolve to the same endpoint.
    sidebar_current = re.search(r'<a href="([^"]+)" aria-current="page"', sidebar)
    mobile_current = re.search(r'<a href="([^"]+)" aria-current="page"', mobile)
    assert sidebar_current and mobile_current
    assert sidebar_current.group(1) == mobile_current.group(1)


def test_update_values_nav_is_linked_and_current(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        db.session.add(Portfolio(
            name="P", reporting_currency_code="EUR",
            annual_spending_amount=Decimal("48000"),
        ))
        db.session.commit()
    for route in ("/values", "/setup/values"):
        body = client.get(route).get_data(as_text=True)
        sidebar, mobile = _nav_regions(body)
        for nav in (sidebar, mobile):
            assert 'href="/values" aria-current="page">Update values</a>' in nav


def test_mobile_menu_markup(page: str) -> None:
    # No-JS <details>/<summary> navigation below 900px.
    assert '<details class="nav-menu" name="header-disclosures">' in page
    assert "<summary>Menu</summary>" in page
    _, mobile = _nav_regions(page)
    assert '<nav class="nav-menu-panel" aria-label="Mobile navigation">' in mobile
    assert 'href="/overview"' in mobile
    # The menu is a real details element, not a JS-only widget.
    assert_no_inline_script(page)


def test_each_breakpoint_exposes_one_navigation() -> None:
    with open("app/static/css/app.css") as fh:
        css = fh.read()
    media_start = css.index("@media (max-width: 900px)")
    desktop, narrow = css[:media_start], css[media_start:]
    # Desktop: mobile menu hidden, sidebar visible.
    assert ".nav-menu { display: none; }" in desktop
    assert ".sidebar {" in desktop
    # Narrow: sidebar hidden, details menu shown.
    assert ".sidebar { display: none; }" in narrow
    assert ".nav-menu { display: block;" in narrow


def test_add_activity_visible_and_linked(page: str) -> None:
    assert "+ Add activity" in page
    assert '<a class="btn btn-primary" href="/activity/new">+ Add activity</a>' in page


def test_labels_match_inputs(page: str) -> None:
    labels = set(re.findall(r'<label for="([^"]+)"', page))
    inputs = set(re.findall(r'<(?:input|select|textarea)\b[^>]*\bid="([^"]+)"', page))
    assert labels, "Values should contain labelled form fields"
    assert labels <= inputs


def test_field_error_accessibility(client: FlaskClient) -> None:
    response = client.post("/setup/portfolio", data={})
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'aria-invalid="true"' in page
    assert 'id="name-error"' in page
    assert 'href="#name"' in page
    assert re.search(r'aria-describedby="[^"]*name-error', page)
    name_input = re.search(r'<input\b[^>]*\bid="name"[^>]*>', page)
    assert name_input and "autofocus" in name_input.group()


def test_focus_visible_css() -> None:
    with open("app/static/css/app.css") as css:
        assert ":focus-visible" in css.read()


def test_404_renders_inside_shell(client: FlaskClient) -> None:
    response = client.get("/shell/preview")
    body = response.get_data(as_text=True)
    assert response.status_code == 404
    assert "Page not found" in body
    assert 'class="sidebar"' in body
    assert body.count("<h1") == 1


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


def test_flash_messages_render_with_categories(app: Flask, client: FlaskClient) -> None:
    from flask import flash, redirect, url_for

    @app.get("/flash-demo")
    def flash_demo():
        flash("Saved.", "success")
        flash("Stale price.", "warning")
        flash("Could not save.", "error")
        return redirect(url_for("setup.show"))

    response = client.get("/flash-demo", follow_redirects=True)
    body = response.get_data(as_text=True)
    assert 'role="status"' in body
    assert 'flash flash-success' in body
    assert 'flash flash-warning' in body
    assert 'flash flash-error' in body


def test_money_macro_output(app: Flask) -> None:
    with app.test_request_context():
        page = render_template_string(
            '{% from "_macros.html" import money, money_missing %}'
            '{{ money(value) }}{{ money_missing(reason) }}',
            value={
                "amount": "18400.00", "currency": "EUR",
                "reporting": {
                    "amount": "27424.88", "currency": "SGD",
                    "note": "1 EUR = 1.4907 SGD · 31 Jul 2026",
                },
            },
            reason="no price since 12 May 2026",
        )
    assert '<span class="ccy">EUR</span> 18,400.00' in page
    assert "≈ " in page and "1 EUR = 1.4907 SGD · 31 Jul 2026" in page
    assert "— no price since 12 May 2026" in page  # missing state, never a zero


# --- Formatting filters (display only; no financial recomputation) ---

@pytest.mark.parametrize(
    ("value", "places", "expected"),
    [
        ("18400.00", 2, "18,400.00"),
        ("18400", 2, "18,400.00"),
        (Decimal("1234567.890"), 2, "1,234,567.89"),
        (Decimal("1234567.895"), 2, "1,234,567.90"),  # display rounding, half even
        (1000, 2, "1,000.00"),
        ("-10275.00", 2, "-10,275.00"),
        ("1000.5000", None, "1,000.5000"),  # quantities keep given precision
        (None, 2, "—"),
    ],
)
def test_money_amount_formatting(value, places, expected: str) -> None:
    assert money_amount(value, places) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2026, 6, 30), "30 Jun 2026"),
        (date(2026, 7, 5), "5 Jul 2026"),
        ("2026-05-12", "12 May 2026"),
    ],
)
def test_display_date_formatting(value, expected: str) -> None:
    assert display_date(value) == expected


@pytest.mark.parametrize(
    ("value", "places", "expected"),
    [
        (Decimal("0.12345"), 1, "12.3"),
        ("1", 1, "100.0"),
        (Decimal("0.333333333333333333"), 2, "33.33"),
        (Decimal("0.6"), None, "60"),
        (Decimal("0.333333"), None, "33.3333"),
        (None, 1, "—"),
    ],
)
def test_percentage_amount_formatting(value, places, expected: str) -> None:
    assert percentage_amount(value, places) == expected
