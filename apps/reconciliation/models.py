# ruff: noqa: RUF012

from django.conf import settings
from django.db import models

from apps.core.models import ImmutableLedgerModel, UUIDModel
from apps.ledger.models import Invoice, MoneyTransaction
from apps.parties.models import Counterparty

from .choices import ReconciliationDirection, ReconciliationMode, SettlementStatus


class SettlementBatch(UUIDModel):
    counterparty = models.ForeignKey(Counterparty, on_delete=models.PROTECT)
    direction = models.CharField(max_length=20, choices=ReconciliationDirection.choices)
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(
        max_length=12,
        choices=SettlementStatus.choices,
        default=SettlementStatus.DRAFT,
    )
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)


class SettlementBatchInvoice(UUIDModel):
    batch = models.ForeignKey(
        SettlementBatch,
        on_delete=models.PROTECT,
        related_name="invoice_items",
    )
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "invoice"],
                name="uniq_settlement_batch_invoice",
            ),
        ]


class SettlementBatchTransaction(UUIDModel):
    batch = models.ForeignKey(
        SettlementBatch,
        on_delete=models.PROTECT,
        related_name="transaction_items",
    )
    transaction = models.ForeignKey(MoneyTransaction, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "transaction"],
                name="uniq_settlement_batch_transaction",
            ),
        ]


class Reconciliation(ImmutableLedgerModel):
    direction = models.CharField(max_length=20, choices=ReconciliationDirection.choices)
    mode = models.CharField(
        max_length=10,
        choices=ReconciliationMode.choices,
        default=ReconciliationMode.DIRECT,
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    settlement_batch = models.ForeignKey(
        SettlementBatch,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reconciliations",
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ReconciliationAllocation(ImmutableLedgerModel):
    reconciliation = models.ForeignKey(
        Reconciliation,
        on_delete=models.PROTECT,
        related_name="allocations",
    )
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT)
    transaction = models.ForeignKey(MoneyTransaction, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta(ImmutableLedgerModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="reconciliation_allocation_amount_positive",
            ),
        ]


class ReconciliationReversal(ImmutableLedgerModel):
    original = models.OneToOneField(
        Reconciliation,
        on_delete=models.PROTECT,
        related_name="reversal",
    )
    reversed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
