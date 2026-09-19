"""Backend contract for bounded account onboarding and maintenance."""

from datetime import date
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import func, select

from app.accounts import _index_accounts
from app.extensions import db
from app.models import Account, CashBalanceCheckpoint, Institution, Portfolio, Transaction


def _records(*, mode: str = "separate_cash") -> tuple[Portfolio, Account]:
    portfolio = Portfolio(
        name="Portfolio",
        reporting_currency_code="EUR",
        annual_spending_amount=Decimal("48000"),
    )
    db.session.add(portfolio)
    db.session.flush()
    institution = Institution(portfolio_id=portfolio.id, name="Bank")
    db.session.add(institution)
    db.session.flush()
    account = Account(
        portfolio_id=portfolio.id,
        institution_id=institution.id,
        name="Everyday savings",
        reference="OLD-REF",
        account_type="cash",
        default_currency_code="EUR",
        is_multicurrency=False,
        cash_tracking_mode=mode,
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        relationship_eligible=True,
        is_active=True,
    )
    db.session.add(account)
    db.session.commit()
    return portfolio, account


def _edit_data(**overrides: str) -> dict[str, str]:
    data = {
        "name": "Daily banking",
        "reference": "ACC-123",
        "cash_tracking_mode": "included_in_aggregate",
        "save_account": "Save account",
    }
    data.update(overrides)
    return data


def _old_checkpoint(account: Account) -> CashBalanceCheckpoint:
    checkpoint = CashBalanceCheckpoint(
        account_id=account.id,
        currency_code="EUR",
        effective_date=date(2026, 6, 30),
        confirmed_balance_amount=Decimal("1000"),
        prior_calculated_balance_amount=Decimal("0"),
        correction_amount=Decimal("1000"),
        source_note="Old confirmation",
    )
    db.session.add(checkpoint)
    db.session.commit()
    return checkpoint


