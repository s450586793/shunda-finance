from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum

from apps.accounts.roles import require_finance_actor
from apps.core.audit import record_audit
from apps.ledger.choices import InvoiceDirection, InvoiceStatus, MoneyDirection
from apps.ledger.models import Invoice, MoneyTransaction

from .choices import ReconciliationDirection, ReconciliationMode
from .models import (
    Reconciliation,
    ReconciliationAllocation,
    ReconciliationReversal,
    SettlementBatch,
)

MONEY_QUANTUM = Decimal("0.01")


@dataclass(frozen=True)
class AllocationInput:
    invoice_id: UUID
    transaction_id: UUID
    amount: Decimal


@transaction.atomic
def create_reconciliation(
    *,
    actor,
    direction,
    allocations,
    note="",
    mode=ReconciliationMode.DIRECT,
    settlement_batch: SettlementBatch | None = None,
    expected_invoice_open_amounts=None,
    expected_transaction_open_amounts=None,
    allow_partial=True,
):
    require_finance_actor(actor, message="只有财务人员可以执行核销")
    allocations = tuple(allocations)
    if not allocations:
        raise ValidationError("核销明细不能为空")
    if direction not in ReconciliationDirection.values:
        raise ValidationError("核销方向不合法")
    if mode not in ReconciliationMode.values:
        raise ValidationError("核销方式不合法")
    if settlement_batch is not None and mode != ReconciliationMode.BATCH:
        raise ValidationError("结算批次只能用于批次核销")
    if settlement_batch is None and mode == ReconciliationMode.BATCH:
        raise ValidationError("批次核销必须关联结算批次")

    invoice_ids = {item.invoice_id for item in allocations}
    transaction_ids = {item.transaction_id for item in allocations}
    invoices = {
        obj.id: obj
        for obj in Invoice.objects.select_for_update().filter(id__in=invoice_ids)
    }
    transactions = {
        obj.id: obj
        for obj in MoneyTransaction.objects.select_for_update().filter(
            id__in=transaction_ids
        )
    }
    _lock_existing_reconciliations(invoice_ids, transaction_ids)
    _validate_allocations(
        direction,
        allocations,
        invoices,
        transactions,
        expected_invoice_open_amounts=expected_invoice_open_amounts,
        expected_transaction_open_amounts=expected_transaction_open_amounts,
        allow_partial=allow_partial,
    )

    reconciliation = Reconciliation.objects.create(
        direction=direction,
        mode=mode,
        created_by=actor,
        note=note,
        settlement_batch=settlement_batch,
    )
    ReconciliationAllocation.objects.bulk_create(
        [
            ReconciliationAllocation(
                reconciliation=reconciliation,
                invoice=invoices[item.invoice_id],
                transaction=transactions[item.transaction_id],
                amount=item.amount,
            )
            for item in allocations
        ]
    )
    record_audit(
        actor,
        "reconciliation.created",
        reconciliation,
        {"allocation_count": len(allocations)},
    )
    return reconciliation


@transaction.atomic
def reverse_reconciliation(*, actor, reconciliation_id, reason):
    require_finance_actor(actor, message="只有财务人员可以执行核销")
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("撤销原因不能为空")

    allocation_rows = list(
        ReconciliationAllocation.objects.filter(
            reconciliation_id=reconciliation_id
        ).values_list("invoice_id", "transaction_id")
    )
    invoice_ids = {invoice_id for invoice_id, _ in allocation_rows}
    transaction_ids = {transaction_id for _, transaction_id in allocation_rows}
    list(Invoice.objects.select_for_update().filter(id__in=invoice_ids))
    list(MoneyTransaction.objects.select_for_update().filter(id__in=transaction_ids))
    list(
        ReconciliationAllocation.objects.select_for_update().filter(
            reconciliation_id=reconciliation_id
        )
    )
    original = Reconciliation.objects.select_for_update().get(pk=reconciliation_id)

    if ReconciliationReversal.objects.filter(original=original).exists():
        raise ValidationError("该核销已经撤销")
    reversal = ReconciliationReversal.objects.create(
        original=original,
        reversed_by=actor,
        reason=reason.strip(),
    )
    record_audit(
        actor,
        "reconciliation.reversed",
        original,
        {"reason": reversal.reason},
    )
    return reversal


