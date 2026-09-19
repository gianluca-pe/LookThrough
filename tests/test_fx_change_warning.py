"""FX direction/change acknowledgement safeguards."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import re

from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import func, select

from app.extensions import db
from app.models import FxRate, Portfolio
from app.services.fx import assess_fx_change


def _portfolio_with_rate(
    app: Flask,
    *,
    base: str = "USD",
    quote: str = "EUR",
    rate: str = "0.9",
) -> int:
    with app.app_context():
        portfolio = Portfolio(
            name="Example portfolio",
            reporting_currency_code="EUR",
            annual_spending_amount=Decimal("30000"),
            annual_spending_currency_code="USD",
            default_as_of_date=date(2026, 8, 30),
        )
        db.session.add_all(
            [
                portfolio,
                FxRate(
                    effective_date=date(2026, 8, 20),
                    base_currency_code=base,
                    quote_currency_code=quote,
                    quote_per_base_amount=Decimal(rate),
                ),
            ]
        )
        db.session.commit()
        return portfolio.id


def test_assessment_normalizes_inverse_and_warns_only_above_ten_percent(
    app: Flask,
) -> None:
    _portfolio_with_rate(app, base="EUR", quote="USD", rate="1.25")
    with app.app_context():
        same = assess_fx_change(
            "USD", "EUR", date(2026, 8, 30), Decimal("0.8")
        )
        exact_threshold = assess_fx_change(
            "USD", "EUR", date(2026, 8, 30), Decimal("0.88")
        )
        over_threshold = assess_fx_change(
            "USD", "EUR", date(2026, 8, 30), Decimal("0.881")
        )

        assert same.previous_rate == Decimal("0.8")
        assert same.change_ratio == Decimal("0")
        assert not same.requires_acknowledgement
        assert exact_threshold.change_ratio == Decimal("0.1")
        assert not exact_threshold.requires_acknowledgement
        assert over_threshold.change_ratio == Decimal("0.10125")
        assert over_threshold.requires_acknowledgement


def test_individual_fx_large_change_requires_explicit_no_js_resubmit(
    client: FlaskClient, app: Flask
) -> None:
    _portfolio_with_rate(app)
    data = {
        "base_currency_code": "USD",
        "quote_currency_code": "EUR",
        "effective_date": "2026-08-30",
        "quote_per_base_amount": "1.2",
    }
    first = client.post("/values/fx", data=data)
    body = first.get_data(as_text=True)

    assert first.status_code == 200
    assert "Previous comparable rate: 1 USD =" in body
    assert re.search(r"differs by\s+33\.3%", body)
    assert "I checked this rate; save it anyway" in body
    checkbox = re.search(
        r'<input[^>]*id="fx-acknowledge_large_change"[^>]*>', body
    ).group(0)
    assert 'aria-invalid="true"' in checkbox and "autofocus" in checkbox
    with app.app_context():
        assert db.session.scalar(select(func.count(FxRate.id))) == 1

    accepted = client.post(
        "/values/fx", data={**data, "acknowledge_large_change": "y"}
    )
    assert accepted.status_code == 302
    with app.app_context():
        saved = db.session.scalar(
            select(FxRate).where(FxRate.effective_date == date(2026, 8, 30))
        )
        assert saved.quote_per_base_amount == Decimal("1.2")


def test_routine_fx_large_change_preserves_rate_and_acknowledges_per_row(
    client: FlaskClient, app: Flask
) -> None:
    _portfolio_with_rate(app)
    data = {"effective_date": "2026-08-30", "fx_USD_EUR": "1.2"}
    first = client.post("/values/routine/fx", data=data)
    body = first.get_data(as_text=True)

    assert first.status_code == 200
    assert 'value="1.2"' in re.search(
        r'<input[^>]*id="fx_USD_EUR"[^>]*>', body
    ).group(0)
    assert 'name="ack_fx_USD_EUR"' in body
    assert "Previous comparable rate: 1 USD =" in body
    with app.app_context():
        assert db.session.scalar(select(func.count(FxRate.id))) == 1

    accepted = client.post(
        "/values/routine/fx",
        data={**data, "ack_fx_USD_EUR": "1"},
    )
    assert accepted.status_code == 302
    assert accepted.headers["Location"].endswith(
        "/values?as_of=2026-08-30#routine-fx-heading"
    )
    with app.app_context():
        assert db.session.scalar(select(func.count(FxRate.id))) == 2


def test_first_rate_has_no_plausibility_warning(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        db.session.add(
            Portfolio(
                name="Example portfolio",
                reporting_currency_code="EUR",
                annual_spending_amount=Decimal("30000"),
                annual_spending_currency_code="USD",
                default_as_of_date=date(2026, 8, 30),
            )
        )
        db.session.commit()
    response = client.post(
        "/values/fx",
        data={
            "base_currency_code": "USD",
            "quote_currency_code": "EUR",
            "effective_date": "2026-08-30",
            "quote_per_base_amount": "100",
        },
    )
    assert response.status_code == 302


def test_individual_same_day_correction_is_explicit_and_retains_prior_value(
    client: FlaskClient, app: Flask
) -> None:
    _portfolio_with_rate(app)
    with app.app_context():
        existing = FxRate(
            effective_date=date(2026, 8, 30),
            base_currency_code="USD",
            quote_currency_code="EUR",
            quote_per_base_amount=Decimal("0.92"),
            source_note="Entered from statement",
        )
        db.session.add(existing)
        db.session.commit()
        existing_id = existing.id

    data = {
        "base_currency_code": "USD",
        "quote_currency_code": "EUR",
        "effective_date": "2026-08-30",
        "quote_per_base_amount": "0.91",
    }
    first = client.post("/values/fx", data=data)
    body = first.get_data(as_text=True)
    compact = " ".join(body.split())
    assert first.status_code == 200
    assert "An FX source already exists for this pair and date" in body
    assert "Existing: 1 USD = 0.92 EUR" in compact
    assert "Replacement: 1 USD = 0.91 EUR" in compact
    checkbox = re.search(
        r'<input[^>]*id="fx-confirm_same_day_correction"[^>]*>', body
    ).group(0)
    assert 'aria-invalid="true"' in checkbox and "autofocus" in checkbox

    accepted = client.post(
        "/values/fx", data={**data, "confirm_same_day_correction": "y"}
    )
    assert accepted.status_code == 302
    with app.app_context():
        rows = list(db.session.scalars(select(FxRate).order_by(FxRate.id)))
        corrected = db.session.get(FxRate, existing_id)
        assert len(rows) == 2
        assert corrected.quote_per_base_amount == Decimal("0.91")
        assert "Entered from statement" in corrected.source_note
        assert "previous 1 USD = 0.92 EUR" in corrected.source_note


def test_same_day_correction_keeps_direct_and_inverse_sources_reciprocal(
    client: FlaskClient, app: Flask
) -> None:
    _portfolio_with_rate(app)
    with app.app_context():
        db.session.add_all(
            [
                FxRate(
                    effective_date=date(2026, 8, 30),
                    base_currency_code="USD",
                    quote_currency_code="EUR",
                    quote_per_base_amount=Decimal("0.9"),
                ),
                FxRate(
                    effective_date=date(2026, 8, 30),
                    base_currency_code="EUR",
                    quote_currency_code="USD",
                    quote_per_base_amount=Decimal("1.111111"),
                ),
            ]
        )
        db.session.commit()

    response = client.post(
        "/values/fx",
        data={
            "base_currency_code": "USD",
            "quote_currency_code": "EUR",
            "effective_date": "2026-08-30",
            "quote_per_base_amount": "0.8",
            "confirm_same_day_correction": "y",
            "acknowledge_large_change": "y",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        rows = list(
            db.session.scalars(
                select(FxRate)
                .where(FxRate.effective_date == date(2026, 8, 30))
                .order_by(FxRate.base_currency_code)
            )
        )
        by_direction = {
            (row.base_currency_code, row.quote_currency_code): row for row in rows
        }
        assert by_direction[("USD", "EUR")].quote_per_base_amount == Decimal("0.8")
        assert by_direction[("EUR", "USD")].quote_per_base_amount == Decimal("1.25")
        assert all("previous 1" in row.source_note for row in rows)
