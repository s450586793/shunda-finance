from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied

from apps.accounts.roles import Role
from apps.core.models import AuditLog
from apps.ledger.choices import MoneyDirection
from apps.reconciliation.choices import ReconciliationDirection, SettlementStatus
from apps.reconciliation.models import (
    Reconciliation,
    ReconciliationReversal,
    SettlementBatch,
    SettlementBatchInvoice,
    SettlementBatchTransaction,
)
from apps.reconciliation.services import (
    AllocationInput,
    create_reconciliation,
    reverse_reconciliation,
)
from apps.reconciliation.settlements import (
    confirm_settlement_batch,
    create_settlement_batch,
)
from tests.builders import make_transaction


@pytest.fixture
def draft_settlement(finance_user, input_invoice):
    input_invoice.issue_date = date(2026, 6, 1)
    input_invoice.save(update_fields=["issue_date"])
    money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=input_invoice.total_amount,
        counterparty=input_invoice.counterparty,
    )
    money.occurred_at = money.occurred_at.replace(
        year=2026, month=6, day=2
    )
    money.save(update_fields=["occurred_at"])
    batch = SettlementBatch.objects.create(
        counterparty=input_invoice.counterparty,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        created_by=finance_user,
    )
    SettlementBatchInvoice.objects.create(batch=batch, invoice=input_invoice)
    SettlementBatchTransaction.objects.create(batch=batch, transaction=money)
    return batch, input_invoice, money


@pytest.mark.django_db(transaction=True)
def test_create_reconciliation_rejects_invalid_actors_before_validation(owner_user):
    stale = User.objects.create_user("stale-finance")
    stale.delete()

    for actor in (owner_user, User(username="unsaved"), stale):
        with pytest.raises(PermissionDenied, match="财务"):
            create_reconciliation(
                actor=actor,
                direction="invalid",
                allocations=[],
            )

    assert not Reconciliation.objects.exists()
    assert not AuditLog.objects.filter(action="reconciliation.created").exists()


@pytest.mark.django_db(transaction=True)
def test_reverse_reconciliation_rejects_owner_before_validation_and_writes(
    finance_user, owner_user, input_invoice, outflow
):
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[
            AllocationInput(input_invoice.id, outflow.id, Decimal("100.00"))
        ],
    )

    with pytest.raises(PermissionDenied, match="财务"):
        reverse_reconciliation(
            actor=owner_user,
            reconciliation_id=reconciliation.id,
            reason=None,
        )

    assert not ReconciliationReversal.objects.exists()
    assert not AuditLog.objects.filter(action="reconciliation.reversed").exists()


@pytest.mark.django_db(transaction=True)
def test_create_settlement_batch_rejects_owner_before_validation_and_writes(
    owner_user, input_invoice
):
    with pytest.raises(PermissionDenied, match="财务"):
        create_settlement_batch(
            actor=owner_user,
            counterparty_id=input_invoice.counterparty_id,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            period_start=date(2026, 6, 30),
            period_end=date(2026, 6, 1),
        )

    assert not SettlementBatch.objects.exists()
    assert not AuditLog.objects.filter(action="settlement_batch.created").exists()


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_rejects_owner_before_lookup(owner_user):
    with pytest.raises(PermissionDenied, match="财务"):
        confirm_settlement_batch(
            uuid4(), actor=owner_user, allocations=[], version=1
        )


@pytest.mark.django_db(transaction=True)
def test_confirm_settlement_batch_rejects_owner_without_state_change(
    owner_user, draft_settlement
):
    batch, invoice, money = draft_settlement
    audit_count = AuditLog.objects.count()

    with pytest.raises(PermissionDenied, match="财务"):
        confirm_settlement_batch(
            batch.id,
            actor=owner_user,
            allocations=[
                AllocationInput(invoice.id, money.id, invoice.total_amount)
            ],
            version=batch.version,
        )

    batch.refresh_from_db()
    assert batch.status == SettlementStatus.DRAFT
    assert batch.version == 1
    assert not Reconciliation.objects.exists()
    assert AuditLog.objects.count() == audit_count


@pytest.mark.django_db(transaction=True)
def test_create_reverse_and_settlement_services_reject_existing_dual_role_actor(
    finance_user, owner_user, input_invoice, outflow
):
    owner_user.groups.add(Group.objects.get(name=Role.FINANCE.value))
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[
            AllocationInput(input_invoice.id, outflow.id, Decimal("100.00"))
        ],
    )
    audit_count = AuditLog.objects.count()

    with pytest.raises(PermissionDenied, match="财务"):
        create_reconciliation(actor=owner_user, direction="invalid", allocations=[])
    with pytest.raises(PermissionDenied, match="财务"):
        reverse_reconciliation(
            actor=owner_user,
            reconciliation_id=reconciliation.id,
            reason="双角色不得撤销",
        )
    with pytest.raises(PermissionDenied, match="财务"):
        create_settlement_batch(
            actor=owner_user,
            counterparty_id=input_invoice.counterparty_id,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            period_start=date(2026, 6, 30),
            period_end=date(2026, 6, 1),
        )

    assert Reconciliation.objects.count() == 1
    assert not ReconciliationReversal.objects.exists()
    assert not SettlementBatch.objects.exists()
    assert AuditLog.objects.count() == audit_count
