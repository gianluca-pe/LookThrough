"""Holdings UI tests: separate Investments and Cash cards
with subtotals, and server-side sortable investment columns. Data comes from
the shared portfolio summary; the template never combines or recomputes it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from flask import Flask
from flask.testing import FlaskClient

from app.extensions import db
from app.models import Account, FxRate, Institution

from test_overview_ui import (
    _account,
    _confirm,
    _portfolio,
    _statement_position,
    _usd_account,
    assert_no_inline_script,
)


def test_holdings_shows_investments_and_cash_cards_with_subtotals(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, value="10000")
        _confirm(portfolio, account, "2000")
    body = client.get("/holdings").get_data(as_text=True)
    assert 'id="investments-heading"' in body and 'id="cash-heading"' in body
    investments = body.split('id="investments-heading"', 1)[1]
    assert "Investments subtotal" in investments and "10,000.00" in investments
    cash = body.split('id="cash-heading"', 1)[1]
    assert "<strong>Brokerage</strong>" in cash and "2,000.00" in cash
    assert "Cash subtotal" in cash
    assert "confirmed" in cash
    assert_no_inline_script(body)


def test_holdings_cash_missing_fx_is_missing_with_targeted_link(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, value="10000")
        _confirm(portfolio, _usd_account(portfolio), "5000")
    body = client.get("/holdings").get_data(as_text=True)
    cash = body.split('id="cash-heading"', 1)[1]
    assert "No eligible FX path" in cash
    assert 'href="/values?base=USD&amp;quote=EUR#fx-heading"' in cash
    # Unconverted cash is never shown as zero: the subtotal is a labelled gap.
    subtotal = cash.split("Cash subtotal", 1)[1].split("</tr>", 1)[0]
    assert '<span class="money-missing">—</span>' in subtotal
    assert "0.00" not in subtotal


def test_holdings_confirmed_zero_cash_is_a_real_value(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, value="10000")
        _confirm(portfolio, account, "0")
    body = client.get("/holdings").get_data(as_text=True)
    cash = body.split('id="cash-heading"', 1)[1]
    assert "0.00" in cash and "1 cash balances valued" in cash


def test_holdings_sort_by_reporting_value_desc_missing_last(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, name="Alpha", value="100")
        _statement_position(portfolio, account, name="Zulu", value="5000")
        _statement_position(portfolio, account, name="Unvalued", value=None)
    body = client.get("/holdings?sort=reporting&direction=desc").get_data(as_text=True)
    assert 'aria-sort="descending"' in body
    zulu = body.index('<th scope="row">Zulu</th>')
    alpha = body.index('<th scope="row">Alpha</th>')
    unvalued = body.index('<th scope="row">Unvalued</th>')
    assert zulu < alpha < unvalued  # missing is never sorted as zero
    # The header link toggles back to ascending, keeps the as-of date, and
    # returns to the card rather than the top of the page.
    assert "sort=reporting&amp;direction=asc&amp;as_of=2026-08-02" in body
    assert "#investments-heading" in body


def test_holdings_sort_by_classification_groups_canonical_roles(
    client: FlaskClient, app: Flask
) -> None:
    from test_overview_ui import _classified_position
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _classified_position(
            portfolio, account, value="100", role="liquidity",
            bucket="bridge", name="Zulu liquidity",
        )
        _classified_position(
            portfolio, account, value="100", role="equity",
            bucket="growth", name="Alpha equity growth",
        )
        _classified_position(
            portfolio, account, value="100", role="equity",
            bucket="now", name="Beta equity now",
        )
        _classified_position(
            portfolio, account, value="100", role="income",
            bucket="now", name="Mike income",
        )
        _classified_position(
            portfolio, account, value="100", role=None, name="Unclassified"
        )
    body = client.get("/holdings?sort=classification").get_data(as_text=True)
    assert 'aria-sort="ascending"' in body
    equity_now = body.index('<th scope="row">Beta equity now</th>')
    equity_growth = body.index('<th scope="row">Alpha equity growth</th>')
    income = body.index('<th scope="row">Mike income</th>')
    liquidity = body.index('<th scope="row">Zulu liquidity</th>')
    unclassified = body.index('<th scope="row">Unclassified</th>')
    # Canonical Overview role order, bucket order within a role, unknown last.
    assert equity_now < equity_growth < income < liquidity < unclassified
    assert "sort=classification&amp;direction=desc" in body

    # Descending reverses the known vocabulary but never lets the
    # unclassified tail lead the table.
    body = client.get("/holdings?sort=classification&direction=desc").get_data(
        as_text=True
    )
    positions = [
        body.index(f'<th scope="row">{name}</th>')
        for name in (
            "Beta equity now", "Alpha equity growth", "Mike income",
            "Zulu liquidity", "Unclassified",
        )
    ]
    assert positions[4] == max(positions)


def test_holdings_cash_sort_by_reporting_value_missing_last(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _confirm(portfolio, account, "100")
        _confirm(portfolio, _usd_account(portfolio), "5000")
    body = client.get("/holdings?cash_sort=reporting&cash_direction=desc").get_data(
        as_text=True
    )
    cash = body.split('id="cash-heading"', 1)[1]
    assert 'aria-sort="descending"' in cash
    # Unconverted USD cash is never a zero: it sorts last even descending.
    assert cash.index("<strong>Brokerage</strong>") < cash.index(
        "<strong>Dollar account</strong>"
    )
    assert "cash_direction=asc" in cash
    # The sort link returns to the Cash card, not the top of the page.
    assert "#cash-heading" in cash

    with app.app_context():
        db.session.add(FxRate(
            effective_date=date(2026, 8, 1), base_currency_code="USD",
            quote_currency_code="EUR", quote_per_base_amount=Decimal("2"),
        ))
        db.session.commit()
    body = client.get("/holdings?cash_sort=reporting&cash_direction=desc").get_data(
        as_text=True
    )
    cash = body.split('id="cash-heading"', 1)[1]
    # USD 5,000 × 2 = 10,000 EUR equivalent outranks EUR 100.
    assert cash.index("<strong>Dollar account</strong>") < cash.index(
        "<strong>Brokerage</strong>"
    )


def test_holdings_default_sort_is_instrument_ascending(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, name="Zulu", value="5000")
        _statement_position(portfolio, account, name="Alpha", value="100")
    for path in ("/holdings", "/holdings?sort=bogus&direction=sideways"):
        body = client.get(path).get_data(as_text=True)
        assert 'aria-sort="ascending"' in body
        assert body.index('<th scope="row">Alpha</th>') < body.index('<th scope="row">Zulu</th>')


def test_holdings_empty_state_unchanged(client: FlaskClient, app: Flask) -> None:
    with app.app_context():
        portfolio = _portfolio()
        _account(portfolio)
    body = client.get("/holdings").get_data(as_text=True)
    assert "No positions yet." in body
    assert 'id="cash-heading"' not in body


# --- Share/access row effects  ---

def _factored_account(portfolio, *, share="1", access="1", name="Joint account"):
    institution = Institution(portfolio_id=portfolio.id, name="Joint Bank")
    db.session.add(institution)
    db.session.flush()
    account = Account(
        portfolio_id=portfolio.id, institution_id=institution.id,
        name=name, account_type="brokerage",
        default_currency_code="EUR", is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal(share),
        present_access_decimal=Decimal(access),
        relationship_eligible=True, is_active=True,
    )
    db.session.add(account)
    db.session.commit()
    return account


def test_holdings_rows_show_non_default_share_access_effects(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        full = _account(portfolio)
        _statement_position(portfolio, full, name="Fully mine", value="5000")
        joint = _factored_account(portfolio, share="0.5", access="0.8")
        _statement_position(portfolio, joint, name="Shared fund", value="10000")
    body = client.get("/holdings").get_data(as_text=True)
    shared = body.split('<th scope="row">Shared fund</th>', 1)[1].split("</tr>", 1)[0]
    assert "included" in shared and "5,000.00" in shared
    assert "accessible now" in shared and "4,000.00" in shared
    own = body.split('<th scope="row">Fully mine</th>', 1)[1].split("</tr>", 1)[0]
    assert "included" not in own and "accessible now" not in own


def test_holdings_cash_row_uses_settlement_account_factors(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        institution = Institution(portfolio_id=portfolio.id, name="Bank")
        db.session.add(institution)
        db.session.flush()
        settlement = Account(
            portfolio_id=portfolio.id, institution_id=institution.id,
            name="Settlement EUR", account_type="brokerage",
            default_currency_code="EUR", is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            portfolio_share_decimal=Decimal("0.5"),
            present_access_decimal=Decimal("0.8"),
            relationship_eligible=True, is_active=True,
        )
        db.session.add(settlement)
        db.session.flush()
        custody = Account(
            portfolio_id=portfolio.id, institution_id=institution.id,
            name="Custody EUR", account_type="brokerage",
            default_currency_code="EUR", is_multicurrency=False,
            cash_tracking_mode="separate_cash",
            cash_settlement_account_id=settlement.id,
            portfolio_share_decimal=Decimal("1"),
            present_access_decimal=Decimal("1"),
            relationship_eligible=True, is_active=True,
        )
        db.session.add(custody)
        db.session.commit()
        _confirm(portfolio, settlement, "1000")
    body = client.get("/holdings").get_data(as_text=True)
    cash = body.split('id="cash-heading"', 1)[1]
    assert "<strong>Settlement EUR</strong>" in cash
    assert "Custody EUR" not in cash  # routed cash counted once, at settlement
    row = cash.split("<strong>Settlement EUR</strong>", 1)[1].split("</tr>", 1)[0]
    assert "included" in row and "500.00" in row
    assert "accessible now" in row and "400.00" in row
