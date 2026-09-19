"""Fixed-deposit maturity disposition, reversal, and browser-flow contract."""

from datetime import date
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select

from app.extensions import db
from app.models import (
    Account,
    FixedDeposit,
    Institution,
    Instrument,
    Portfolio,
    PositionRegistration,
    Posting,
    Transaction,
    ValuationObservation,
)
from app.services.activity_history import ReversalCommand, get_activity, reverse_activity
from app.services.cash import resolve_cash
from app.services.fixed_deposits import (
    FixedDepositCommand,
    FixedDepositValidationError,
    MaturityDispositionCommand,
    post_maturity_disposition,
    preview_maturity_disposition,
    save_fixed_deposit_terms,
)
from app.services.portfolio_summary import build_portfolio_summary


MATURITY = date(2026, 8, 10)
AS_OF = date(2026, 8, 28)


def _records() -> tuple[Portfolio, PositionRegistration, Account]:
    portfolio = Portfolio(
        name="Portfolio",
        reporting_currency_code="USD",
        annual_spending_amount=Decimal("48000"),
        annual_spending_currency_code="USD",
        default_as_of_date=AS_OF,
    )
    db.session.add(portfolio)
    db.session.flush()
    institution = Institution(portfolio_id=portfolio.id, name="Bank")
    db.session.add(institution)
    db.session.flush()
    fd_account = Account(
        portfolio_id=portfolio.id,
        institution_id=institution.id,
        name="Fixed Deposits",
        account_type="deposit",
        default_currency_code="USD",
        is_multicurrency=False,
        cash_tracking_mode="included_in_aggregate",
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        relationship_eligible=True,
        is_active=True,
    )
    cash_account = Account(
        portfolio_id=portfolio.id,
        institution_id=institution.id,
        name="USD Savings",
        account_type="cash",
        default_currency_code="USD",
        is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        relationship_eligible=True,
        is_active=True,
    )
    db.session.add_all([fd_account, cash_account])
    db.session.flush()
    instrument = Instrument(
        portfolio_id=portfolio.id,
        name="FD_USD_203",
        ticker_or_isin="203",
        instrument_type="fixed_deposit",
        valuation_currency_code="USD",
        is_active=True,
    )
    db.session.add(instrument)
    db.session.flush()
    registration = PositionRegistration(
        account_id=fd_account.id,
        instrument_id=instrument.id,
        tracking_mode="statement_valued",
        opening_date=date(2026, 5, 8),
    )
    db.session.add(registration)
    db.session.flush()
    db.session.add(
        ValuationObservation(
            position_registration_id=registration.id,
            effective_date=date(2026, 5, 8),
            native_value_amount=Decimal("85000"),
            currency_code="USD",
        )
    )
    save_fixed_deposit_terms(
        portfolio.id,
        FixedDepositCommand(
            registration_id=registration.id,
            currency_code="USD",
            start_date=date(2026, 5, 8),
            maturity_date=MATURITY,
            annual_rate_decimal=None,
            maturity_action="return_to_cash",
            expected_maturity_proceeds_amount=Decimal("85690.25"),
            notes="Bank confirmation 203",
        ),
    )
    db.session.commit()
    return portfolio, registration, cash_account


def _command(
    portfolio: Portfolio,
    registration: PositionRegistration,
    cash_account: Account,
    **changes,
) -> MaturityDispositionCommand:
    values = {
        "portfolio_id": portfolio.id,
        "registration_id": registration.id,
        "disposition_type": "return_to_cash",
        "effective_date": MATURITY,
        "confirmed_principal_amount": Decimal("85000"),
        "confirmed_interest_amount": Decimal("690.25"),
        "cash_account_id": cash_account.id,
        "note": "Bank maturity statement",
    }
    values.update(changes)
    return MaturityDispositionCommand(**values)


