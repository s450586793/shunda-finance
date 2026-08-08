from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db.models.query import QuerySet

from apps.ledger.choices import MoneyDirection
from apps.parties.models import Counterparty
from apps.reconciliation.choices import (
    ReconciliationDirection,
    SettlementStatus,
)
from apps.reconciliation.models import (
    Reconciliation,
    SettlementBatch,
    SettlementBatchInvoice,
    SettlementBatchTransaction,
)
from apps.reconciliation.presenters import settlement_context
from apps.reconciliation.services import (
    AllocationInput,
    create_reconciliation,
    reverse_reconciliation,
)
from apps.reconciliation.settlements import (
    confirm_settlement_batch,
    create_settlement_batch,
)
from tests.builders import make_invoice, make_transaction


def _settlement_record_pair(finance_user, party, amount=Decimal("100.00")):
    invoice = make_invoice(
        finance_user,
        total_amount=amount,
        counterparty=party,
    )
    invoice.issue_date = date(2026, 6, 10)
    invoice.save(update_fields=["issue_date"])
    money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=amount,
        counterparty=party,
    )
    money.occurred_at = datetime(2026, 6, 15, 9, tzinfo=UTC)
    money.save(update_fields=["occurred_at"])
    return invoice, money


def _batch(finance_user, party, invoice, money):
    batch = SettlementBatch.objects.create(
        counterparty=party,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        created_by=finance_user,
    )
    SettlementBatchInvoice.objects.create(batch=batch, invoice=invoice)
    SettlementBatchTransaction.objects.create(batch=batch, transaction=money)
    return batch


@pytest.mark.django_db(transaction=True)
def test_create_settlement_batch_never_aggregates_a_locked_queryset(finance_user):
    party = Counterparty.objects.create(
        name="锁查询结算单位",
        normalized_name="锁查询结算单位",
        is_supplier=True,
    )
    _settlement_record_pair(finance_user, party)
    locked_queries = []
    original_fetch_all = QuerySet._fetch_all

    def inspect_fetch(queryset):
        if queryset.query.select_for_update and all(
            queryset.query is not query for query in locked_queries
        ):
            locked_queries.append(queryset.query)
        return original_fetch_all(queryset)

    with patch.object(QuerySet, "_fetch_all", inspect_fetch):
        create_settlement_batch(
            actor=finance_user,
            counterparty_id=party.id,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
        )

    assert len(locked_queries) == 2
    assert all(not query.annotations for query in locked_queries)
    assert all(query.group_by is None for query in locked_queries)


@pytest.mark.django_db(transaction=True)
def test_settlement_draft_excludes_every_partially_occupied_record(finance_user):
    party = Counterparty.objects.create(
        name="部分占用结算单位",
        normalized_name="部分占用结算单位",
        is_supplier=True,
    )
    occupied_invoice, occupied_money = _settlement_record_pair(finance_user, party)
    fresh_invoice, fresh_money = _settlement_record_pair(finance_user, party)
    support_invoice = make_invoice(
        finance_user,
        total_amount=Decimal("10.00"),
        counterparty=party,
    )
    support_money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("10.00"),
        counterparty=party,
    )
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[
            AllocationInput(occupied_invoice.id, support_money.id, Decimal("10.00"))
        ],
    )
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[
            AllocationInput(support_invoice.id, occupied_money.id, Decimal("10.00"))
        ],
    )

    batch = create_settlement_batch(
        actor=finance_user,
        counterparty_id=party.id,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
    )

    assert list(batch.invoice_items.values_list("invoice_id", flat=True)) == [
        fresh_invoice.id
    ]
    assert list(batch.transaction_items.values_list("transaction_id", flat=True)) == [
        fresh_money.id
    ]


