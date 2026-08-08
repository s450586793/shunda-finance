from decimal import Decimal

import pytest

from apps.reconciliation.choices import ReconciliationDirection
from apps.reconciliation.queries import invoice_open_amount, transaction_open_amount
from apps.reconciliation.services import (
    AllocationInput,
    create_reconciliation,
    reverse_reconciliation,
)


@pytest.mark.django_db(transaction=True)
def test_open_amounts_cover_unused_partial_full_and_reversed(
    finance_user, input_invoice, outflow
):
    assert invoice_open_amount(input_invoice.id) == Decimal("1000.00")
    assert transaction_open_amount(outflow.id) == Decimal("1000.00")

    first = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(input_invoice.id, outflow.id, Decimal("400.00"))],
    )
    assert invoice_open_amount(input_invoice.id) == Decimal("600.00")
    assert transaction_open_amount(outflow.id) == Decimal("600.00")

    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(input_invoice.id, outflow.id, Decimal("600.00"))],
    )
    assert invoice_open_amount(input_invoice.id) == Decimal("0.00")
    assert transaction_open_amount(outflow.id) == Decimal("0.00")

    reverse_reconciliation(
        actor=finance_user,
        reconciliation_id=first.id,
        reason="更正首笔金额",
    )
    assert invoice_open_amount(input_invoice.id) == Decimal("400.00")
    assert transaction_open_amount(outflow.id) == Decimal("400.00")
