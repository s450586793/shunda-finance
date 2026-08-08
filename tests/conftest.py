from decimal import Decimal

import pytest
from django.contrib.auth.models import User

from apps.accounts.roles import Role, assign_role
from apps.ledger.choices import InvoiceDirection, MoneyDirection
from tests.builders import make_invoice, make_transaction


@pytest.fixture
def finance_user(db):
    user = User.objects.create_user("finance", password="secret")
    assign_role(user, Role.FINANCE)
    return user


@pytest.fixture
def owner_user(db):
    user = User.objects.create_user("owner", password="secret")
    assign_role(user, Role.OWNER)
    return user


@pytest.fixture
def finance_client(client, finance_user):
    client.force_login(finance_user)
    return client


@pytest.fixture
def owner_client(client, owner_user):
    client.force_login(owner_user)
    return client


@pytest.fixture
def input_invoice(finance_user):
    return make_invoice(
        finance_user,
        direction=InvoiceDirection.INPUT,
        total_amount=Decimal("1000.00"),
    )


@pytest.fixture
def two_outflows(finance_user, input_invoice):
    return [
        make_transaction(
            finance_user,
            direction=MoneyDirection.OUTFLOW,
            amount=Decimal("400.00"),
            counterparty=input_invoice.counterparty,
        ),
        make_transaction(
            finance_user,
            direction=MoneyDirection.OUTFLOW,
            amount=Decimal("600.00"),
            counterparty=input_invoice.counterparty,
        ),
    ]


@pytest.fixture
def outflow(finance_user, input_invoice):
    return make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("1000.00"),
        counterparty=input_invoice.counterparty,
    )
