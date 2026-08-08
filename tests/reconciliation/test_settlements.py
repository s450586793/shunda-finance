from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.ledger.choices import InvoiceDirection, MoneyDirection
from apps.parties.models import Counterparty
from apps.reconciliation.choices import (
    ReconciliationDirection,
    ReconciliationMode,
    SettlementStatus,
)
from apps.reconciliation.models import (
    SettlementBatch,
    SettlementBatchInvoice,
    SettlementBatchTransaction,
)
from apps.reconciliation.services import AllocationInput, create_reconciliation
from apps.reconciliation.settlements import confirm_settlement_batch
from tests.builders import make_invoice, make_transaction


@pytest.fixture
def settlement_batch(finance_user):
    party = Counterparty.objects.create(
        name="结算单位",
        normalized_name="结算单位",
        is_supplier=True,
    )
    invoice = make_invoice(
        finance_user,
        direction=InvoiceDirection.INPUT,
        total_amount=Decimal("100.00"),
        counterparty=party,
    )
    invoice.issue_date = date(2026, 6, 1)
    invoice.save(update_fields=["issue_date"])
    money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("100.00"),
        counterparty=party,
    )
    money.occurred_at = money.occurred_at.replace(month=6)
    money.save(update_fields=["occurred_at"])
    batch = SettlementBatch.objects.create(
        counterparty=party,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        created_by=finance_user,
    )
    SettlementBatchInvoice.objects.create(batch=batch, invoice=invoice)
    SettlementBatchTransaction.objects.create(batch=batch, transaction=money)
    return batch, invoice, money


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_creates_batch_reconciliation(
    finance_user, settlement_batch
):
    batch, invoice, money = settlement_batch

    reconciliation = confirm_settlement_batch(
        batch.id,
        actor=finance_user,
        version=batch.version,
        allocations=[AllocationInput(invoice.id, money.id, Decimal("100.00"))],
    )

    batch.refresh_from_db()
    assert reconciliation.mode == ReconciliationMode.BATCH
    assert reconciliation.settlement_batch_id == batch.id
    assert reconciliation.allocations.get().amount == Decimal("100.00")
    assert batch.status == SettlementStatus.CONFIRMED
    assert batch.version == 2


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_rejects_stale_version(finance_user, settlement_batch):
    batch, invoice, money = settlement_batch

    with pytest.raises(ValidationError, match="结算批次版本已过期"):
        confirm_settlement_batch(
            batch.id,
            actor=finance_user,
            version=batch.version + 1,
            allocations=[AllocationInput(invoice.id, money.id, Decimal("100.00"))],
        )


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_rejects_second_confirmation(
    finance_user, settlement_batch
):
    batch, invoice, money = settlement_batch
    allocations = [AllocationInput(invoice.id, money.id, Decimal("100.00"))]
    confirm_settlement_batch(batch.id, actor=finance_user, version=1, allocations=allocations)

    with pytest.raises(ValidationError, match="结算批次不能重复确认"):
        confirm_settlement_batch(batch.id, actor=finance_user, version=2, allocations=allocations)


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_rejects_item_outside_batch(
    finance_user, settlement_batch
):
    batch, _invoice, money = settlement_batch
    outside = make_invoice(finance_user, total_amount=Decimal("100.00"))

    with pytest.raises(ValidationError, match="发票不属于结算批次"):
        confirm_settlement_batch(
            batch.id,
            actor=finance_user,
            version=batch.version,
            allocations=[AllocationInput(outside.id, money.id, Decimal("100.00"))],
        )


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_rejects_mismatched_allocation_totals(
    finance_user, settlement_batch
):
    batch, invoice, money = settlement_batch

    with pytest.raises(ValidationError, match="结算批次核销金额不一致"):
        confirm_settlement_batch(
            batch.id,
            actor=finance_user,
            version=batch.version,
            allocations=[AllocationInput(invoice.id, money.id, Decimal("99.99"))],
        )


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_rejects_wrong_direction(finance_user, settlement_batch):
    batch, invoice, money = settlement_batch
    invoice.direction = InvoiceDirection.OUTPUT
    invoice.save(update_fields=["direction"])

    with pytest.raises(ValidationError, match="结算批次方向不匹配"):
        confirm_settlement_batch(
            batch.id,
            actor=finance_user,
            version=batch.version,
            allocations=[AllocationInput(invoice.id, money.id, Decimal("100.00"))],
        )


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_rejects_item_outside_period(
    finance_user, settlement_batch
):
    batch, invoice, money = settlement_batch
    money.occurred_at = money.occurred_at.replace(month=7)
    money.save(update_fields=["occurred_at"])

    with pytest.raises(ValidationError, match="资金不在结算期间内"):
        confirm_settlement_batch(
            batch.id,
            finance_user,
            [AllocationInput(invoice.id, money.id, Decimal("100.00"))],
            version=batch.version,
        )


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_rejects_unknown_counterparty(finance_user, settlement_batch):
    batch, invoice, money = settlement_batch
    other_party = Counterparty.objects.create(
        name="未知单位",
        normalized_name="未知单位",
        is_supplier=True,
    )
    money.counterparty = other_party
    money.save(update_fields=["counterparty"])

    with pytest.raises(ValidationError, match="结算批次交易对方不匹配"):
        confirm_settlement_batch(
            batch.id,
            actor=finance_user,
            version=batch.version,
            allocations=[AllocationInput(invoice.id, money.id, Decimal("100.00"))],
        )


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_rejects_invalid_direction(finance_user, settlement_batch):
    batch, invoice, money = settlement_batch
    batch.direction = "invalid"
    batch.save(update_fields=["direction"])

    with pytest.raises(ValidationError, match="结算批次方向不合法"):
        confirm_settlement_batch(
            batch.id,
            finance_user,
            [AllocationInput(invoice.id, money.id, Decimal("100.00"))],
            version=batch.version,
        )


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_requires_every_invoice_item(
    finance_user, settlement_batch
):
    batch, invoice, money = settlement_batch
    omitted_invoice = make_invoice(
        finance_user,
        total_amount=Decimal("50.00"),
        counterparty=batch.counterparty,
    )
    omitted_invoice.issue_date = date(2026, 6, 2)
    omitted_invoice.save(update_fields=["issue_date"])
    SettlementBatchInvoice.objects.create(batch=batch, invoice=omitted_invoice)

    with pytest.raises(ValidationError, match="结算批次明细不完整"):
        confirm_settlement_batch(
            batch.id,
            finance_user,
            [AllocationInput(invoice.id, money.id, Decimal("100.00"))],
            version=batch.version,
        )


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_requires_every_transaction_item(
    finance_user, settlement_batch
):
    batch, invoice, money = settlement_batch
    omitted_money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("50.00"),
        counterparty=batch.counterparty,
    )
    omitted_money.occurred_at = omitted_money.occurred_at.replace(month=6)
    omitted_money.save(update_fields=["occurred_at"])
    SettlementBatchTransaction.objects.create(batch=batch, transaction=omitted_money)

    with pytest.raises(ValidationError, match="结算批次明细不完整"):
        confirm_settlement_batch(
            batch.id,
            finance_user,
            [AllocationInput(invoice.id, money.id, Decimal("100.00"))],
            version=batch.version,
        )


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_rejects_empty_allocations(
    finance_user, settlement_batch
):
    batch, _invoice, _money = settlement_batch

    with pytest.raises(ValidationError, match="结算批次核销明细不能为空"):
        confirm_settlement_batch(batch.id, finance_user, [], version=batch.version)


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_rejects_empty_invoice_item_set(finance_user):
    party = Counterparty.objects.create(
        name="空发票结算单位",
        normalized_name="空发票结算单位",
        is_supplier=True,
    )
    batch = SettlementBatch.objects.create(
        counterparty=party,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        created_by=finance_user,
    )

    with pytest.raises(ValidationError, match="结算批次缺少发票明细"):
        confirm_settlement_batch(batch.id, finance_user, [], version=batch.version)


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_rejects_empty_transaction_item_set(
    finance_user, settlement_batch
):
    batch, _invoice, _money = settlement_batch
    SettlementBatchTransaction.objects.filter(batch=batch).delete()

    with pytest.raises(ValidationError, match="结算批次缺少资金明细"):
        confirm_settlement_batch(batch.id, finance_user, [], version=batch.version)


