from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from apps.imports.choices import SourceKind
from apps.imports.types import (
    NormalizedInvoiceRow,
    NormalizedTransactionRow,
    ParsedRow,
    RowIssue,
)


@dataclass
class FakeImporter:
    rows: tuple[ParsedRow, ...] = ()
    error: Exception | None = None
    source_kinds: frozenset[str] = frozenset(SourceKind.values)
    parse_positions: list[int] = field(default_factory=list)

    def supports(self, filename, headers):
        return filename.endswith(".csv") and headers == ["marker"]

    def parse(self, file_obj):
        self.parse_positions.append(file_obj.tell())
        file_obj.read()
        if self.error:
            raise self.error
        return iter(self.rows)


def make_invoice_row(
    *,
    row_number,
    direction="input",
    invoice_number="INV-001",
    seller_tax_id="913200",
    normalized=...,
    issues=(),
):
    if normalized is ...:
        normalized = NormalizedInvoiceRow(
            direction=direction,
            invoice_number=invoice_number,
            seller_tax_id=seller_tax_id,
            seller_name="测试供应商",
            buyer_tax_id="913201",
            buyer_name="顺达",
            issue_date=date(2026, 7, 1),
            total_amount=Decimal("100.00"),
            status="normal",
        )
    return ParsedRow(
        row_number=row_number,
        raw_data={"row": row_number},
        normalized=normalized,
        issues=tuple(RowIssue(*issue) for issue in issues),
    )


def make_transaction_row(*, row_number, normalized=..., issues=()):
    if normalized is ...:
        normalized = NormalizedTransactionRow(
            channel="bank",
            direction="outflow",
            occurred_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
            amount=Decimal("100.00"),
            balance_after=Decimal("200.00"),
            account_identifier="bank-account",
            counterparty_name="测试收款方",
            counterparty_account="62220000",
            transaction_id="TX-001",
            summary="货款",
        )
    return ParsedRow(
        row_number=row_number,
        raw_data={"row": row_number},
        normalized=normalized,
        issues=tuple(RowIssue(*issue) for issue in issues),
    )