def test_return_to_cash_uses_confirmed_amounts_and_reversal_restores_state(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, registration, cash_account = _records()
        preview = preview_maturity_disposition(
            _command(portfolio, registration, cash_account)
        )
        assert preview.cash_effect_amount == Decimal("85690.25")
        assert preview.expected_difference_amount == Decimal("0")

        posted = post_maturity_disposition(
            _command(portfolio, registration, cash_account)
        )
        transaction = db.session.get(Transaction, posted.transaction_id)
        effects = list(
            db.session.scalars(
                select(Posting)
                .where(Posting.transaction_id == transaction.id)
                .order_by(Posting.posting_kind)
            )
        )
        assert transaction.transaction_type == "fixed_deposit_maturity"
        assert {row.posting_kind for row in effects} == {"cash", "clearing", "income"}
        assert next(row for row in effects if row.posting_kind == "clearing").cash_amount_delta == Decimal("-85000")
        assert next(row for row in effects if row.posting_kind == "income").cash_amount_delta == Decimal("690.25")
        assert registration.closing_date == MATURITY
        assert resolve_cash(portfolio.id, cash_account.id, "USD", AS_OF).amount == Decimal("85690.25")
        assert build_portfolio_summary(portfolio, AS_OF)["holdings"] == []
        history = get_activity(transaction.id, portfolio_id=portfolio.id)
        assert history.activity_label == "Fixed-deposit maturity"
        assert history.instrument_name == "FD_USD_203"
        assert history.cash_effect == Decimal("85690.25")

        reverse_activity(
            ReversalCommand(
                portfolio_id=portfolio.id,
                transaction_id=transaction.id,
                reason="Bank corrected the maturity statement",
            )
        )
        assert registration.closing_date is None
        assert resolve_cash(portfolio.id, cash_account.id, "USD", AS_OF).amount == Decimal("0")
        restored = build_portfolio_summary(portfolio, AS_OF)
        assert [row["instrument_name"] for row in restored["holdings"]] == ["FD_USD_203"]
        assert restored["actionable_attention_items"][0]["action_kind"] == "fixed_deposit_disposition"


def test_rollover_creates_successor_and_only_interest_reaches_cash(
    app: Flask,
) -> None:
    with app.app_context():
        portfolio, registration, cash_account = _records()
        command = _command(
            portfolio,
            registration,
            cash_account,
            disposition_type="rollover",
            confirmed_interest_amount=Decimal("714.66"),
            successor_instrument_name="FD_USD_207",
            successor_reference="207",
            successor_maturity_date=date(2026, 11, 10),
            successor_annual_rate_decimal=Decimal("0.031"),
            successor_expected_proceeds_amount=Decimal("85650"),
            successor_maturity_action="undecided",
        )
        posted = post_maturity_disposition(command)
        successor = db.session.get(
            PositionRegistration, posted.successor_registration_id
        )
        observation = db.session.scalar(
            select(ValuationObservation).where(
                ValuationObservation.position_registration_id == successor.id
            )
        )
        successor_terms = db.session.scalar(
            select(FixedDeposit).where(
                FixedDeposit.position_registration_id == successor.id
            )
        )
        assert registration.closing_date == MATURITY
        assert successor.instrument.name == "FD_USD_207"
        assert successor.instrument.ticker_or_isin == "207"
        assert observation.native_value_amount == Decimal("85000")
        assert successor_terms.expected_maturity_proceeds_amount == Decimal("85650")
        assert resolve_cash(portfolio.id, cash_account.id, "USD", AS_OF).amount == Decimal("714.66")
        current = build_portfolio_summary(portfolio, AS_OF)
        assert [row["instrument_name"] for row in current["holdings"]] == ["FD_USD_207"]

        reverse_activity(
            ReversalCommand(
                portfolio_id=portfolio.id,
                transaction_id=posted.transaction_id,
                reason="Rollover entered in error",
            )
        )
        assert registration.closing_date is None
        assert successor.closing_date == MATURITY
        assert [
            row["instrument_name"]
            for row in build_portfolio_summary(portfolio, AS_OF)["holdings"]
        ] == ["FD_USD_203"]
        assert resolve_cash(portfolio.id, cash_account.id, "USD", AS_OF).amount == Decimal("0")


def test_manual_disposition_requires_note_and_does_not_invent_cash(app: Flask) -> None:
    with app.app_context():
        portfolio, registration, cash_account = _records()
        try:
            preview_maturity_disposition(
                _command(
                    portfolio,
                    registration,
                    cash_account,
                    disposition_type="manual",
                    cash_account_id=None,
                    note="",
                )
            )
        except FixedDepositValidationError as exc:
            assert exc.field == "note"
        else:
            raise AssertionError("Expected a required manual note")

        posted = post_maturity_disposition(
            _command(
                portfolio,
                registration,
                cash_account,
                disposition_type="manual",
                cash_account_id=None,
                note="Principal moved to an external account not tracked here",
            )
        )
        assert posted.preview.cash_effect_amount == Decimal("0")
        assert resolve_cash(portfolio.id, cash_account.id, "USD", AS_OF).amount == Decimal("0")


def _route_data(cash_account_id: int, **changes) -> dict[str, str]:
    values = {
        "disposition_type": "return_to_cash",
        "effective_date": "2026-08-10",
        "confirmed_principal_amount": "85000",
        "confirmed_interest_amount": "690.25",
        "cash_account_id": str(cash_account_id),
        "note": "Bank statement",
        "successor_instrument_name": "",
        "successor_reference": "",
        "successor_maturity_date": "",
        "successor_annual_rate_percent": "",
        "successor_expected_proceeds_amount": "",
        "successor_maturity_action": "undecided",
        **changes,
    }
    return values


def test_no_js_route_previews_then_posts_and_attention_targets_it(
    app: Flask, client: FlaskClient
) -> None:
    with app.app_context():
        portfolio, registration, cash_account = _records()
        registration_id = registration.id
        cash_account_id = cash_account.id

    overview = client.get("/overview?as_of=2026-08-28").get_data(as_text=True)
    assert f'/positions/{registration_id}/fixed-deposit/disposition?as_of=2026-08-28' in overview

    preview = client.post(
        f"/positions/{registration_id}/fixed-deposit/disposition?as_of=2026-08-28",
        data=_route_data(cash_account_id, preview_disposition="Preview"),
    )
    body = preview.get_data(as_text=True)
    assert preview.status_code == 200
    assert "Check before recording" in body
    assert "USD</span> 85,690.25" in body
    assert "Record maturity disposition" in body

    posted = client.post(
        f"/positions/{registration_id}/fixed-deposit/disposition?as_of=2026-08-28",
        data=_route_data(cash_account_id, confirm_disposition="Record"),
    )
    assert posted.status_code == 302
    assert "/activity/" in posted.headers["Location"]
    with app.app_context():
        assert db.session.query(Transaction).filter_by(
            transaction_type="fixed_deposit_maturity"
        ).count() == 1