@pytest.mark.django_db(transaction=True)
def test_settlement_batch_invoice_is_unique_per_batch_and_reusable_in_another_batch(
    finance_user, settlement_batch
):
    batch, invoice, _money = settlement_batch

    with transaction.atomic(), pytest.raises(IntegrityError):
        SettlementBatchInvoice.objects.create(batch=batch, invoice=invoice)

    other_batch = SettlementBatch.objects.create(
        counterparty=batch.counterparty,
        direction=batch.direction,
        period_start=batch.period_start,
        period_end=batch.period_end,
        created_by=finance_user,
    )
    item = SettlementBatchInvoice.objects.create(batch=other_batch, invoice=invoice)

    assert item.batch_id == other_batch.id


@pytest.mark.django_db(transaction=True)
def test_settlement_batch_transaction_is_unique_per_batch_and_reusable_in_another_batch(
    finance_user, settlement_batch
):
    batch, _invoice, money = settlement_batch

    with transaction.atomic(), pytest.raises(IntegrityError):
        SettlementBatchTransaction.objects.create(batch=batch, transaction=money)

    other_batch = SettlementBatch.objects.create(
        counterparty=batch.counterparty,
        direction=batch.direction,
        period_start=batch.period_start,
        period_end=batch.period_end,
        created_by=finance_user,
    )
    item = SettlementBatchTransaction.objects.create(batch=other_batch, transaction=money)

    assert item.batch_id == other_batch.id


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_rejects_fully_occupied_item_without_state_change(
    finance_user, settlement_batch
):
    batch, invoice, money = settlement_batch
    prior_money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("100.00"),
        counterparty=batch.counterparty,
    )
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(invoice.id, prior_money.id, Decimal("100.00"))],
    )

    with pytest.raises(ValidationError, match="结算批次明细已被其他核销占用"):
        confirm_settlement_batch(
            batch.id,
            finance_user,
            [AllocationInput(invoice.id, money.id, Decimal("100.00"))],
            version=batch.version,
        )

    batch.refresh_from_db()
    assert batch.status == SettlementStatus.DRAFT
    assert batch.version == 1
    assert batch.reconciliations.count() == 0
