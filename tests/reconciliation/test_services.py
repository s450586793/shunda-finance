from decimal import Decimal
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.core.models import AuditLog
from apps.imports.choices import SourceKind
from apps.imports.models import ImportBatch
from apps.ledger.choices import (
    InvoiceDirection,
    InvoiceStatus,
    MoneyChannel,
    MoneyDirection,
)
from apps.ledger.models import AccountBalanceSnapshot, FundingAccount
from apps.reconciliation.choices import ReconciliationDirection
from apps.reconciliation.models import (
    Reconciliation,
    ReconciliationAllocation,
    ReconciliationReversal,
)
from apps.reconciliation.services import (
    AllocationInput,
    create_reconciliation,
    reverse_reconciliation,
)
from tests.builders import make_invoice, make_transaction


@pytest.mark.django_db(transaction=True)
def test_one_invoice_can_use_multiple_payments(
    finance_user, input_invoice, two_outflows
):
    result = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[
            AllocationInput(input_invoice.id, two_outflows[0].id, Decimal("400.00")),
            AllocationInput(input_invoice.id, two_outflows[1].id, Decimal("600.00")),
        ],
        note="分两次支付",
    )

    assert result.allocations.count() == 2


@pytest.mark.django_db(transaction=True)
def test_payment_cannot_be_allocated_twice(finance_user, input_invoice, outflow):
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(input_invoice.id, outflow.id, outflow.amount)],
    )

    with pytest.raises(ValidationError, match="资金可核销金额不足"):
        create_reconciliation(
            actor=finance_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[AllocationInput(input_invoice.id, outflow.id, Decimal("0.01"))],
        )


@pytest.mark.django_db(transaction=True)
def test_sales_receipt_reconciles_output_invoice_with_inflow(finance_user):
    invoice = make_invoice(
        finance_user,
        direction=InvoiceDirection.OUTPUT,
        total_amount=Decimal("100.00"),
    )
    money = make_transaction(
        finance_user,
        direction=MoneyDirection.INFLOW,
        amount=Decimal("100.00"),
        counterparty=invoice.counterparty,
    )

    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.SALES_RECEIPT,
        allocations=[AllocationInput(invoice.id, money.id, Decimal("100.00"))],
    )

    assert reconciliation.allocations.count() == 1


@pytest.mark.django_db(transaction=True)
def test_create_reconciliation_rejects_empty_allocations(finance_user):
    with pytest.raises(ValidationError, match="核销明细不能为空"):
        create_reconciliation(
            actor=finance_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[],
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("amount", [Decimal("0.00"), Decimal("-0.01")])
def test_create_reconciliation_rejects_non_positive_amount(
    finance_user, input_invoice, outflow, amount
):
    with pytest.raises(ValidationError, match="核销金额必须大于零"):
        create_reconciliation(
            actor=finance_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[AllocationInput(input_invoice.id, outflow.id, amount)],
        )


@pytest.mark.django_db(transaction=True)
def test_create_reconciliation_rejects_missing_invoice(finance_user, outflow):
    with pytest.raises(ValidationError, match="发票不存在"):
        create_reconciliation(
            actor=finance_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[AllocationInput(uuid4(), outflow.id, Decimal("1.00"))],
        )


@pytest.mark.django_db(transaction=True)
def test_create_reconciliation_rejects_missing_transaction(finance_user, input_invoice):
    with pytest.raises(ValidationError, match="资金不存在"):
        create_reconciliation(
            actor=finance_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[AllocationInput(input_invoice.id, uuid4(), Decimal("1.00"))],
        )


@pytest.mark.django_db(transaction=True)
def test_create_reconciliation_never_treats_balance_snapshot_as_funds(
    finance_user, input_invoice
):
    account = FundingAccount.objects.create(
        channel=MoneyChannel.BANK,
        name="核销快照账户",
        identifier="reconciliation-snapshot",
        masked_identifier="********snapshot",
    )
    batch = ImportBatch.objects.create(
        source_kind=SourceKind.BANK,
        created_by=finance_user,
    )
    snapshot = AccountBalanceSnapshot.objects.create(
        account=account,
        as_of="2026-07-01T09:00:00+08:00",
        balance=Decimal("100000.00"),
        source_batch=batch,
    )

    with pytest.raises(ValidationError, match="资金不存在"):
        create_reconciliation(
            actor=finance_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[AllocationInput(input_invoice.id, snapshot.id, Decimal("1.00"))],
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("status", [InvoiceStatus.VOID, InvoiceStatus.RED])
def test_create_reconciliation_rejects_void_or_red_invoice(
    finance_user, input_invoice, outflow, status
):
    input_invoice.status = status
    input_invoice.save(update_fields=["status"])

    with pytest.raises(ValidationError, match="作废或红冲发票不能核销"):
        create_reconciliation(
            actor=finance_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[AllocationInput(input_invoice.id, outflow.id, Decimal("1.00"))],
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("invoice_direction", "money_direction", "direction"),
    [
        (
            InvoiceDirection.OUTPUT,
            MoneyDirection.OUTFLOW,
            ReconciliationDirection.PURCHASE_PAYMENT,
        ),
        (
            InvoiceDirection.INPUT,
            MoneyDirection.INFLOW,
            ReconciliationDirection.SALES_RECEIPT,
        ),
    ],
)
def test_create_reconciliation_rejects_wrong_direction(
    finance_user, invoice_direction, money_direction, direction
):
    invoice = make_invoice(finance_user, direction=invoice_direction)
    money = make_transaction(
        finance_user,
        direction=money_direction,
        counterparty=invoice.counterparty,
    )

    with pytest.raises(ValidationError, match="核销方向与发票或资金方向不匹配"):
        create_reconciliation(
            actor=finance_user,
            direction=direction,
            allocations=[AllocationInput(invoice.id, money.id, Decimal("1.00"))],
        )


@pytest.mark.django_db(transaction=True)
def test_create_reconciliation_rejects_counterparty_mismatch(
    finance_user, input_invoice
):
    money = make_transaction(finance_user, direction=MoneyDirection.OUTFLOW)

    with pytest.raises(ValidationError, match="发票和资金交易对方不一致"):
        create_reconciliation(
            actor=finance_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[AllocationInput(input_invoice.id, money.id, Decimal("1.00"))],
        )


@pytest.mark.django_db(transaction=True)
def test_create_reconciliation_aggregates_duplicate_invoice_amounts(
    finance_user, input_invoice
):
    money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("1500.00"),
        counterparty=input_invoice.counterparty,
    )

    with pytest.raises(ValidationError, match="发票可核销金额不足"):
        create_reconciliation(
            actor=finance_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[
                AllocationInput(input_invoice.id, money.id, Decimal("500.00")),
                AllocationInput(input_invoice.id, money.id, Decimal("501.00")),
            ],
        )


@pytest.mark.django_db(transaction=True)
def test_create_reconciliation_aggregates_duplicate_transaction_amounts(finance_user):
    invoice = make_invoice(finance_user, total_amount=Decimal("2000.00"))
    money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("1000.00"),
        counterparty=invoice.counterparty,
    )

    with pytest.raises(ValidationError, match="资金可核销金额不足"):
        create_reconciliation(
            actor=finance_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[
                AllocationInput(invoice.id, money.id, Decimal("500.00")),
                AllocationInput(invoice.id, money.id, Decimal("501.00")),
            ],
        )


