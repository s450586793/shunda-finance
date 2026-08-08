from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class NormalizedInvoiceRow:
    direction: str
    invoice_number: str
    seller_tax_id: str
    seller_name: str
    buyer_tax_id: str
    buyer_name: str
    issue_date: date
    total_amount: Decimal
    status: str


@dataclass(frozen=True)
class NormalizedTransactionRow:
    channel: str
    direction: str
    occurred_at: datetime
    amount: Decimal
    balance_after: Decimal | None
    account_identifier: str
    counterparty_name: str
    counterparty_account: str
    transaction_id: str
    summary: str


@dataclass(frozen=True)
class RowIssue:
    code: str
    message: str
    field: str = ""


@dataclass(frozen=True)
class ParsedRow:
    row_number: int
    raw_data: dict[str, Any]
    normalized: NormalizedInvoiceRow | NormalizedTransactionRow | None
    issues: tuple[RowIssue, ...] = ()


@dataclass(frozen=True)
class ImportResult:
    batch_id: UUID
    posted_rows: int
    duplicate_rows: int
    error_rows: int

    @classmethod
    def from_batch(cls, batch):
        return cls(
            batch_id=batch.id,
            posted_rows=batch.rows.filter(posted_at__isnull=False).count(),
            duplicate_rows=batch.duplicate_rows,
            error_rows=batch.error_rows,
        )

    def as_dict(self):
        return {
            "batch_id": str(self.batch_id),
            "posted_rows": self.posted_rows,
            "duplicate_rows": self.duplicate_rows,
            "error_rows": self.error_rows,
        }


class UnsupportedTemplateError(ValueError):
    pass


class DuplicateSourceFileError(ValueError):
    pass


class RowValidationError(ValueError):
    pass
