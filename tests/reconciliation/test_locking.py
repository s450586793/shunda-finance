from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.ledger.models import Invoice, MoneyTransaction
from apps.reconciliation.choices import ReconciliationDirection
from apps.reconciliation.models import Reconciliation, ReconciliationAllocation
from apps.reconciliation.services import (
    AllocationInput,
    create_reconciliation,
    reverse_reconciliation,
)


class TrackingLockedQuerySet:
    def __init__(self, queryset, label, events):
        self.queryset = queryset
        self.label = label
        self.events = events

    def filter(self, *args, **kwargs):
        return type(self)(self.queryset.filter(*args, **kwargs), self.label, self.events)

    def values_list(self, *args, **kwargs):
        return type(self)(
            self.queryset.values_list(*args, **kwargs),
            self.label,
            self.events,
        )

    def get(self, *args, **kwargs):
        self.events.append(self.label)
        return self.queryset.get(*args, **kwargs)

    def __iter__(self):
        self.events.append(self.label)
        return iter(self.queryset)


@contextmanager
def track_locked_queryset_evaluations(events):
    def lock_recorder(model, label):
        original = model.objects.select_for_update

        def select_for_update():
            return TrackingLockedQuerySet(original(), label, events)

        return select_for_update

    with (
        patch.object(
            Invoice.objects,
            "select_for_update",
            side_effect=lock_recorder(Invoice, "invoice"),
        ),
        patch.object(
            MoneyTransaction.objects,
            "select_for_update",
            side_effect=lock_recorder(MoneyTransaction, "transaction"),
        ),
        patch.object(
            ReconciliationAllocation.objects,
            "select_for_update",
            side_effect=lock_recorder(ReconciliationAllocation, "allocation"),
        ),
        patch.object(
            Reconciliation.objects,
            "select_for_update",
            side_effect=lock_recorder(Reconciliation, "reconciliation"),
        ),
    ):
        yield


@pytest.mark.django_db(transaction=True)
def test_create_reconciliation_evaluates_locks_in_canonical_order(
    finance_user, input_invoice, outflow
):
    events = []

    with track_locked_queryset_evaluations(events):
        create_reconciliation(
            actor=finance_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[AllocationInput(input_invoice.id, outflow.id, Decimal("1.00"))],
        )

    assert events == ["invoice", "transaction", "allocation", "reconciliation"]


@pytest.mark.django_db(transaction=True)
def test_reverse_reconciliation_evaluates_locks_in_canonical_order(
    finance_user, input_invoice, outflow
):
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(input_invoice.id, outflow.id, Decimal("1.00"))],
    )
    events = []

    with track_locked_queryset_evaluations(events):
        reverse_reconciliation(
            actor=finance_user,
            reconciliation_id=reconciliation.id,
            reason="录入错误",
        )

    assert events == ["invoice", "transaction", "allocation", "reconciliation"]
