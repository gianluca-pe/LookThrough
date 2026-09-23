"""Current navigation and safe recovery from retired planning screens."""

from decimal import Decimal

import pytest

from app.extensions import db
from app.services.retirement_plans import adopted_plan, save_plan
from test_m5_retirement import _portfolio_account
from test_two_tier_retirement import seed, plan_data


@pytest.mark.parametrize("path", ["/planning/funding", "/planning/retirement"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_retired_urls_keep_selected_database_and_date(app, client, path, method):
    with app.app_context():
        _portfolio_account()
        db.session.commit()
    response = getattr(client, method)(
        path + "?as_of=2026-08-30",
        environ_overrides={"SCRIPT_NAME": "/d/synthetic-selection"},
    )
    assert response.status_code == (303 if method == "post" else 302)
    assert response.location == "/d/synthetic-selection/retirement?as_of=2026-08-30"


def test_retired_forms_still_require_csrf(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    for path in ("/planning/funding", "/planning/retirement"):
        assert client.post(path, data={}).status_code == 400


def test_no_plan_has_no_legacy_links_and_cannot_create_budget_plan(app, client):
    with app.app_context():
        portfolio, _ = _portfolio_account()
        portfolio_id = portfolio.id
        db.session.commit()
    for path in ("/retirement", "/settings"):
        body = client.get(path).data
        assert b"View existing budget plan" not in body
        assert b"Annual inflation assumption" not in body
        assert b"/planning/funding" not in body
        assert b"/planning/retirement" not in body
    for method in ("get", "post"):
        response = getattr(client, method)("/retirement/budget")
        assert response.location == "/retirement"
        assert response.status_code == (303 if method == "post" else 302)
    with app.app_context():
        assert adopted_plan(portfolio_id) is None


def test_existing_budget_link_is_prefixed_and_preserves_plan(app, client):
    with app.app_context():
        portfolio, _, plan = seed()
        portfolio_id, core = portfolio.id, plan.core_amount
    prefix = "/d/synthetic-selection"
    body = client.get("/retirement", environ_overrides={"SCRIPT_NAME": prefix}).data
    assert b"View existing budget plan" in body
    assert (prefix + "/retirement/budget").encode() in body
    page = client.get("/retirement/budget")
    assert page.status_code == 200
    assert b"Current Retirement planner" in page.data
    assert b"/planning/funding" not in page.data
    assert b"/planning/retirement" not in page.data
    with app.app_context():
        plan = adopted_plan(portfolio_id)
        assert plan.planning_mode == "budget"
        assert plan.core_amount == core


def test_affordability_plan_has_no_budget_link_and_redirects_old_url(app, client):
    with app.app_context():
        portfolio, _, _ = seed()
        values = plan_data(planning_mode="affordability", legacy_value_basis="today", spending_policy="full_budget", terminal_legacy_target_amount=Decimal("0"), terminal_legacy_target_currency_code="USD")
        save_plan(portfolio.id, values, confirmed=True)
        db.session.commit()
    assert b"View existing budget plan" not in client.get("/retirement").data
    response = client.get("/retirement/budget")
    assert response.status_code == 302
    assert response.location == "/retirement"
