"""Routine creation, Guide, and empty-account cleanup."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import re

from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    Account,
    CashBalanceCheckpoint,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    Posting,
    Transaction,
    ValuationObservation,
)


def _records(app: Flask) -> tuple[int, int]:
    app.config["CURRENT_DATE_PROVIDER"] = lambda: date(2026, 9, 2)
    with app.app_context():
        portfolio = Portfolio(
            name="Example portfolio",
            reporting_currency_code="USD",
            annual_spending_amount=Decimal("30000"),
        )
        db.session.add(portfolio)
        db.session.flush()
        institution = Institution(portfolio_id=portfolio.id, name="Example Bank")
        db.session.add(institution)
        db.session.flush()
        account = Account(
            portfolio_id=portfolio.id,
            institution_id=institution.id,
            name="TEST-ACCOUNT-001",
            account_type="cash",
            default_currency_code="USD",
            is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add(account)
        db.session.commit()
        return institution.id, account.id


def _account_data(institution_id: int, **overrides: str) -> dict[str, str]:
    data = {
        "institution_id": str(institution_id),
        "name": "Savings USD",
        "reference": "130",
        "account_type": "cash",
        "default_currency_code": "USD",
        "cash_tracking_mode": "separate_cash",
        "portfolio_share_percent": "100",
        "present_access_percent": "100",
        "relationship_eligible": "y",
        "add_account": "Add account",
    }
    data.update(overrides)
    return data


def test_routine_account_creation_stays_outside_setup(
    app: Flask, client: FlaskClient
) -> None:
    institution_id, _ = _records(app)
    body = client.get("/accounts/new").get_data(as_text=True)
    assert "<h1>Add account</h1>" in body
    assert 'aria-label="Setup progress"' not in body
    assert "Investments and fixed deposits are holdings inside it" in body

    response = client.post("/accounts/new", data=_account_data(institution_id))
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/accounts/2/created")
    continuation = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "What belongs in this account?" in continuation
    assert "Set opening cash balance" in continuation
    assert "Add holding" in continuation
    assert "Add fixed deposit" in continuation


def test_missing_institution_continues_back_selected(
    app: Flask, client: FlaskClient
) -> None:
    _records(app)
    account_page = client.get("/accounts/new").get_data(as_text=True)
    assert 'href="/institutions/new">Add institution</a>' in account_page

    page = client.get("/institutions/new").get_data(as_text=True)
    assert "<h1>Add institution</h1>" in page
    assert 'aria-label="Setup progress"' not in page
    sidebar = page.split('<aside class="sidebar">', 1)[1].split("</aside>", 1)[0]
    assert 'href="/accounts" aria-current="page">Accounts</a>' in sidebar

    response = client.post(
        "/institutions/new",
        data={"name": "Emirates NBD", "add_institution": "Add institution"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(
        "/accounts/new?institution=2"
    )
    selected = client.get(response.headers["Location"]).get_data(as_text=True)
    assert '<option selected value="2">Emirates NBD</option>' in selected


def test_routine_institution_duplicate_preserves_form_and_focus(
    app: Flask, client: FlaskClient
) -> None:
    _records(app)
    response = client.post(
        "/institutions/new",
        data={"name": " example bank ", "add_institution": "Add institution"},
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "already in the portfolio" in body
    assert 'href="#name"' in body
    name_input = re.search(r'<input[^>]*id="name"[^>]*>', body).group(0)
    assert "autofocus" in name_input
    with app.app_context():
        assert db.session.scalar(select(func.count(Institution.id))) == 1


def test_routine_institution_prefill_ignores_other_portfolio(
    app: Flask, client: FlaskClient
) -> None:
    _records(app)
    with app.app_context():
        other = Portfolio(
            name="Other",
            reporting_currency_code="EUR",
            annual_spending_amount=Decimal("1"),
        )
        db.session.add(other)
        db.session.flush()
        foreign = Institution(portfolio_id=other.id, name="Foreign bank")
        db.session.add(foreign)
        db.session.commit()
        foreign_id = foreign.id

    body = client.get(
        f"/accounts/new?institution={foreign_id}"
    ).get_data(as_text=True)
    assert "Foreign bank" not in body
    assert f'<option selected value="{foreign_id}">' not in body


def test_routine_fixed_deposit_is_statement_valued_and_continues_to_terms(
    app: Flask, client: FlaskClient
) -> None:
    _, account_id = _records(app)
    path = f"/positions/new?account={account_id}&kind=fixed_deposit"
    body = client.get(path).get_data(as_text=True)
    assert "<h1>Add fixed deposit</h1>" in body
    assert 'aria-label="Setup progress"' not in body
    assert f'<option selected value="{account_id}">' in body
    assert 'value="USD"' in body
    assert "does not move cash" in body

    response = client.post(
        path,
        data={
            "account_id": str(account_id),
            # These values are deliberately hostile: the route owns the FD
            # invariants rather than trusting browser-hidden fields.
            "instrument_id": "999",
            "instrument_type": "stock",
            "tracking_mode": "transaction_tracked",
            "new_instrument_name": "FD_USD_207",
            "ticker_or_isin": "207",
            "valuation_currency_code": "usd",
            "effective_date": "2026-09-02",
            "statement_value": "85000",
            "save_position": "Add fixed deposit",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/positions/1/fixed-deposit")
    with app.app_context():
        instrument = db.session.scalar(select(Instrument))
        registration = db.session.scalar(select(PositionRegistration))
        observation = db.session.scalar(select(ValuationObservation))
        assert instrument.instrument_type == "fixed_deposit"
        assert instrument.valuation_currency_code == "USD"
        assert registration.account_id == account_id
        assert registration.tracking_mode == "statement_valued"
        assert observation.native_value_amount == Decimal("85000")


def test_invalid_fixed_deposit_is_atomic(app: Flask, client: FlaskClient) -> None:
    _, account_id = _records(app)
    body = client.post(
        f"/positions/new?account={account_id}&kind=fixed_deposit",
        data={
            "account_id": str(account_id),
            "new_instrument_name": "FD_USD_207",
            "valuation_currency_code": "USD",
            "effective_date": "2026-09-02",
            "statement_value": "-1",
        },
    ).get_data(as_text=True)
    assert "Statement value must not be negative" in body
    with app.app_context():
        assert db.session.scalar(select(func.count(Instrument.id))) == 0
        assert db.session.scalar(select(func.count(PositionRegistration.id))) == 0


def test_account_prefill_cannot_cross_portfolios(
    app: Flask, client: FlaskClient
) -> None:
    _, account_id = _records(app)
    with app.app_context():
        other = Portfolio(
            name="Other",
            reporting_currency_code="EUR",
            annual_spending_amount=Decimal("1"),
        )
        db.session.add(other)
        db.session.flush()
        institution = Institution(portfolio_id=other.id, name="Other bank")
        db.session.add(institution)
        db.session.flush()
        foreign = Account(
            portfolio_id=other.id,
            institution_id=institution.id,
            name="Foreign",
            account_type="cash",
            default_currency_code="EUR",
            is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add(foreign)
        db.session.commit()
        foreign_id = foreign.id

    body = client.post(
        f"/positions/new?account={foreign_id}&kind=fixed_deposit",
        data={
            "account_id": str(foreign_id),
            "new_instrument_name": "Not ours",
            "valuation_currency_code": "EUR",
            "effective_date": "2026-09-02",
            "statement_value": "100",
        },
    ).get_data(as_text=True)
    assert "Not a valid choice" in body
    with app.app_context():
        assert db.session.scalar(select(func.count(PositionRegistration.id))) == 0
        assert db.session.get(Account, account_id).is_active


def test_zero_only_accidental_account_can_be_archived(
    app: Flask, client: FlaskClient
) -> None:
    _, account_id = _records(app)
    with app.app_context():
        db.session.add(
            CashBalanceCheckpoint(
                account_id=account_id,
                currency_code="USD",
                effective_date=date(2026, 9, 2),
                confirmed_balance_amount=Decimal("0"),
                prior_calculated_balance_amount=Decimal("0"),
                correction_amount=Decimal("0"),
            )
        )
        db.session.commit()

    body = client.get(f"/accounts/{account_id}/archive").get_data(as_text=True)
    assert "Confirm archive" in body
    response = client.post(f"/accounts/{account_id}/archive")
    assert response.status_code == 302
    with app.app_context():
        assert not db.session.get(Account, account_id).is_active
        assert db.session.scalar(select(func.count(CashBalanceCheckpoint.id))) == 1


def test_archive_rechecks_and_names_financial_blockers(
    app: Flask, client: FlaskClient
) -> None:
    _, account_id = _records(app)
    assert "Confirm archive" in client.get(
        f"/accounts/{account_id}/archive"
    ).get_data(as_text=True)
    with app.app_context():
        account = db.session.get(Account, account_id)
        instrument = Instrument(
            portfolio_id=account.portfolio_id,
            name="Existing FD",
            instrument_type="fixed_deposit",
            valuation_currency_code="USD",
            is_active=True,
        )
        db.session.add(instrument)
        db.session.flush()
        db.session.add(
            PositionRegistration(
                account_id=account_id,
                instrument_id=instrument.id,
                tracking_mode="statement_valued",
                opening_date=date(2026, 9, 2),
            )
        )
        transaction = Transaction(
            portfolio_id=account.portfolio_id,
            transaction_type="deposit",
            effective_date=date(2026, 9, 2),
            status="posted",
        )
        db.session.add(transaction)
        db.session.flush()
        db.session.add(
            Posting(
                transaction_id=transaction.id,
                account_id=account_id,
                posting_kind="cash",
                currency_code="USD",
                cash_amount_delta=Decimal("1"),
            )
        )
        db.session.add(
            CashBalanceCheckpoint(
                account_id=account_id,
                currency_code="USD",
                effective_date=date(2026, 9, 2),
                confirmed_balance_amount=Decimal("1"),
                prior_calculated_balance_amount=Decimal("0"),
                correction_amount=Decimal("1"),
            )
        )
        db.session.commit()

    body = client.post(f"/accounts/{account_id}/archive").get_data(as_text=True)
    assert "This account cannot be archived as empty" in body
    assert "positions" in body
    assert "activity history" in body
    assert "cash confirmations" in body
    with app.app_context():
        assert db.session.get(Account, account_id).is_active


def test_archive_blocks_cash_settlement_relationship(
    app: Flask, client: FlaskClient
) -> None:
    institution_id, account_id = _records(app)
    with app.app_context():
        account = db.session.get(Account, account_id)
        routed = Account(
            portfolio_id=account.portfolio_id,
            institution_id=institution_id,
            name="Brokerage USD",
            account_type="brokerage",
            default_currency_code="USD",
            is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            cash_settlement_account_id=account_id,
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add(routed)
        db.session.commit()

    body = client.get(f"/accounts/{account_id}/archive").get_data(as_text=True)
    assert "cash settlement routing" in body
    assert "Archive empty account</button>" not in body


def test_guide_is_task_led_and_links_to_routine_paths(
    app: Flask, client: FlaskClient
) -> None:
    _records(app)
    body = client.get("/guide").get_data(as_text=True)
    assert "<h1>Guide</h1>" in body
    assert "Institution" in body and "Account" in body and "Holding" in body
    assert 'href="/accounts/new"' in body
    assert 'href="/positions/new"' in body
    assert 'href="/positions/new?kind=fixed_deposit"' in body
    assert 'aria-label="Setup progress"' not in body


def test_activity_chooser_exposes_the_dedicated_fixed_deposit_path(
    app: Flask, client: FlaskClient
) -> None:
    _records(app)
    body = client.get("/activity/new").get_data(as_text=True)
    assert 'href="/positions/new?kind=fixed_deposit"' in body
    assert "This is not a Buy" in body


def test_routine_new_instrument_disclosure_reopens_on_ticker_error(
    app: Flask, client: FlaskClient
) -> None:
    """The routine Add-holding disclosure re-opens on a
    ticker_or_isin error, matching the setup path."""
    _, account_id = _records(app)
    created = client.post("/setup/positions", data={
        "account_id": str(account_id), "instrument_id": "__new__",
        "new_instrument_name": "Global Fund", "instrument_type": "fund",
        "valuation_currency_code": "usd", "tracking_mode": "transaction_tracked",
        "effective_date": "2026-09-01", "opening_quantity": "10",
        "save_position": "Add position",
    })
    assert created.status_code == 302

    body = client.post("/positions/new", data={
        "account_id": str(account_id), "instrument_id": "1",
        "new_instrument_name": "", "ticker_or_isin": "X" * 65,
        "instrument_type": "fund", "valuation_currency_code": "usd",
        "tracking_mode": "transaction_tracked", "effective_date": "2026-09-02",
        "opening_quantity": "5", "save_position": "Add holding",
    }).get_data(as_text=True)
    assert "There is a problem" in body
    details = re.search(r'<details class="disclosure"[^>]*>', body).group(0)
    assert "open" in details
    assert 'id="ticker_or_isin-error"' in body


def test_access_disclosure_reopens_on_its_field_errors(
    app: Flask, client: FlaskClient
) -> None:
    institution_id, _ = _records(app)
    body = client.post(
        "/accounts/new",
        data=_account_data(institution_id, earliest_access_date="not-a-date"),
    ).get_data(as_text=True)
    assert "There is a problem" in body
    # The disclosure holding the failing field re-opens server-side;
    # without this the error is trapped in a closed <details>.
    details = re.search(r'<details class="disclosure"[^>]*>', body).group(0)
    assert "open" in details
    assert 'id="earliest_access_date-error"' in body


def test_fixed_deposit_form_carries_the_activity_switcher(
    app: Flask, client: FlaskClient
) -> None:
    """FD onboarding is part of the activity family; the plain
    Add-holding form is not, so only the FD form shows the switcher."""
    _, account_id = _records(app)
    fd = client.get(f"/positions/new?account={account_id}&kind=fixed_deposit").get_data(as_text=True)
    switcher = fd.split('aria-label="Activity type"', 1)[1].split("</nav>", 1)[0]
    assert '<strong aria-current="page">Fixed deposit</strong>' in switcher
    assert 'href="/activity/new?type=buy"' in switcher

    plain = client.get(f"/positions/new?account={account_id}").get_data(as_text=True)
    assert 'aria-label="Activity type"' not in plain