@pytest.mark.django_db(transaction=True)
def test_create_reconciliation_records_audit_entry(finance_user, input_invoice, outflow):
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(input_invoice.id, outflow.id, Decimal("1.00"))],
    )

    audit = AuditLog.objects.get(target_id=str(reconciliation.id))
    assert audit.actor == finance_user
    assert audit.action == "reconciliation.created"
    assert audit.changes == {"allocation_count": 1}


@pytest.mark.django_db(transaction=True)
def test_reconciliation_records_cannot_be_physically_deleted(
    finance_user, input_invoice, outflow
):
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(input_invoice.id, outflow.id, Decimal("1.00"))],
    )
    allocation = reconciliation.allocations.get()

    for record in (reconciliation, allocation):
        with pytest.raises(RuntimeError, match="正式财务记录不允许物理删除"):
            record.delete()

    reversal = reverse_reconciliation(
        actor=finance_user,
        reconciliation_id=reconciliation.id,
        reason="录入错误",
    )
    with pytest.raises(RuntimeError, match="正式财务记录不允许物理删除"):
        ReconciliationReversal.objects.filter(pk=reversal.id).delete()

    assert Reconciliation.objects.filter(pk=reconciliation.id).exists()
    assert ReconciliationAllocation.objects.filter(pk=allocation.id).exists()
    assert ReconciliationReversal.objects.filter(pk=reversal.id).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("invalid_amount", [Decimal("0.00"), Decimal("-0.01")])
def test_direct_allocation_creation_requires_positive_amount(
    finance_user, input_invoice, outflow, invalid_amount
):
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(input_invoice.id, outflow.id, Decimal("1.00"))],
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        ReconciliationAllocation.objects.create(
            reconciliation=reconciliation,
            invoice=input_invoice,
            transaction=outflow,
            amount=invalid_amount,
        )

    allocation = ReconciliationAllocation.objects.create(
        reconciliation=reconciliation,
        invoice=input_invoice,
        transaction=outflow,
        amount=Decimal("1.00"),
    )
    assert allocation.amount == Decimal("1.00")


@pytest.mark.django_db(transaction=True)
def test_reverse_reconciliation_requires_reason_and_can_only_run_once(
    finance_user, input_invoice, outflow
):
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(input_invoice.id, outflow.id, Decimal("1.00"))],
    )

    with pytest.raises(ValidationError, match="撤销原因不能为空"):
        reverse_reconciliation(
            actor=finance_user,
            reconciliation_id=reconciliation.id,
            reason="  ",
        )

    with pytest.raises(ValidationError, match="撤销原因不能为空"):
        reverse_reconciliation(
            actor=finance_user,
            reconciliation_id=reconciliation.id,
            reason=None,
        )

    reversal = reverse_reconciliation(
        actor=finance_user,
        reconciliation_id=reconciliation.id,
        reason="  录入错误  ",
    )
    assert reversal.reason == "录入错误"

    with pytest.raises(ValidationError, match="该核销已经撤销"):
        reverse_reconciliation(
            actor=finance_user,
            reconciliation_id=reconciliation.id,
            reason="再次撤销",
        )

    audit = AuditLog.objects.filter(action="reconciliation.reversed").get()
    assert audit.changes == {"reason": "录入错误"}
