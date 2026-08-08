from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Q, Sum
from django.db.models.functions import Coalesce

from apps.accounts.roles import require_finance_actor
from apps.core.audit import record_audit
from apps.ledger.choices import InvoiceDirection, InvoiceStatus, MoneyDirection
from apps.ledger.models import Invoice, MoneyTransaction
from apps.parties.models import Counterparty

from .choices import ReconciliationDirection, ReconciliationMode, SettlementStatus
from .models import (
    ReconciliationAllocation,
    SettlementBatch,
    SettlementBatchInvoice,
    SettlementBatchTransaction,
)
from .queries import invoice_open_amount, transaction_open_amount
from .services import create_reconciliation

MONEY_FIELD = models.DecimalField(max_digits=18, decimal_places=2)


@transaction.atomic
def create_settlement_batch(
    *, actor, counterparty_id, direction, period_start, period_end
):
    require_finance_actor(actor, message="只有财务人员可以执行核销")
    if period_start > period_end:
        raise ValidationError("开始日期不能晚于结束日期")
    invoice_direction, money_direction = _directions_for(direction)
    try:
        counterparty = Counterparty.objects.get(pk=counterparty_id)
    except Counterparty.DoesNotExist as exc:
        raise ValidationError("往来单位不存在") from exc

    active_invoice_allocations = ReconciliationAllocation.objects.filter(
        invoice_id=models.OuterRef("pk"),
        reconciliation__reversal__isnull=True,
    )
    locked_invoices = list(
        Invoice.objects.filter(
            counterparty=counterparty,
            direction=invoice_direction,
            status=InvoiceStatus.NORMAL,
            issue_date__range=(period_start, period_end),
        )
        .filter(~models.Exists(active_invoice_allocations))
        .order_by("issue_date", "id")
        .select_for_update()
    )
    open_invoice_ids = set(
        _with_open_amount(
            Invoice.objects.filter(id__in=[item.id for item in locked_invoices]),
            "total_amount",
        )
        .filter(open_amount__gt=0)
        .values_list("id", flat=True)
    )
    invoices = [item for item in locked_invoices if item.id in open_invoice_ids]
    if not invoices:
        raise ValidationError("结算期间内没有可核销的发票")
    active_transaction_allocations = ReconciliationAllocation.objects.filter(
        transaction_id=models.OuterRef("pk"),
        reconciliation__reversal__isnull=True,
    )
    locked_transactions = list(
        MoneyTransaction.objects.filter(
            counterparty=counterparty,
            direction=money_direction,
            occurred_at__date__range=(period_start, period_end),
        )
        .filter(~models.Exists(active_transaction_allocations))
        .order_by("occurred_at", "id")
        .select_for_update()
    )
    open_transaction_ids = set(
        _with_open_amount(
            MoneyTransaction.objects.filter(
                id__in=[item.id for item in locked_transactions]
            ),
            "amount",
        )
        .filter(open_amount__gt=0)
        .values_list("id", flat=True)
    )
    transactions = [
        item for item in locked_transactions if item.id in open_transaction_ids
    ]
    if not transactions:
        raise ValidationError("结算期间内没有可核销的资金")

    batch = SettlementBatch.objects.create(
        counterparty=counterparty,
        direction=direction,
        period_start=period_start,
        period_end=period_end,
        created_by=actor,
    )
    SettlementBatchInvoice.objects.bulk_create(
        [SettlementBatchInvoice(batch=batch, invoice=invoice) for invoice in invoices]
    )
    SettlementBatchTransaction.objects.bulk_create(
        [
            SettlementBatchTransaction(batch=batch, transaction=money)
            for money in transactions
        ]
    )
    record_audit(
        actor,
        "settlement_batch.created",
        batch,
        {
            "invoice_count": len(invoices),
            "transaction_count": len(transactions),
        },
    )
    return batch


def _with_open_amount(query, total_field):
    return query.annotate(
        allocated_amount=Coalesce(
            Sum(
                "reconciliationallocation__amount",
                filter=Q(
                    reconciliationallocation__reconciliation__reversal__isnull=True
                ),
            ),
            Decimal("0.00"),
            output_field=MONEY_FIELD,
        )
    ).annotate(
        open_amount=models.ExpressionWrapper(
            F(total_field) - F("allocated_amount"), output_field=MONEY_FIELD
        )
    )


@transaction.atomic
def confirm_settlement_batch(batch_id, actor, allocations, *, version):
    require_finance_actor(actor, message="只有财务人员可以执行核销")
    batch = SettlementBatch.objects.select_for_update().get(pk=batch_id)
    allocations = tuple(allocations)
    _validate_batch_version(batch, version)
    _validate_batch_allocations(batch, allocations)

    reconciliation = create_reconciliation(
        actor=actor,
        direction=batch.direction,
        allocations=allocations,
        mode=ReconciliationMode.BATCH,
        settlement_batch=batch,
    )
    batch.status = SettlementStatus.CONFIRMED
    batch.version += 1
    batch.save(update_fields=["status", "version"])
    return reconciliation


