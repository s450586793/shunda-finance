from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import UUID

from apps.ledger.choices import InvoiceDirection, InvoiceStatus, MoneyDirection
from apps.ledger.models import Invoice, MoneyTransaction

from .queries import invoice_open_amount, transaction_open_amount


@dataclass(frozen=True)
class Candidate:
    kind: str
    transaction_ids: tuple[UUID, ...]
    invoice_ids: tuple[UUID, ...]
    total: Decimal
    difference: Decimal
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class AvailableTransaction:
    id: UUID
    occurred_at: datetime
    open_amount: Decimal


def transaction_candidates(invoice_id, *, start, end):
    _validate_date_window(start, end)
    invoice = Invoice.objects.select_related("counterparty").get(pk=invoice_id)
    target = invoice_open_amount(invoice.id)
    if target <= 0:
        return []

    items = list(available_transactions_for_invoice(invoice, start=start, end=end))
    exact = [
        Candidate(
            kind="CONTIGUOUS_EXACT",
            transaction_ids=tuple(item.id for item in window),
            invoice_ids=(invoice.id,),
            total=total,
            difference=Decimal("0.00"),
            start_at=window[0].occurred_at,
            end_at=window[-1].occurred_at,
        )
        for window, total in _contiguous_windows(items, target)
    ]
    if exact:
        return exact
    if not items:
        return []

    available_total = sum((item.open_amount for item in items), Decimal("0.00"))
    return [
        Candidate(
            kind="PARTIAL",
            transaction_ids=tuple(item.id for item in items),
            invoice_ids=(invoice.id,),
            total=min(target, available_total),
            difference=max(target - available_total, Decimal("0.00")),
            start_at=items[0].occurred_at,
            end_at=items[-1].occurred_at,
        )
    ]


def available_transactions_for_invoice(invoice, *, start, end):
    expected_direction = (
        MoneyDirection.OUTFLOW
        if invoice.direction == InvoiceDirection.INPUT
        else MoneyDirection.INFLOW
    )
    query = MoneyTransaction.objects.filter(
        counterparty=invoice.counterparty,
        direction=expected_direction,
        occurred_at__date__range=(start, end),
    ).order_by("occurred_at", "id")
    for money in query:
        open_amount = transaction_open_amount(money.id)
        if open_amount > 0:
            yield AvailableTransaction(money.id, money.occurred_at, open_amount)


def invoice_candidates(transaction_id, *, start, end):
    _validate_date_window(start, end)
    money = MoneyTransaction.objects.select_related("counterparty").get(pk=transaction_id)
    target = transaction_open_amount(money.id)
    if target <= 0 or money.counterparty_id is None:
        return []

    direction = (
        InvoiceDirection.INPUT
        if money.direction == MoneyDirection.OUTFLOW
        else InvoiceDirection.OUTPUT
    )
    query = Invoice.objects.filter(
        counterparty=money.counterparty,
        direction=direction,
        issue_date__range=(start, end),
        status=InvoiceStatus.NORMAL,
    ).order_by("issue_date", "id")
    available = [
        (invoice, amount)
        for invoice in query
        if (amount := invoice_open_amount(invoice.id)) > 0
    ]
    return [
        Candidate(
            kind="MULTI_INVOICE_EXACT",
            transaction_ids=(money.id,),
            invoice_ids=tuple(invoice.id for invoice, _ in window),
            total=total,
            difference=Decimal("0.00"),
            start_at=datetime.combine(window[0][0].issue_date, time.min, tzinfo=UTC),
            end_at=datetime.combine(window[-1][0].issue_date, time.max, tzinfo=UTC),
        )
        for window, total in _contiguous_windows(available, target)
    ]


def _contiguous_windows(items, target):
    for start_index in range(len(items)):
        total = Decimal("0.00")
        for end_index in range(start_index, len(items)):
            total += items[end_index][1] if isinstance(items[end_index], tuple) else items[end_index].open_amount
            if total == target:
                yield items[start_index : end_index + 1], total
            if total > target:
                break


def _validate_date_window(start, end):
    if not isinstance(start, date) or not isinstance(end, date) or start > end:
        raise ValueError("日期区间不合法")
