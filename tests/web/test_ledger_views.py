from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from django.contrib.auth.models import User

from apps.ledger.choices import (
    InvoiceDirection,
    InvoiceStatus,
    MoneyChannel,
    MoneyDirection,
)
from apps.parties.models import Counterparty
from apps.reconciliation.choices import ReconciliationDirection
from apps.reconciliation.services import (
    AllocationInput,
    create_reconciliation,
    reverse_reconciliation,
)
from tests.builders import make_invoice, make_transaction


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url",
    ["/ledger/invoices/", "/ledger/transactions/", "/parties/"],
)
def test_ledger_pages_require_authentication(client, url):
    response = client.get(url)

    assert response.status_code == 302
    assert response.headers["Location"] == f"/accounts/login/?next={url}"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url",
    ["/ledger/invoices/", "/ledger/transactions/", "/parties/"],
)
def test_owner_and_finance_can_read_ledgers(owner_client, finance_client, url):
    assert owner_client.get(url).status_code == 200
    assert finance_client.get(url).status_code == 200


@pytest.mark.django_db
def test_authenticated_user_without_business_role_is_forbidden(client):
    user = User.objects.create_user("auditor")
    client.force_login(user)

    assert client.get("/ledger/invoices/").status_code == 403


@pytest.mark.django_db
def test_invoice_filters_and_valid_allocation_state(finance_client, finance_user):
    supplier = Counterparty.objects.create(
        name="铁路供应商",
        normalized_name="铁路供应商",
        is_supplier=True,
    )
    partial = make_invoice(
        finance_user,
        direction=InvoiceDirection.INPUT,
        total_amount=Decimal("1000.00"),
        counterparty=supplier,
        invoice_number="FILTER-PARTIAL",
    )
    payment = make_transaction(
        finance_user,
        amount=Decimal("500.00"),
        counterparty=supplier,
    )
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(partial.id, payment.id, Decimal("400.00"))],
    )
    make_invoice(
        finance_user,
        direction=InvoiceDirection.OUTPUT,
        counterparty=supplier,
        invoice_number="FILTER-OTHER",
    )

    response = finance_client.get(
        "/ledger/invoices/",
        {
            "direction": InvoiceDirection.INPUT,
            "counterparty": str(supplier.id),
            "issue_start": "2026-07-01",
            "issue_end": "2026-07-01",
            "status": InvoiceStatus.NORMAL,
            "reconciliation_state": "partial",
            "invoice_number": "partial",
        },
    )

    assert list(response.context["page"].object_list) == [partial]
    assert response.context["page"].object_list[0].open_amount == Decimal("600.00")
    assert "600.00" in response.content.decode()


@pytest.mark.django_db(transaction=True)
def test_reversed_allocation_does_not_reduce_invoice_open_amount(
    finance_client, finance_user
):
    invoice = make_invoice(finance_user, invoice_number="REVERSED-OPEN")
    money = make_transaction(
        finance_user,
        amount=invoice.total_amount,
        counterparty=invoice.counterparty,
    )
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(invoice.id, money.id, invoice.total_amount)],
    )
    reverse_reconciliation(
        actor=finance_user,
        reconciliation_id=reconciliation.id,
        reason="测试撤销",
    )

    response = finance_client.get(
        "/ledger/invoices/",
        {"invoice_number": "REVERSED-OPEN", "reconciliation_state": "unreconciled"},
    )

    assert list(response.context["page"].object_list) == [invoice]
    assert response.context["page"].object_list[0].open_amount == Decimal("1000.00")


@pytest.mark.django_db
def test_transaction_filters_open_amount_and_masks_account(
    finance_client, finance_user
):
    party = Counterparty.objects.create(
        name="收款单位",
        normalized_name="收款单位",
        is_customer=True,
    )
    money = make_transaction(
        finance_user,
        direction=MoneyDirection.INFLOW,
        amount=Decimal("300.00"),
        counterparty=party,
    )
    money.transaction_id = "TXN-FILTER-001"
    money.occurred_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    money.counterparty_account = "6222021234567890123"
    money.save(update_fields=["transaction_id", "occurred_at", "counterparty_account"])

    response = finance_client.get(
        "/ledger/transactions/",
        {
            "channel": MoneyChannel.BANK,
            "direction": MoneyDirection.INFLOW,
            "counterparty": str(party.id),
            "date_start": "2026-07-08",
            "date_end": "2026-07-08",
            "open_amount": "open",
            "transaction_id": "FILTER",
        },
    )

    assert list(response.context["page"].object_list) == [money]
    body = response.content.decode()
    assert "300.00" in body
    assert money.account.masked_identifier in body
    assert money.account.identifier not in body
    assert money.counterparty_account not in body


