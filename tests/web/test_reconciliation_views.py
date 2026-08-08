from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from apps.imports.choices import SourceKind
from apps.imports.models import ImportBatch
from apps.ledger.choices import MoneyChannel, MoneyDirection
from apps.ledger.models import AccountBalanceSnapshot, FundingAccount
from apps.reconciliation.choices import ReconciliationDirection
from apps.reconciliation.models import Reconciliation
from apps.reconciliation.services import (
    AllocationInput,
    create_reconciliation,
    reverse_reconciliation,
)
from tests.builders import make_invoice, make_transaction

JUNE_PAYMENTS_WITHOUT_2000 = (
    (date(2026, 6, 1), "800.00"),
    (date(2026, 6, 2), "4400.00"),
    (date(2026, 6, 3), "2700.00"),
    (date(2026, 6, 4), "2900.00"),
    (date(2026, 6, 9), "850.00"),
    (date(2026, 6, 14), "3750.00"),
    (date(2026, 6, 17), "8000.00"),
    (date(2026, 6, 18), "8100.00"),
    (date(2026, 6, 19), "2750.00"),
    (date(2026, 6, 23), "4150.00"),
    (date(2026, 6, 24), "5800.00"),
    (date(2026, 6, 27), "850.00"),
)


@pytest.fixture
def railroad_invoice(finance_user):
    invoice = make_invoice(finance_user, total_amount=Decimal("46050.00"))
    invoice.issue_date = date(2026, 7, 1)
    invoice.save(update_fields=["issue_date"])
    return invoice


@pytest.fixture
def railroad_payments(finance_user, railroad_invoice):
    payments = []
    for paid_on, amount in JUNE_PAYMENTS_WITHOUT_2000:
        item = make_transaction(
            finance_user,
            direction=MoneyDirection.OUTFLOW,
            amount=Decimal(amount),
            counterparty=railroad_invoice.counterparty,
        )
        item.occurred_at = datetime.combine(paid_on, time(9), tzinfo=UTC)
        item.save(update_fields=["occurred_at"])
        payments.append(item)
    return payments


@pytest.mark.django_db
def test_reconciliation_endpoints_require_finance(client, owner_user):
    selected_workbench_url = (
        f"{reverse('reconciliation:workbench')}"
        "?invoice=00000000-0000-0000-0000-000000000000"
    )
    urls = (
        selected_workbench_url,
        "/reconciliation/candidates/?invoice=00000000-0000-0000-0000-000000000000&start=2026-06-01&end=2026-06-30",
        "/reconciliation/settlements/",
    )
    for url in urls:
        client.logout()
        anonymous = client.get(url)
        assert anonymous.status_code == 302
        assert anonymous.headers["Location"].startswith("/accounts/login/?next=")
        client.force_login(owner_user)
        assert client.get(url).status_code == 403

    user = User.objects.create_user("ordinary")
    client.force_login(user)
    assert client.get("/reconciliation/workbench/").status_code == 403


@pytest.mark.django_db
def test_workbench_shows_selected_totals_and_difference(
    finance_client, railroad_invoice, railroad_payments
):
    response = finance_client.get(
        "/reconciliation/workbench/",
        {
            "invoice": str(railroad_invoice.id),
            "start": "2026-06-01",
            "end": "2026-06-30",
        },
    )

    body = response.content.decode()
    assert response.status_code == 200
    assert "46,050.00" in body
    assert "45,050.00" in body
    assert "1,000.00" in body
    assert "2,000.00" not in body
    assert 'name="expected_invoice_open" value="46050.00"' in body
    assert body.count('name="expected_transaction_open"') == len(railroad_payments)
    assert body.index("待核发票") < body.index("实际资金")


