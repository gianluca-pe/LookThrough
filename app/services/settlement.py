"""Direct custody-to-cash settlement routing for business activities."""

from __future__ import annotations

from app.models import Account


class SettlementAccountError(ValueError):
    """An invalid persisted or requested settlement-account relationship."""


def validate_cash_settlement_account(
    source: Account, target: Account | None
) -> None:
    """Require a bounded direct same-institution, same-currency cash destination."""

    if target is None:
        return
    if source.cash_tracking_mode != "separate_cash":
        raise SettlementAccountError(
            "A custody account must use separate cash tracking before its cash can "
            "settle in another account."
        )
    if target.id == source.id:
        raise SettlementAccountError(
            "Choose ‘Cash stays in this account’ instead of linking the account to itself."
        )
    if not target.is_active:
        raise SettlementAccountError("Choose an active cash settlement account.")
    if target.portfolio_id != source.portfolio_id:
        raise SettlementAccountError(
            "Cash settlement must stay inside the same portfolio."
        )
    if target.institution_id != source.institution_id:
        raise SettlementAccountError(
            "Cash settlement must stay inside the same institution."
        )
    if source.is_multicurrency:
        raise SettlementAccountError(
            "A multicurrency custody account cannot use one settlement account. "
            "Keep cash in the account for now."
        )
    if target.is_multicurrency or (
        target.default_currency_code != source.default_currency_code
    ):
        raise SettlementAccountError(
            "Cash settlement must use a single-currency account in the same currency."
        )
    if target.cash_tracking_mode != "separate_cash":
        raise SettlementAccountError(
            "The settlement account must track cash separately."
        )
    if target.cash_settlement_account_id is not None:
        raise SettlementAccountError(
            "Settlement links must be direct; the destination cannot route cash again."
        )


def cash_settlement_account(source: Account, currency_code: str) -> Account:
    """Resolve the account whose cash balance receives an activity's cash effect."""

    target = source.cash_settlement_account
    validate_cash_settlement_account(source, target)
    resolved = target or source
    if not resolved.is_multicurrency and resolved.default_currency_code != currency_code:
        raise SettlementAccountError(
            f"Cash settlement account {resolved.name} does not support {currency_code}."
        )
    return resolved