@pytest.mark.django_db
def test_inactive_historical_counterparty_remains_selectable(
    finance_client, finance_user
):
    inactive = Counterparty.objects.create(
        name="历史停用单位",
        normalized_name="历史停用单位",
        is_supplier=True,
        active=False,
    )
    invoice = make_invoice(finance_user, counterparty=inactive)
    money = make_transaction(finance_user, counterparty=inactive)

    invoice_response = finance_client.get(
        "/ledger/invoices/", {"counterparty": str(inactive.pk)}
    )
    transaction_response = finance_client.get(
        "/ledger/transactions/", {"counterparty": str(inactive.pk)}
    )

    assert list(invoice_response.context["page"].object_list) == [invoice]
    assert list(transaction_response.context["page"].object_list) == [money]
    for response in (invoice_response, transaction_response):
        body = response.content.decode()
        assert f'value="{inactive.pk}" selected' in body
        assert "历史停用单位（停用）" in body


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("url", "params"),
    [
        (
            "/ledger/invoices/",
            {
                "direction": "bad",
                "counterparty": "not-a-uuid",
                "issue_start": "2026-99-99",
                "issue_end": "no-date",
                "status": "bad",
                "reconciliation_state": "bad",
                "page": "not-a-page",
            },
        ),
        (
            "/ledger/transactions/",
            {
                "channel": "bad",
                "direction": "bad",
                "counterparty": "not-a-uuid",
                "date_start": "bad",
                "date_end": "bad",
                "open_amount": "1e1000000",
                "page": "-1",
            },
        ),
        ("/parties/", {"kind": "bad", "active": "bad", "page": "999"}),
    ],
)
def test_invalid_filter_values_degrade_gracefully(finance_client, url, params):
    response = finance_client.get(url, params)

    assert response.status_code == 200


@pytest.mark.django_db
def test_out_of_range_open_amount_is_ignored(finance_client, finance_user):
    money = make_transaction(finance_user)

    response = finance_client.get("/ledger/transactions/", {"open_amount": "1e1000000"})

    assert response.status_code == 200
    assert list(response.context["page"].object_list) == [money]


@pytest.mark.django_db
def test_invoice_pagination_keeps_filters(finance_client, finance_user):
    for index in range(51):
        make_invoice(
            finance_user,
            invoice_number=f"PAGE-{index:03d}-{uuid4().hex[:8]}",
        )

    response = finance_client.get(
        "/ledger/invoices/",
        {"direction": InvoiceDirection.INPUT, "invoice_number": "PAGE", "page": 1},
    )

    assert response.context["page"].paginator.per_page == 50
    assert len(response.context["page"].object_list) == 50
    assert response.context["querystring"] == "direction=input&invoice_number=PAGE"
    assert (
        "direction=input&amp;invoice_number=PAGE&amp;page=2"
        in response.content.decode()
    )


@pytest.mark.django_db
def test_counterparty_filters_and_owner_navigation(owner_client):
    customer = Counterparty.objects.create(
        name="目标客户",
        normalized_name="目标客户",
        tax_id="913200001",
        is_customer=True,
    )
    Counterparty.objects.create(
        name="其他供应商",
        normalized_name="其他供应商",
        is_supplier=True,
        active=False,
    )

    response = owner_client.get(
        "/parties/", {"query": "目标", "kind": "customer", "active": "true"}
    )

    assert list(response.context["page"].object_list) == [customer]
    body = response.content.decode()
    assert "目标客户" in body
    assert "其他供应商" not in body
    assert '<a href="/imports/"' not in body
    assert "导入中心" in body