@pytest.mark.django_db
def test_candidate_json_is_strict_minimal_and_never_exposes_balance_snapshot(
    finance_client, finance_user, railroad_invoice, railroad_payments
):
    account = FundingAccount.objects.create(
        channel=MoneyChannel.BANK,
        name="候选快照账户",
        identifier="sensitive-full-account",
        masked_identifier="********0001",
    )
    source_batch = ImportBatch.objects.create(
        source_kind=SourceKind.BANK,
        created_by=finance_user,
    )
    snapshot = AccountBalanceSnapshot.objects.create(
        account=account,
        as_of=datetime(2026, 6, 30, tzinfo=UTC),
        balance=Decimal("999999.00"),
        source_batch=source_batch,
    )

    response = finance_client.get(
        "/reconciliation/candidates/",
        {
            "invoice": str(railroad_invoice.id),
            "start": "2026-06-01",
            "end": "2026-06-30",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"][0]["kind"] == "PARTIAL"
    assert payload["items"][0]["total"] == "45,050.00"
    assert payload["items"][0]["total_cents"] == 4_505_000
    assert payload["items"][0]["difference"] == "1,000.00"
    transaction_ids = payload["items"][0]["transaction_ids"]
    assert transaction_ids == [str(item.id) for item in railroad_payments]
    serialized = response.content.decode()
    assert str(snapshot.id) not in serialized
    assert "sensitive-full-account" not in serialized
    assert "source_payload" not in serialized
    assert "balance" not in serialized.lower()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"invoice": "not-a-uuid", "start": "2026-06-01", "end": "2026-06-30"},
        {
            "invoice": "00000000-0000-0000-0000-000000000000",
            "start": "bad",
            "end": "2026-06-30",
        },
        {
            "invoice": "00000000-0000-0000-0000-000000000000",
            "start": "2026-07-01",
            "end": "2026-06-30",
        },
    ],
)
def test_candidate_json_rejects_invalid_parameters(finance_client, params):
    response = finance_client.get("/reconciliation/candidates/", params)

    assert response.status_code == 400
    assert response.json() == {"error": "候选查询参数不合法。"}


@pytest.mark.django_db
def test_partial_direct_reconciliation_requires_explicit_confirmation(
    finance_client, finance_user
):
    invoice = make_invoice(finance_user, total_amount=Decimal("1000.00"))
    money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("700.00"),
        counterparty=invoice.counterparty,
    )
    payload = {
        "invoice_id": str(invoice.id),
        "expected_invoice_open": "1000.00",
        "transaction_id": str(money.id),
        "expected_transaction_open": "700.00",
        "amount": "700.00",
    }

    rejected = finance_client.post("/reconciliation/confirm/", payload)

    assert rejected.status_code == 400
    assert "本次核销后仍剩余 300.00 元，请明确确认部分核销。" in rejected.content.decode()
    assert not Reconciliation.objects.exists()

    accepted = finance_client.post(
        "/reconciliation/confirm/", {**payload, "partial_confirm": "on"}
    )
    assert accepted.status_code == 302
    reconciliation = Reconciliation.objects.get()
    assert reconciliation.allocations.get().amount == Decimal("700.00")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("transaction_ids", "amounts", "message"),
    [
        (("same", "same"), ("1.00", "1.00"), "资金流水不能重复"),
        (("same",), ("0.00",), "核销金额必须大于零"),
        (("same",), ("-0.01",), "核销金额必须大于零"),
        (("same",), ("0.001",), "核销金额最多保留两位小数"),
    ],
)
def test_direct_form_rejects_duplicate_or_invalid_allocations(
    finance_client,
    finance_user,
    transaction_ids,
    amounts,
    message,
):
    invoice = make_invoice(finance_user)
    money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        counterparty=invoice.counterparty,
    )
    ids = [str(money.id) if item == "same" else item for item in transaction_ids]

    response = finance_client.post(
        "/reconciliation/confirm/",
        {
            "invoice_id": str(invoice.id),
            "expected_invoice_open": "1000.00",
            "transaction_id": ids,
            "expected_transaction_open": ["1000.00"] * len(ids),
            "amount": amounts,
        },
    )

    assert response.status_code == 400
    assert message in response.content.decode()
    assert not Reconciliation.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("amount", ["NaN", "Infinity", "1e1000000", "10000000000000000.00"])