def _lock_existing_reconciliations(invoice_ids, transaction_ids):
    allocations = ReconciliationAllocation.objects.select_for_update().filter(
        Q(invoice_id__in=invoice_ids) | Q(transaction_id__in=transaction_ids)
    )
    reconciliation_ids = list(allocations.values_list("reconciliation_id", flat=True))
    list(Reconciliation.objects.select_for_update().filter(id__in=reconciliation_ids))


def _validate_allocations(
    direction,
    allocations,
    invoices,
    transactions,
    *,
    expected_invoice_open_amounts,
    expected_transaction_open_amounts,
    allow_partial,
):
    requested_invoice_amounts = defaultdict(lambda: Decimal("0.00"))
    requested_transaction_amounts = defaultdict(lambda: Decimal("0.00"))
    expected_invoice_direction, expected_money_direction = _directions_for(direction)

    for item in allocations:
        if not isinstance(item.amount, Decimal):
            raise ValidationError("核销金额必须使用 Decimal")
        if item.amount <= 0:
            raise ValidationError("核销金额必须大于零")
        if item.amount != item.amount.quantize(MONEY_QUANTUM):
            raise ValidationError("核销金额最多保留两位小数")
        if item.invoice_id not in invoices:
            raise ValidationError("发票不存在")
        if item.transaction_id not in transactions:
            raise ValidationError("资金不存在")

        invoice = invoices[item.invoice_id]
        money = transactions[item.transaction_id]
        if invoice.status in {InvoiceStatus.VOID, InvoiceStatus.RED}:
            raise ValidationError("作废或红冲发票不能核销")
        if (
            invoice.direction != expected_invoice_direction
            or money.direction != expected_money_direction
        ):
            raise ValidationError("核销方向与发票或资金方向不匹配")
        if invoice.counterparty_id != money.counterparty_id:
            raise ValidationError("发票和资金交易对方不一致")

        requested_invoice_amounts[item.invoice_id] += item.amount
        requested_transaction_amounts[item.transaction_id] += item.amount

    invoice_allocated = _allocated_amounts("invoice_id", requested_invoice_amounts)
    transaction_allocated = _allocated_amounts(
        "transaction_id", requested_transaction_amounts
    )
    invoice_open_amounts = {
        invoice_id: invoices[invoice_id].total_amount - invoice_allocated[invoice_id]
        for invoice_id in requested_invoice_amounts
    }
    transaction_open_amounts = {
        transaction_id: transactions[transaction_id].amount
        - transaction_allocated[transaction_id]
        for transaction_id in requested_transaction_amounts
    }
    _validate_expected_open_amounts(
        expected_invoice_open_amounts,
        invoice_open_amounts,
    )
    _validate_expected_open_amounts(
        expected_transaction_open_amounts,
        transaction_open_amounts,
    )
    if not allow_partial:
        for invoice_id, requested in requested_invoice_amounts.items():
            remaining = invoice_open_amounts[invoice_id] - requested
            if remaining > 0:
                raise ValidationError(
                    f"本次核销后仍剩余 {remaining:,.2f} 元，请明确确认部分核销。"
                )
    for transaction_id, requested in requested_transaction_amounts.items():
        if requested > transaction_open_amounts[transaction_id]:
            raise ValidationError("资金可核销金额不足")
    for invoice_id, requested in requested_invoice_amounts.items():
        if requested > invoice_open_amounts[invoice_id]:
            raise ValidationError("发票可核销金额不足")


def _validate_expected_open_amounts(expected_amounts, current_amounts):
    if expected_amounts is None:
        return
    if set(expected_amounts) != set(current_amounts) or any(
        expected_amounts[item_id] != amount
        for item_id, amount in current_amounts.items()
    ):
        raise ValidationError("页面数据已过期，请刷新后重试")


def _directions_for(direction):
    if direction == ReconciliationDirection.PURCHASE_PAYMENT:
        return InvoiceDirection.INPUT, MoneyDirection.OUTFLOW
    return InvoiceDirection.OUTPUT, MoneyDirection.INFLOW


def _allocated_amounts(field_name, requested_amounts):
    rows = (
        ReconciliationAllocation.objects.filter(
            **{f"{field_name}__in": requested_amounts},
            reconciliation__reversal__isnull=True,
        )
        .values(field_name)
        .annotate(total=Sum("amount"))
    )
    return defaultdict(
        lambda: Decimal("0.00"),
        {row[field_name]: row["total"] for row in rows},
    )
