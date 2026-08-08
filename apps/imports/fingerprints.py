import hashlib
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from unicodedata import normalize

from .types import NormalizedTransactionRow


def transaction_fingerprint(row: NormalizedTransactionRow) -> str:
    occurred_at = row.occurred_at
    if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
        raise ValueError("发生时间必须是带时区的 datetime")

    try:
        amount = Decimal(row.amount).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("金额必须是有效 Decimal") from exc

    transaction_id = _canonical_text(row.transaction_id)
    if transaction_id:
        fields = (
            "transaction-id",
            _canonical_text(row.channel),
            _canonical_text(row.account_identifier),
            transaction_id,
        )
    else:
        fields = (
            "fallback",
            _canonical_text(row.channel),
            _canonical_text(row.account_identifier),
            occurred_at.astimezone(UTC).isoformat(timespec="microseconds"),
            _canonical_text(row.direction),
            format(amount, "f"),
            _canonical_text(row.counterparty_name),
            _canonical_text(row.counterparty_account),
        )
    return hashlib.sha256("\x1f".join(fields).encode()).hexdigest()


def _canonical_text(value: object) -> str:
    return " ".join(normalize("NFKC", str(value)).split()).casefold()