def test_direct_form_rejects_non_finite_or_out_of_range_amount(
    finance_client, finance_user, amount
):
    invoice = make_invoice(finance_user)
    money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        counterparty=invoice.counterparty,
    )

    response = finance_client.post(
        "/reconciliation/confirm/",
        {
            "invoice_id": str(invoice.id),
            "expected_invoice_open": "1000.00",
            "transaction_id": str(money.id),
            "expected_transaction_open": "1000.00",
            "amount": amount,
        },
    )

    assert response.status_code == 400
    assert "核销金额" in response.content.decode()
    assert not Reconciliation.objects.exists()


@pytest.mark.django_db
def test_direct_form_uses_safe_chinese_error_for_invalid_invoice_uuid(
    finance_client, finance_user
):
    money = make_transaction(finance_user)

    response = finance_client.post(
        "/reconciliation/confirm/",
        {
            "invoice_id": "not-a-uuid",
            "expected_invoice_open": "1000.00",
            "transaction_id": str(money.id),
            "expected_transaction_open": "1000.00",
            "amount": "1.00",
        },
    )

    body = response.content.decode()
    assert response.status_code == 400
    assert "发票编号不合法" in body
    assert "valid UUID" not in body


@pytest.mark.django_db
def test_direct_confirmation_ignores_client_direction_and_rejects_stale_amount(
    finance_client, finance_user
):
    invoice = make_invoice(finance_user, total_amount=Decimal("100.00"))
    money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("100.00"),
        counterparty=invoice.counterparty,
    )
    payload = {
        "invoice_id": str(invoice.id),
        "expected_invoice_open": "100.00",
        "transaction_id": str(money.id),
        "expected_transaction_open": "100.00",
        "amount": "100.00",
        "direction": ReconciliationDirection.SALES_RECEIPT,
    }
    first = finance_client.post("/reconciliation/confirm/", payload)
    assert first.status_code == 302
    assert Reconciliation.objects.get().direction == ReconciliationDirection.PURCHASE_PAYMENT

    stale = finance_client.post("/reconciliation/confirm/", payload)
    assert stale.status_code == 409
    assert "页面数据已过期" in stale.content.decode()
    assert Reconciliation.objects.count() == 1


@pytest.mark.django_db
def test_direct_confirmation_rejects_invoice_open_amount_changed_by_reversal(
    finance_client, finance_user
):
    invoice = make_invoice(finance_user, total_amount=Decimal("100.00"))
    prior_money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("40.00"),
        counterparty=invoice.counterparty,
    )
    selected_money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("60.00"),
        counterparty=invoice.counterparty,
    )
    prior = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(invoice.id, prior_money.id, Decimal("40.00"))],
    )
    page = finance_client.get(
        "/reconciliation/workbench/", {"invoice": str(invoice.id)}
    )
    assert 'name="expected_invoice_open" value="60.00"' in page.content.decode()
    reverse_reconciliation(
        actor=finance_user,
        reconciliation_id=prior.id,
        reason="加载页面后撤销",
    )

    response = finance_client.post(
        "/reconciliation/confirm/",
        {
            "invoice_id": str(invoice.id),
            "expected_invoice_open": "60.00",
            "transaction_id": str(selected_money.id),
            "expected_transaction_open": "60.00",
            "amount": "60.00",
        },
    )

    body = response.content.decode()
    assert response.status_code == 409
    assert "页面数据已过期" in body
    assert 'data-stale="true"' in body
    assert not Reconciliation.objects.filter(reversal__isnull=True).exists()