@pytest.mark.django_db(transaction=True)
def test_settlement_draft_can_reuse_records_after_allocation_reversal(finance_user):
    party = Counterparty.objects.create(
        name="撤销后结算单位",
        normalized_name="撤销后结算单位",
        is_supplier=True,
    )
    invoice, money = _settlement_record_pair(finance_user, party)
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(invoice.id, money.id, Decimal("10.00"))],
    )
    reverse_reconciliation(
        actor=finance_user,
        reconciliation_id=reconciliation.id,
        reason="测试撤销",
    )

    batch = create_settlement_batch(
        actor=finance_user,
        counterparty_id=party.id,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
    )

    assert batch.invoice_items.get().invoice_id == invoice.id
    assert batch.transaction_items.get().transaction_id == money.id


@pytest.mark.django_db(transaction=True)
def test_settlement_presenter_marks_partial_external_allocation_as_occupied(
    finance_user,
):
    party = Counterparty.objects.create(
        name="展示占用结算单位",
        normalized_name="展示占用结算单位",
        is_supplier=True,
    )
    invoice, money = _settlement_record_pair(finance_user, party)
    batch = _batch(finance_user, party, invoice, money)
    support_money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("10.00"),
        counterparty=party,
    )
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(invoice.id, support_money.id, Decimal("10.00"))],
    )

    context = settlement_context(batch)

    assert context["invoice_items"][0].occupied is True
    assert context["transaction_items"][0].occupied is False
    assert context["allocation_rows"] == ()
    assert context["can_confirm"] is False


@pytest.mark.django_db(transaction=True)
def test_settlement_presenter_does_not_mark_own_confirmed_allocation_as_occupied(
    finance_user,
):
    party = Counterparty.objects.create(
        name="自身核销结算单位",
        normalized_name="自身核销结算单位",
        is_supplier=True,
    )
    invoice, money = _settlement_record_pair(finance_user, party)
    batch = _batch(finance_user, party, invoice, money)
    confirm_settlement_batch(
        batch.id,
        finance_user,
        [AllocationInput(invoice.id, money.id, Decimal("100.00"))],
        version=batch.version,
    )

    context = settlement_context(batch)

    assert context["invoice_items"][0].occupied is False
    assert context["transaction_items"][0].occupied is False
    assert context["can_confirm"] is False


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_rejects_external_partial_occupancy_atomically(
    finance_user,
):
    party = Counterparty.objects.create(
        name="确认占用结算单位",
        normalized_name="确认占用结算单位",
        is_supplier=True,
    )
    invoice, money = _settlement_record_pair(finance_user, party)
    batch = _batch(finance_user, party, invoice, money)
    support_money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("10.00"),
        counterparty=party,
    )
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(invoice.id, support_money.id, Decimal("10.00"))],
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
    assert not Reconciliation.objects.filter(settlement_batch=batch).exists()


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_service_rejects_duplicate_pair(finance_user):
    party = Counterparty.objects.create(
        name="重复明细结算单位",
        normalized_name="重复明细结算单位",
        is_supplier=True,
    )
    invoice, money = _settlement_record_pair(finance_user, party)
    batch = _batch(finance_user, party, invoice, money)

    with pytest.raises(ValidationError, match="结算批次核销明细不能重复"):
        confirm_settlement_batch(
            batch.id,
            finance_user,
            [
                AllocationInput(invoice.id, money.id, Decimal("50.00")),
                AllocationInput(invoice.id, money.id, Decimal("50.00")),
            ],
            version=batch.version,
        )

    assert not Reconciliation.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_create_reconciliation_enforces_partial_confirmation_inside_lock(
    finance_user,
):
    invoice = make_invoice(finance_user, total_amount=Decimal("100.00"))
    money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("70.00"),
        counterparty=invoice.counterparty,
    )

    with pytest.raises(ValidationError, match="请明确确认部分核销"):
        create_reconciliation(
            actor=finance_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[AllocationInput(invoice.id, money.id, Decimal("70.00"))],
            allow_partial=False,
        )

    assert not Reconciliation.objects.exists()
