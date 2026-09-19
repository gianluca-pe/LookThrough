"""Current/former instrument catalogue route contract."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import re
from urllib.parse import parse_qs, urlparse

from flask import Flask, template_rendered
from flask.testing import FlaskClient

from app.extensions import db
from app.models import (
    Account,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    Posting,
    Price,
    Transaction,
)


def _portfolio_account(app: Flask, *, name: str = "Brokerage") -> tuple[int, int]:
    with app.app_context():
        portfolio = db.session.query(Portfolio).first()
        if portfolio is None:
            portfolio = Portfolio(
                name="Portfolio",
                reporting_currency_code="EUR",
                annual_spending_amount=Decimal("48000"),
                annual_spending_currency_code="EUR",
                default_as_of_date=date(2026, 8, 9),
            )
            institution = Institution(portfolio=portfolio, name="Bank")
            db.session.add_all([portfolio, institution])
            db.session.flush()
        else:
            institution = db.session.query(Institution).filter_by(
                portfolio_id=portfolio.id
            ).first()
        account = Account(
            portfolio_id=portfolio.id,
            institution_id=institution.id,
            name=name,
            account_type="brokerage",
            default_currency_code="EUR",
            is_multicurrency=False,
            cash_tracking_mode="included_in_aggregate",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add(account)
        db.session.commit()
        return portfolio.id, account.id


def _instrument(
    portfolio_id: int,
    account_id: int | None,
    *,
    name: str,
    opening_date: date = date(2026, 8, 1),
) -> int:
    instrument = Instrument(
        portfolio_id=portfolio_id,
        name=name,
        instrument_type="stock",
        valuation_currency_code="EUR",
        is_active=True,
    )
    db.session.add(instrument)
    db.session.flush()
    if account_id is None:
        db.session.commit()
        return instrument.id
    registration = PositionRegistration(
        account_id=account_id,
        instrument_id=instrument.id,
        tracking_mode="transaction_tracked",
        opening_date=opening_date,
    )
    opening = Transaction(
        portfolio_id=portfolio_id,
        transaction_type="opening_balance",
        effective_date=opening_date,
        status="posted",
    )
    db.session.add_all([registration, opening])
    db.session.flush()
    db.session.add_all(
        [
            Posting(
                transaction_id=opening.id,
                account_id=account_id,
                posting_kind="instrument",
                instrument_id=instrument.id,
                currency_code="EUR",
                quantity_delta=Decimal("10"),
            ),
            Price(
                instrument_id=instrument.id,
                effective_date=opening_date,
                price_amount=Decimal("5"),
                currency_code="EUR",
            ),
        ]
    )
    db.session.commit()
    return instrument.id


def _sell_all(
    portfolio_id: int,
    account_id: int,
    instrument_id: int,
    *,
    effective_date: date = date(2026, 8, 7),
) -> int:
    sale = Transaction(
        portfolio_id=portfolio_id,
        transaction_type="sell",
        effective_date=effective_date,
        status="posted",
    )
    db.session.add(sale)
    db.session.flush()
    db.session.add(
        Posting(
            transaction_id=sale.id,
            account_id=account_id,
            posting_kind="instrument",
            instrument_id=instrument_id,
            currency_code="EUR",
            quantity_delta=Decimal("-10"),
        )
    )
    db.session.commit()
    return sale.id


def test_default_current_hides_former_and_unused(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        _instrument(portfolio_id, account_id, name="Current holding")
        sold_id = _instrument(portfolio_id, account_id, name="Former holding")
        _sell_all(portfolio_id, account_id, sold_id)
        _instrument(portfolio_id, None, name="Unused identity")

    current = client.get("/instruments?as_of=2026-08-09").get_data(as_text=True)
    assert "Current holding" in current
    assert "Former holding" not in current
    assert "Unused identity" not in current

    former = client.get(
        "/instruments?as_of=2026-08-09&view=former"
    ).get_data(as_text=True)
    assert "Former holding" in former
    assert "Current holding" not in former
    assert "Unused identity" not in former

    all_rows = client.get(
        "/instruments?as_of=2026-08-09&view=all"
    ).get_data(as_text=True)
    assert "Current holding" in all_rows
    assert "Former holding" in all_rows
    assert "Unused identity" in all_rows


def test_template_contract_exposes_counts_and_row_statuses(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        _instrument(portfolio_id, account_id, name="Current holding")
        sold_id = _instrument(portfolio_id, account_id, name="Former holding")
        _sell_all(portfolio_id, account_id, sold_id)
        _instrument(portfolio_id, None, name="Unused identity")

    captured = []

    def record(sender, template, context, **extra):
        captured.append(context)

    with template_rendered.connected_to(record, app):
        response = client.get("/instruments?as_of=2026-08-09&view=all")
    assert response.status_code == 200
    context = captured[-1]
    assert context["instrument_view"] == "all"
    assert context["instrument_counts"] == {
        "current": 1,
        "former": 1,
        "all": 3,
    }
    assert {
        row["name"]: row["holding_status"] for row in context["instruments"]
    } == {
        "Current holding": "current",
        "Former holding": "former",
        "Unused identity": "unused",
    }


def test_catalogue_controls_preserve_as_of_and_all_labels_noncurrent_rows(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        _instrument(portfolio_id, account_id, name="Current holding")
        sold_id = _instrument(portfolio_id, account_id, name="Former holding")
        _sell_all(portfolio_id, account_id, sold_id)
        _instrument(portfolio_id, None, name="Unused identity")

    body = client.get("/instruments?view=all&as_of=2026-08-09").get_data(as_text=True)
    controls = body.split('aria-label="Instrument catalogue view"', 1)[1].split("</nav>", 1)[0]
    assert 'href="/instruments?view=current&amp;as_of=2026-08-09"' in controls
    assert 'href="/instruments?view=former&amp;as_of=2026-08-09"' in controls
    assert re.search(
        r'href="/instruments\?view=all&amp;as_of=2026-08-09"\s+aria-current="page"',
        controls,
    )
    assert re.search(r">\s*Current <span class=\"catalogue-count\">1</span>", controls)
    assert re.search(r">\s*Former <span class=\"catalogue-count\">1</span>", controls)
    assert re.search(r">\s*All <span class=\"catalogue-count\">3</span>", controls)
    as_of_form = body.split('<form class="asof-form"', 1)[1].split("</form>", 1)[0]
    assert 'type="hidden" name="view" value="all"' in as_of_form
    former_row = body.split('<th scope="row">Former holding', 1)[1].split("</tr>", 1)[0]
    unused_row = body.split('<th scope="row">Unused identity', 1)[1].split("</tr>", 1)[0]
    current_row = body.split('<th scope="row">Current holding', 1)[1].split("</tr>", 1)[0]
    assert '<span class="catalogue-status">Former</span>' in former_row
    assert '<span class="catalogue-status">Unused</span>' in unused_row
    assert 'catalogue-status' not in current_row


def test_catalogue_editor_and_empty_state_keep_selected_scope(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        instrument_id = _instrument(portfolio_id, account_id, name="Former holding")
        _sell_all(portfolio_id, account_id, instrument_id)

    current = client.get("/instruments?as_of=2026-08-09").get_data(as_text=True)
    assert "No instruments are held as of 9 Aug 2026." in current
    assert 'href="/instruments?view=former&amp;as_of=2026-08-09">View former instruments</a>' in current
    assert 'href="/instruments?view=all&amp;as_of=2026-08-09">View all instrument identities</a>' in current
    former = client.get("/instruments?view=former&as_of=2026-08-09").get_data(as_text=True)
    edit_url = f'/instruments/{instrument_id}/classification?view=former&amp;as_of=2026-08-09'
    assert f'href="{edit_url}"' in former
    editor = client.get(edit_url.replace("&amp;", "&")).get_data(as_text=True)
    assert 'href="/instruments?view=former&amp;as_of=2026-08-09">Cancel</a>' in editor


def test_historical_as_of_and_invalid_view_use_current_truth(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        sold_id = _instrument(portfolio_id, account_id, name="Round trip stock")
        _sell_all(portfolio_id, account_id, sold_id)

    historical = client.get(
        "/instruments?as_of=2026-08-06"
    ).get_data(as_text=True)
    assert "Round trip stock" in historical
    invalid = client.get(
        "/instruments?as_of=2026-08-09&view=unexpected"
    ).get_data(as_text=True)
    assert "Round trip stock" not in invalid

    with app.app_context():
        repurchase = Transaction(
            portfolio_id=portfolio_id,
            transaction_type="buy",
            effective_date=date(2026, 8, 8),
            status="posted",
        )
        db.session.add(repurchase)
        db.session.flush()
        db.session.add(
            Posting(
                transaction_id=repurchase.id,
                account_id=account_id,
                posting_kind="instrument",
                instrument_id=sold_id,
                currency_code="EUR",
                quantity_delta=Decimal("2"),
            )
        )
        db.session.commit()
    repurchased = client.get(
        "/instruments?as_of=2026-08-09"
    ).get_data(as_text=True)
    assert "Round trip stock" in repurchased


def test_instrument_held_in_another_account_remains_current(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, sold_account_id = _portfolio_account(app, name="First account")
    _, open_account_id = _portfolio_account(app, name="Second account")
    with app.app_context():
        instrument_id = _instrument(
            portfolio_id, sold_account_id, name="Shared instrument"
        )
        _sell_all(portfolio_id, sold_account_id, instrument_id)
        registration = PositionRegistration(
            account_id=open_account_id,
            instrument_id=instrument_id,
            tracking_mode="transaction_tracked",
            opening_date=date(2026, 8, 2),
        )
        opening = Transaction(
            portfolio_id=portfolio_id,
            transaction_type="opening_balance",
            effective_date=date(2026, 8, 2),
            status="posted",
        )
        db.session.add_all([registration, opening])
        db.session.flush()
        db.session.add(
            Posting(
                transaction_id=opening.id,
                account_id=open_account_id,
                posting_kind="instrument",
                instrument_id=instrument_id,
                currency_code="EUR",
                quantity_delta=Decimal("3"),
            )
        )
        db.session.commit()

    body = client.get("/instruments?as_of=2026-08-09").get_data(as_text=True)
    assert "Shared instrument" in body


def test_classification_save_preserves_originating_catalogue_view(
    client: FlaskClient, app: Flask
) -> None:
    portfolio_id, account_id = _portfolio_account(app)
    with app.app_context():
        instrument_id = _instrument(
            portfolio_id, account_id, name="Former unclassified"
        )
        _sell_all(portfolio_id, account_id, instrument_id)

    response = client.post(
        f"/instruments/{instrument_id}/classification"
        "?view=former&as_of=2026-08-09",
        data={
            "effective_date": "2026-08-09",
            "classification_mode": "simple",
            "primary_role_code": "equity",
            "fire_bucket_code": "projects",
            "save_classification": "Save classification",
        },
    )
    assert response.status_code == 302
    parsed = urlparse(response.headers["Location"])
    assert parsed.path == "/instruments"
    assert parse_qs(parsed.query) == {
        "view": ["former"],
        "as_of": ["2026-08-09"],
    }
