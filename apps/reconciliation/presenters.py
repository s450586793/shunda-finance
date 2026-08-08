from dataclasses import dataclass
from decimal import Decimal

from .models import ReconciliationAllocation
from .queries import invoice_open_amount, transaction_open_amount
from .services import AllocationInput


def money_text(amount):
    return f"{amount:,.2f}"


def money_input(amount):
    return f"{amount:.2f}"


def money_cents(amount):
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError("金额必须精确到分")
    return int(cents)


def candidate_to_dict(candidate):
    return {
        "kind": candidate.kind,
        "transaction_ids": [str(item) for item in candidate.transaction_ids],
        "total": money_text(candidate.total),
        "total_cents": money_cents(candidate.total),
        "difference": money_text(candidate.difference),
        "difference_cents": money_cents(candidate.difference),
        "start": candidate.start_at.date().isoformat(),
        "end": candidate.end_at.date().isoformat(),
    }


@dataclass(frozen=True)
class SettlementItem:
    record: object
    open_amount: Decimal
    open_text: str
    occupied: bool


@dataclass(frozen=True)
class AllocationRow:
    invoice_id: object
    transaction_id: object
    amount: Decimal
    amount_input: str


def settlement_context(batch):
    external_allocations = ReconciliationAllocation.objects.filter(
        reconciliation__reversal__isnull=True,
    ).exclude(reconciliation__settlement_batch=batch)
    occupied_invoice_ids = set(
        external_allocations.filter(
            invoice_id__in=batch.invoice_items.values("invoice_id")
        ).values_list("invoice_id", flat=True)
    )
    occupied_transaction_ids = set(
        external_allocations.filter(
            transaction_id__in=batch.transaction_items.values("transaction_id")
        ).values_list("transaction_id", flat=True)
    )
    invoice_items = [
        SettlementItem(
            item.invoice,
            (amount := invoice_open_amount(item.invoice_id)),
            money_text(amount),
            item.invoice_id in occupied_invoice_ids,
        )
        for item in batch.invoice_items.select_related("invoice__counterparty").order_by(
            "invoice__issue_date", "invoice_id"
        )
    ]
    transaction_items = [
        SettlementItem(
            item.transaction,
            (amount := transaction_open_amount(item.transaction_id)),
            money_text(amount),
            item.transaction_id in occupied_transaction_ids,
        )
        for item in batch.transaction_items.select_related(
            "transaction__account", "transaction__counterparty"
        ).order_by("transaction__occurred_at", "transaction_id")
    ]
    allocations = _allocation_plan(invoice_items, transaction_items)
    invoice_total = sum(
        (item.open_amount for item in invoice_items if not item.occupied), Decimal("0.00")
    )
    money_total = sum(
        (item.open_amount for item in transaction_items if not item.occupied), Decimal("0.00")
    )
    allocated = sum((item.amount for item in allocations), Decimal("0.00"))
    occupied = any(item.occupied for item in (*invoice_items, *transaction_items))
    return {
        "invoice_items": invoice_items,
        "transaction_items": transaction_items,
        "allocation_rows": allocations,
        "invoice_total": money_text(invoice_total),
        "money_total": money_text(money_total),
        "allocated_total": money_text(allocated),
        "invoice_unallocated": money_text(invoice_total - allocated),
        "money_unallocated": money_text(money_total - allocated),
        "can_confirm": bool(allocations)
        and invoice_total == money_total
        and not occupied,
    }


def _allocation_plan(invoice_items, transaction_items):
    allocations = []
    invoice_index = 0
    transaction_index = 0
    invoice_remaining = [
        Decimal("0.00") if item.occupied else max(item.open_amount, Decimal("0.00"))
        for item in invoice_items
    ]
    transaction_remaining = [
        Decimal("0.00") if item.occupied else max(item.open_amount, Decimal("0.00"))
        for item in transaction_items
    ]
    while invoice_index < len(invoice_items) and transaction_index < len(transaction_items):
        if invoice_remaining[invoice_index] == 0:
            invoice_index += 1
            continue
        if transaction_remaining[transaction_index] == 0:
            transaction_index += 1
            continue
        amount = min(
            invoice_remaining[invoice_index], transaction_remaining[transaction_index]
        )
        allocations.append(
            AllocationRow(
                invoice_items[invoice_index].record.id,
                transaction_items[transaction_index].record.id,
                amount,
                money_input(amount),
            )
        )
        invoice_remaining[invoice_index] -= amount
        transaction_remaining[transaction_index] -= amount
    return tuple(allocations)


def direct_allocation_rows(transactions, invoice_open):
    remaining = invoice_open
    rows = []
    for item in transactions:
        amount = min(remaining, item.open_amount)
        rows.append(
            {
                "record": item.record,
                "open_amount": item.open_amount,
                "open_text": money_text(item.open_amount),
                "open_input": money_input(item.open_amount),
                "open_cents": money_cents(item.open_amount),
                "allocation_input": money_input(amount),
                "selected": amount > 0,
            }
        )
        remaining -= amount
    return rows


def as_allocation_inputs(rows):
    return tuple(
        AllocationInput(row.invoice_id, row.transaction_id, row.amount) for row in rows
    )
