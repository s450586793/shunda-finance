from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from apps.ledger.choices import InvoiceDirection, MoneyDirection
from apps.parties.models import Counterparty
from tests.builders import make_invoice, make_transaction

JUNE_PAYMENTS = [
    (date(2026, 6, 1), "800.00"),
    (date(2026, 6, 2), "4400.00"),
    (date(2026, 6, 3), "2700.00"),
    (date(2026, 6, 4), "2900.00"),
    (date(2026, 6, 9), "850.00"),
    (date(2026, 6, 14), "3750.00"),
    (date(2026, 6, 16), "2000.00"),
    (date(2026, 6, 17), "8000.00"),
    (date(2026, 6, 18), "8100.00"),
    (date(2026, 6, 19), "2750.00"),
    (date(2026, 6, 23), "4150.00"),
    (date(2026, 6, 24), "5800.00"),
    (date(2026, 6, 27), "850.00"),
]


@pytest.fixture
def synthetic_railway_party():
    return Counterparty.objects.create(
        name="测试铁路物流有限公司",
        normalized_name="测试铁路物流有限公司",
        is_supplier=True,
    )


@pytest.fixture
def synthetic_railway_invoice(finance_user, synthetic_railway_party):
    return make_invoice(
        finance_user,
        direction=InvoiceDirection.INPUT,
        total_amount=Decimal("46050.00"),
        counterparty=synthetic_railway_party,
    )


@pytest.fixture
def synthetic_railway_june_transactions(finance_user, synthetic_railway_party):
    transactions = []
    for paid_on, amount in JUNE_PAYMENTS:
        item = make_transaction(
            finance_user,
            direction=MoneyDirection.OUTFLOW,
            amount=Decimal(amount),
            counterparty=synthetic_railway_party,
        )
        item.occurred_at = datetime.combine(paid_on, time(9, 0), tzinfo=UTC)
        item.save(update_fields=["occurred_at"])
        transactions.append(item)
    return transactions
