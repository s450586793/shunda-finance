from decimal import Decimal

from django.db import models
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce

from apps.ledger.models import Invoice, MoneyTransaction

MONEY_FIELD = models.DecimalField(max_digits=18, decimal_places=2)


def invoice_open_amount(invoice_id):
    invoice = Invoice.objects.annotate(
        allocated=Coalesce(
            Sum(
                "reconciliationallocation__amount",
                filter=Q(
                    reconciliationallocation__reconciliation__reversal__isnull=True
                ),
            ),
            Decimal("0.00"),
            output_field=MONEY_FIELD,
        )
    ).get(pk=invoice_id)
    return invoice.total_amount - invoice.allocated


def transaction_open_amount(transaction_id):
    money = MoneyTransaction.objects.annotate(
        allocated=Coalesce(
            Sum(
                "reconciliationallocation__amount",
                filter=Q(
                    reconciliationallocation__reconciliation__reversal__isnull=True
                ),
            ),
            Decimal("0.00"),
            output_field=MONEY_FIELD,
        )
    ).get(pk=transaction_id)
    return money.amount - money.allocated