@pytest.mark.django_db
def test_direct_confirmation_rejects_changed_transaction_open_amount(
    finance_client, finance_user
):
    invoice = make_invoice(finance_user, total_amount=Decimal("100.00"))
    money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("100.00"),
        counterparty=invoice.counterparty,
    )
    page = finance_client.get(
        "/reconciliation/workbench/", {"invoice": str(invoice.id)}
    )
    assert 'name="expected_transaction_open" value="100.00"' in page.content.decode()
    other_invoice = make_invoice(
        finance_user,
        total_amount=Decimal("40.00"),
        counterparty=invoice.counterparty,
    )
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(other_invoice.id, money.id, Decimal("40.00"))],
    )

    response = finance_client.post(
        "/reconciliation/confirm/",
        {
            "invoice_id": str(invoice.id),
            "expected_invoice_open": "100.00",
            "transaction_id": str(money.id),
            "expected_transaction_open": "100.00",
            "amount": "100.00",
        },
    )

    assert response.status_code == 409
    assert "页面数据已过期" in response.content.decode()
    assert Reconciliation.objects.filter(reversal__isnull=True).count() == 1


@pytest.mark.django_db
def test_confirm_and_reverse_are_csrf_protected_posts(finance_user, input_invoice, outflow):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(finance_user)
    page = csrf_client.get(
        "/reconciliation/workbench/", {"invoice": str(input_invoice.id)}
    )
    token = page.cookies["csrftoken"].value
    payload = {
        "invoice_id": str(input_invoice.id),
        "expected_invoice_open": "1000.00",
        "transaction_id": str(outflow.id),
        "expected_transaction_open": "1000.00",
        "amount": "1000.00",
    }

    assert csrf_client.get("/reconciliation/confirm/").status_code == 405
    assert csrf_client.post("/reconciliation/confirm/", payload).status_code == 403
    confirmed = csrf_client.post(
        "/reconciliation/confirm/", {**payload, "csrfmiddlewaretoken": token}
    )
    reconciliation = Reconciliation.objects.get()
    reverse_url = f"/reconciliation/{reconciliation.id}/reverse/"
    assert confirmed.status_code == 302
    assert csrf_client.post(reverse_url, {"reason": "录入错误"}).status_code == 403


@pytest.mark.django_db
def test_reversal_shows_allocations_requires_reason_and_keeps_original_accessible(
    finance_client, finance_user, input_invoice, outflow
):
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(input_invoice.id, outflow.id, Decimal("1000.00"))],
    )
    detail_url = f"/reconciliation/{reconciliation.id}/"
    reverse_url = f"{detail_url}reverse/"

    page = finance_client.get(reverse_url)
    assert page.status_code == 200
    assert input_invoice.invoice_number in page.content.decode()
    assert "1,000.00" in page.content.decode()

    invalid = finance_client.post(reverse_url, {"reason": "  "})
    assert invalid.status_code == 400
    assert "撤销原因不能为空" in invalid.content.decode()

    reversed_response = finance_client.post(reverse_url, {"reason": "  录入错误  "})
    assert reversed_response.status_code == 302
    detail = finance_client.get(detail_url)
    body = detail.content.decode()
    assert detail.status_code == 200
    assert "已撤销" in body
    assert "录入错误" in body

    repeated = finance_client.post(reverse_url, {"reason": "再次撤销"})
    assert repeated.status_code == 409
    assert "该核销已经撤销" in repeated.content.decode()


@pytest.mark.django_db
def test_finance_navigation_links_reconciliation_but_owner_navigation_is_disabled(
    client, finance_user, owner_user
):
    client.force_login(finance_user)
    finance_body = client.get("/reconciliation/workbench/").content.decode()
    assert 'href="/reconciliation/workbench/"' in finance_body
    assert 'href="/reconciliation/settlements/"' in finance_body

    client.force_login(owner_user)
    owner_body = client.get("/ledger/invoices/").content.decode()
    assert 'href="/reconciliation/workbench/"' not in owner_body
    assert 'href="/reconciliation/settlements/"' not in owner_body
    assert "人工核销" in owner_body
    assert "结算批次" in owner_body