def test_edit_get_prefills_bounded_account_fields(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, account = _records()
        account_id = account.id

    body = client.get(f"/accounts/{account_id}/edit").get_data(as_text=True)

    assert 'value="Everyday savings"' in body
    assert 'value="OLD-REF"' in body
    assert '<option selected value="separate_cash">' in body
    # Share/access fields render stored factors as percentages.
    assert 'id="portfolio_share_percent"' in body
    assert 'id="present_access_percent"' in body
    assert 'id="earliest_access_date"' in body
    assert 'id="access_note"' in body


def test_separate_to_aggregate_preserves_but_excludes_confirmation(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, account = _records()
        old_id = _old_checkpoint(account).id
        account_id = account.id

    response = client.post(f"/accounts/{account_id}/edit", data=_edit_data())

    assert response.status_code == 302
    with app.app_context():
        account = db.session.get(Account, account_id)
        old = db.session.get(CashBalanceCheckpoint, old_id)
        assert account.name == "Daily banking"
        assert account.reference == "ACC-123"
        assert account.cash_tracking_mode == "included_in_aggregate"
        assert old.superseded_at is None
        assert db.session.scalar(select(func.count(Transaction.id))) == 0

    detail = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "no longer count" in detail
    assert "No separate cash balance is added" in detail


def test_aggregate_to_separate_requires_fresh_confirmation(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, account = _records(mode="included_in_aggregate")
        old_id = _old_checkpoint(account).id
        account_id = account.id

    response = client.post(
        f"/accounts/{account_id}/edit",
        data=_edit_data(cash_tracking_mode="separate_cash"),
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Fresh confirmed balance is required" in body
    assert "Confirmation date is required" in body
    with app.app_context():
        assert db.session.get(Account, account_id).cash_tracking_mode == "included_in_aggregate"
        assert db.session.get(CashBalanceCheckpoint, old_id).superseded_at is None


def test_aggregate_to_separate_supersedes_old_and_confirms_atomically(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, account = _records(mode="included_in_aggregate")
        old_id = _old_checkpoint(account).id
        account_id = account.id

    response = client.post(
        f"/accounts/{account_id}/edit",
        data=_edit_data(
            cash_tracking_mode="separate_cash",
            currency_code="eur",
            confirmed_balance_amount="1250",
            effective_date="2026-08-08",
            source_note="Current online balance",
        ),
    )

    assert response.status_code == 302
    with app.app_context():
        account = db.session.get(Account, account_id)
        rows = list(
            db.session.scalars(
                select(CashBalanceCheckpoint)
                .where(CashBalanceCheckpoint.account_id == account_id)
                .order_by(CashBalanceCheckpoint.id)
            )
        )
        assert account.cash_tracking_mode == "separate_cash"
        assert len(rows) == 2
        assert rows[0].id == old_id and rows[0].superseded_at is not None
        assert rows[1].superseded_at is None
        assert rows[1].confirmed_balance_amount == Decimal("1250")
        assert rows[1].source_note == "Current online balance"
        assert db.session.scalar(select(func.count(Transaction.id))) == 0


def test_invalid_fresh_confirmation_rolls_back_mode_and_supersession(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        _, account = _records(mode="included_in_aggregate")
        old_id = _old_checkpoint(account).id
        account_id = account.id

    response = client.post(
        f"/accounts/{account_id}/edit",
        data=_edit_data(
            cash_tracking_mode="separate_cash",
            currency_code="USD",
            confirmed_balance_amount="1250",
            effective_date="2026-08-08",
        ),
    )

    assert response.status_code == 200
    assert "Currency must match this single-currency account" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Account, account_id).cash_tracking_mode == "included_in_aggregate"
        assert db.session.get(CashBalanceCheckpoint, old_id).superseded_at is None
        assert db.session.scalar(select(func.count(CashBalanceCheckpoint.id))) == 1


def test_account_edit_saves_direct_same_institution_cash_settlement_link(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, brokerage = _records()
        brokerage.account_type = "brokerage"
        settlement = Account(
            portfolio_id=portfolio.id,
            institution_id=brokerage.institution_id,
            name="Settlement EUR",
            account_type="cash",
            default_currency_code="EUR",
            is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add(settlement)
        db.session.commit()
        brokerage_id = brokerage.id
        settlement_id = settlement.id

    response = client.post(
        f"/accounts/{brokerage_id}/edit",
        data=_edit_data(
            cash_tracking_mode="separate_cash",
            cash_settlement_account_id=str(settlement_id),
        ),
    )

    assert response.status_code == 302
    with app.app_context():
        brokerage = db.session.get(Account, brokerage_id)
        assert brokerage.cash_settlement_account_id == settlement_id


def test_settlement_destination_cannot_switch_to_aggregate(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, settlement = _records()
        source = Account(
            portfolio_id=portfolio.id,
            institution_id=settlement.institution_id,
            name="Brokerage EUR",
            account_type="brokerage",
            default_currency_code="EUR",
            is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            cash_settlement_account_id=settlement.id,
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add(source)
        db.session.commit()
        settlement_id = settlement.id

    response = client.post(
        f"/accounts/{settlement_id}/edit",
        data=_edit_data(cash_tracking_mode="included_in_aggregate"),
    )

    assert response.status_code == 200
    assert "receives activity cash" in response.get_data(as_text=True)
    with app.app_context():
        assert (
            db.session.get(Account, settlement_id).cash_tracking_mode
            == "separate_cash"
        )


def test_older_edit_form_omission_does_not_clear_existing_settlement_link(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, source = _records()
        target = Account(
            portfolio_id=portfolio.id,
            institution_id=source.institution_id,
            name="Settlement EUR",
            account_type="cash",
            default_currency_code="EUR",
            is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add(target)
        db.session.flush()
        source.cash_settlement_account_id = target.id
        db.session.commit()
        source_id = source.id
        target_id = target.id

    response = client.post(
        f"/accounts/{source_id}/edit",
        data=_edit_data(cash_tracking_mode="separate_cash"),
    )

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Account, source_id).cash_settlement_account_id == target_id


def test_account_sort_contract_is_server_side_and_stable(app: Flask) -> None:
    with app.app_context():
        portfolio, first = _records(mode="separate_cash")
        second = Account(
            portfolio_id=portfolio.id,
            institution_id=first.institution_id,
            name="Aggregate wrapper",
            account_type="retirement",
            default_currency_code="EUR",
            is_multicurrency=False,
            cash_tracking_mode="included_in_aggregate",
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True,
            is_active=True,
        )
        db.session.add(second)
        db.session.commit()

        with app.test_request_context("/accounts?sort=cash&direction=asc"):
            accounts, sort_by, direction = _index_accounts(portfolio.id)

        assert sort_by == "cash" and direction == "asc"
        assert [row.cash_tracking_mode for row in accounts] == [
            "included_in_aggregate",
            "separate_cash",
        ]