def _validate_batch_version(batch, version):
    if batch.status == SettlementStatus.CONFIRMED:
        raise ValidationError("结算批次不能重复确认")
    if batch.status != SettlementStatus.DRAFT:
        raise ValidationError("结算批次状态不允许确认")
    if version != batch.version:
        raise ValidationError("结算批次版本已过期")


def _validate_batch_allocations(batch, allocations):
    pairs = [(item.invoice_id, item.transaction_id) for item in allocations]
    if len(pairs) != len(set(pairs)):
        raise ValidationError("结算批次核销明细不能重复")
    invoice_ids = {item.invoice_id for item in allocations}
    transaction_ids = {item.transaction_id for item in allocations}
    batch_invoice_ids = set(
        SettlementBatchInvoice.objects.filter(batch=batch).values_list(
            "invoice_id", flat=True
        )
    )
    batch_transaction_ids = set(
        SettlementBatchTransaction.objects.filter(batch=batch).values_list(
            "transaction_id", flat=True
        )
    )
    if not batch_invoice_ids:
        raise ValidationError("结算批次缺少发票明细")
    if not batch_transaction_ids:
        raise ValidationError("结算批次缺少资金明细")
    if not allocations:
        raise ValidationError("结算批次核销明细不能为空")
    if not invoice_ids <= batch_invoice_ids:
        raise ValidationError("发票不属于结算批次")
    if not transaction_ids <= batch_transaction_ids:
        raise ValidationError("资金不属于结算批次")
    if invoice_ids != batch_invoice_ids or transaction_ids != batch_transaction_ids:
        raise ValidationError("结算批次明细不完整")

    invoices = Invoice.objects.select_for_update().in_bulk(batch_invoice_ids)
    transactions = MoneyTransaction.objects.select_for_update().in_bulk(
        batch_transaction_ids
    )
    _validate_batch_records(batch, invoices.values(), transactions.values())
    externally_occupied = (
        ReconciliationAllocation.objects.filter(
            Q(invoice_id__in=batch_invoice_ids)
            | Q(transaction_id__in=batch_transaction_ids),
            reconciliation__reversal__isnull=True,
        )
        .exclude(reconciliation__settlement_batch=batch)
        .exists()
    )
    if externally_occupied:
        raise ValidationError("结算批次明细已被其他核销占用")
    invoice_open_amounts = {
        invoice_id: invoice_open_amount(invoice_id) for invoice_id in batch_invoice_ids
    }
    transaction_open_amounts = {
        transaction_id: transaction_open_amount(transaction_id)
        for transaction_id in batch_transaction_ids
    }
    if any(amount <= 0 for amount in invoice_open_amounts.values()):
        raise ValidationError("结算批次包含无可核销金额的发票")
    if any(amount <= 0 for amount in transaction_open_amounts.values()):
        raise ValidationError("结算批次包含无可核销金额的资金")
    _validate_allocation_totals(
        allocations,
        invoice_open_amounts,
        transaction_open_amounts,
    )


def _validate_batch_records(batch, invoices, transactions):
    expected_invoice_direction, expected_money_direction = _directions_for(batch.direction)
    for invoice in invoices:
        if invoice.counterparty_id != batch.counterparty_id:
            raise ValidationError("结算批次交易对方不匹配")
        if invoice.direction != expected_invoice_direction:
            raise ValidationError("结算批次方向不匹配")
        if not batch.period_start <= invoice.issue_date <= batch.period_end:
            raise ValidationError("发票不在结算期间内")
    for money in transactions:
        if money.counterparty_id != batch.counterparty_id:
            raise ValidationError("结算批次交易对方不匹配")
        if money.direction != expected_money_direction:
            raise ValidationError("结算批次方向不匹配")
        if not batch.period_start <= money.occurred_at.date() <= batch.period_end:
            raise ValidationError("资金不在结算期间内")


def _validate_allocation_totals(
    allocations,
    invoice_open_amounts,
    transaction_open_amounts,
):
    invoice_totals = defaultdict(lambda: Decimal("0.00"))
    transaction_totals = defaultdict(lambda: Decimal("0.00"))
    for item in allocations:
        invoice_totals[item.invoice_id] += item.amount
        transaction_totals[item.transaction_id] += item.amount

    for invoice_id, open_amount in invoice_open_amounts.items():
        if invoice_totals[invoice_id] != open_amount:
            raise ValidationError("结算批次核销金额不一致")
    for transaction_id, open_amount in transaction_open_amounts.items():
        if transaction_totals[transaction_id] != open_amount:
            raise ValidationError("结算批次核销金额不一致")


def _directions_for(direction):
    if direction == "purchase_payment":
        return InvoiceDirection.INPUT, MoneyDirection.OUTFLOW
    if direction == ReconciliationDirection.SALES_RECEIPT:
        return InvoiceDirection.OUTPUT, MoneyDirection.INFLOW
    raise ValidationError("结算批次方向不合法")
