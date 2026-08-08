from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apps.imports.fingerprints import transaction_fingerprint
from apps.imports.types import NormalizedTransactionRow


def test_transaction_fingerprint_uses_canonical_business_fields_only():
    first = _transaction(amount=Decimal("12.3"), balance_after=Decimal("999.99"))
    second = _transaction(amount=Decimal("12.30"), balance_after=Decimal("0.00"))

    assert transaction_fingerprint(first) == transaction_fingerprint(second)


def test_transaction_id_fingerprint_ignores_display_and_payment_fields():
    first = _transaction()
    second = _transaction(
        direction="inflow",
        occurred_at=datetime(2026, 7, 8, 12, 30, tzinfo=UTC),
        amount=Decimal("987.65"),
        balance_after=Decimal("1234.56"),
        counterparty_name="另一种展示名称",
        counterparty_account="other-counterparty-account",
        summary="另一段备注",
        transaction_id="  tx-001  ",
    )

    assert transaction_fingerprint(first) == transaction_fingerprint(second)


@pytest.mark.parametrize(
    "overrides",
    [
        {"channel": "wechat"},
        {"account_identifier": "account-002"},
    ],
)
def test_transaction_id_fingerprint_keeps_channel_and_funding_account_identity(overrides):
    assert transaction_fingerprint(_transaction()) != transaction_fingerprint(
        _transaction(**overrides)
    )


def test_missing_transaction_id_uses_fallback_fields_but_not_summary_or_balance():
    first = _transaction(transaction_id="", balance_after=Decimal("999.99"))
    display_only_change = _transaction(
        transaction_id="   ",
        balance_after=Decimal("0.00"),
        summary="更新后的备注",
    )
    changed_amount = _transaction(transaction_id="", amount=Decimal("12.31"))

    assert transaction_fingerprint(first) == transaction_fingerprint(display_only_change)
    assert transaction_fingerprint(first) != transaction_fingerprint(changed_amount)


def test_transaction_fingerprint_normalizes_equivalent_datetime_and_text():
    utc_row = _transaction()
    offset_row = _transaction(
        occurred_at=datetime(2026, 7, 1, 17, 0, tzinfo=timezone(timedelta(hours=8))),
        account_identifier="  ACCOUNT-001 ",
        counterparty_name="测试　单位",
    )

    assert transaction_fingerprint(utc_row) == transaction_fingerprint(offset_row)


@pytest.mark.parametrize(
    "occurred_at", [datetime.fromisoformat("2026-07-01T09:00:00"), "2026-07-01"]
)
def test_transaction_fingerprint_rejects_non_aware_datetime(occurred_at):
    with pytest.raises(ValueError, match="发生时间"):
        transaction_fingerprint(_transaction(occurred_at=occurred_at))


def _transaction(**overrides):
    defaults = {
        "channel": "bank",
        "direction": "outflow",
        "occurred_at": datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        "amount": Decimal("12.30"),
        "balance_after": Decimal("100.00"),
        "account_identifier": "account-001",
        "counterparty_name": "测试 单位",
        "counterparty_account": "6222 0000",
        "transaction_id": "TX-001",
        "summary": "货款",
    }
    defaults.update(overrides)
    return NormalizedTransactionRow(**defaults)
