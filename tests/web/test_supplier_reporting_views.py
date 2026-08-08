from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.imports.choices import SourceKind
from apps.imports.models import (
    CoverageStatus,
    DataCoveragePeriod,
    SourceFile,
)
from apps.parties.models import Counterparty
from apps.reconciliation.choices import ReconciliationDirection
from apps.reconciliation.services import AllocationInput, create_reconciliation
from tests.builders import make_invoice, make_transaction


def _register_full_coverage():
    for source_kind in (
        SourceKind.INPUT_INVOICE,
        SourceKind.BANK,
        SourceKind.WECHAT,
    ):
        DataCoveragePeriod.objects.create(
            year=2026,
            source_kind=source_kind,
            status=CoverageStatus.FULL,
            expected_start=date(2026, 1, 1),
            expected_end=date(2026, 12, 31),
            actual_start=date(2026, 1, 1),
            actual_end=date(2026, 12, 31),
        )


@pytest.mark.django_db
def test_supplier_pages_require_reporting_role(client, finance_user, owner_user):
    supplier = Counterparty.objects.create(
        name="页面供应商",
        normalized_name="页面供应商",
        is_supplier=True,
    )
    summary_url = reverse("reporting:suppliers")
    detail_url = reverse("reporting:supplier-detail", args=[supplier.id])

    assert client.get(summary_url).status_code == 302
    assert client.get(detail_url).status_code == 302

    client.force_login(finance_user)
    assert client.get(summary_url).status_code == 200
    assert client.get(detail_url).status_code == 200

    client.force_login(owner_user)
    assert client.get(summary_url).status_code == 200
    assert client.get(detail_url).status_code == 200

    client.force_login(User.objects.create_user("supplier-auditor"))
    assert client.get(summary_url).status_code == 403
    assert client.get(detail_url).status_code == 403


@pytest.mark.django_db
def test_supplier_pages_reject_invalid_dates_and_non_suppliers(owner_client):
    customer = Counterparty.objects.create(
        name="仅客户",
        normalized_name="仅客户",
        is_customer=True,
    )
    summary_url = reverse("reporting:suppliers")
    detail_url = reverse("reporting:supplier-detail", args=[customer.id])

    assert owner_client.get(summary_url, {"as_of": "bad"}).status_code == 400
    assert owner_client.get(detail_url, {"as_of": "bad"}).status_code == 400
    assert owner_client.get(
        detail_url,
        {"as_of": "2026-07-31"},
    ).status_code == 404


@pytest.mark.django_db
def test_supplier_summary_rejects_date_without_safe_cutoff(owner_client):
    response = owner_client.get(
        reverse("reporting:suppliers"),
        {"as_of": "9999-12-31"},
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_supplier_detail_rejects_date_without_safe_cutoff(owner_client):
    supplier = Counterparty.objects.create(
        name="极限日期供应商",
        normalized_name="极限日期供应商",
        is_supplier=True,
    )

    response = owner_client.get(
        reverse("reporting:supplier-detail", args=[supplier.id]),
        {"as_of": "9999-12-31"},
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_supplier_summary_search_and_detail_amounts(finance_client, finance_user):
    supplier = Counterparty.objects.create(
        name="无锡页面供应商",
        normalized_name="无锡页面供应商",
        is_supplier=True,
    )
    other_supplier = Counterparty.objects.create(
        name="常州其他供应商",
        normalized_name="常州其他供应商",
        is_supplier=True,
    )
    invoice = make_invoice(
        finance_user,
        counterparty=supplier,
        total_amount=Decimal("1000.00"),
        invoice_number="SUPPLIER-INV-1",
    )
    payment = make_transaction(
        finance_user,
        counterparty=supplier,
        amount=Decimal("400.00"),
    )
    payment.transaction_id = "SUPPLIER-PAY-1"
    payment.save(update_fields=["transaction_id"])
    make_invoice(
        finance_user,
        counterparty=other_supplier,
        invoice_number="OTHER-INV-1",
    )

    response = finance_client.get(
        reverse("reporting:suppliers"),
        {"q": supplier.name, "as_of": "2026-07-31"},
    )
    detail_url = reverse(
        "reporting:supplier-detail",
        args=[invoice.counterparty_id],
    )
    detail = finance_client.get(detail_url, {"as_of": "2026-07-31"})

    assert response.status_code == 200
    assert response.context["search"] == supplier.name
    assert [
        row.counterparty_id for row in response.context["rows"]
    ] == [supplier.id]
    assert response.context["totals"].supplier_count == 1
    assert response.context["totals"].invoiced_amount == Decimal("1000.00")
    assert response.context["totals"].paid_amount == Decimal("400.00")
    assert response.context["totals"].balance == Decimal("600.00")

    summary_body = response.content.decode()
    assert supplier.name in summary_body
    assert other_supplier.name not in summary_body
    assert f"{detail_url}?as_of=2026-07-31" in summary_body
    assert "1000.00" in summary_body
    assert "400.00" in summary_body
    assert "600.00" in summary_body

    assert detail.status_code == 200
    assert detail.context["summary"].balance == Decimal("600.00")
    detail_body = detail.content.decode()
    assert invoice.invoice_number in detail_body
    assert payment.transaction_id in detail_body
    assert "1000.00" in detail_body
    assert "400.00" in detail_body
    assert "600.00" in detail_body
    assert (
        f'{reverse("reporting:suppliers")}?as_of=2026-07-31'
        in detail_body
    )


@pytest.mark.django_db
def test_supplier_detail_displays_negative_running_balance(
    finance_client,
    finance_user,
):
    invoice = make_invoice(
        finance_user,
        total_amount=Decimal("1000.00"),
        invoice_number="NEGATIVE-BALANCE-INVOICE",
    )
    make_transaction(
        finance_user,
        counterparty=invoice.counterparty,
        amount=Decimal("1200.00"),
    )

    response = finance_client.get(
        reverse(
            "reporting:supplier-detail",
            args=[invoice.counterparty_id],
        ),
        {"as_of": "2026-07-31"},
    )

    assert response.status_code == 200
    assert response.context["summary"].balance == Decimal("-200.00")
    assert "-200.00" in response.content.decode()


@pytest.mark.django_db
def test_only_finance_sees_reconciliation_and_source_actions(
    finance_client,
    finance_user,
    owner_user,
    settings,
    tmp_path,
):
    settings.MEDIA_ROOT = tmp_path
    invoice = make_invoice(finance_user, invoice_number="ACTION-INVOICE")
    SourceFile.objects.create(
        batch=invoice.import_batch,
        file=SimpleUploadedFile("source.xlsx", b"source"),
        original_name="source.xlsx",
        sha256="a" * 64,
        size=6,
    )
    url = reverse(
        "reporting:supplier-detail",
        args=[invoice.counterparty_id],
    )
    source_url = reverse("imports:source", args=[invoice.import_batch_id])
    reconciliation_url = (
        f"{reverse('reconciliation:workbench')}?invoice={invoice.id}"
    )

    finance_response = finance_client.get(url, {"as_of": "2026-07-31"})
    finance_body = finance_response.content.decode()
    workbench_response = finance_client.get(reconciliation_url)
    finance_client.force_login(owner_user)
    owner_response = finance_client.get(url, {"as_of": "2026-07-31"})
    owner_body = owner_response.content.decode()
    owner_workbench_response = finance_client.get(reconciliation_url)

    assert finance_response.status_code == 200
    assert reconciliation_url in finance_body
    assert workbench_response.status_code == 200
    assert invoice.invoice_number in workbench_response.content.decode()
    assert "去核销" in finance_body
    assert source_url in finance_body
    assert "下载原文件" in finance_body

    assert owner_response.status_code == 200
    assert invoice.invoice_number in owner_body
    assert reconciliation_url not in owner_body
    assert owner_workbench_response.status_code == 403
    assert "去核销" not in owner_body
    assert source_url not in owner_body
    assert "下载原文件" not in owner_body


@pytest.mark.django_db
def test_historical_supplier_detail_hides_reconcile_action_when_currently_settled(
    finance_client,
    finance_user,
):
    invoice = make_invoice(
        finance_user,
        total_amount=Decimal("1000.00"),
        invoice_number="HISTORICAL-SETTLED-INVOICE",
    )
    payment = make_transaction(
        finance_user,
        counterparty=invoice.counterparty,
        amount=Decimal("1000.00"),
    )
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(invoice.id, payment.id, Decimal("1000.00"))],
    )
    type(reconciliation).objects.filter(pk=reconciliation.pk).update(
        created_at=datetime(2026, 8, 1, 8, tzinfo=UTC)
    )
    reconciliation_url = (
        f"{reverse('reconciliation:workbench')}?invoice={invoice.id}"
    )

    response = finance_client.get(
        reverse("reporting:supplier-detail", args=[invoice.counterparty_id]),
        {"as_of": "2026-07-31"},
    )
    invoice_row = next(
        row for row in response.context["rows"] if row.reference_id == invoice.id
    )
    body = response.content.decode()

    assert response.status_code == 200
    assert invoice_row.open_amount == Decimal("1000.00")
    assert not invoice_row.can_reconcile
    assert reconciliation_url not in body
    assert "去核销" not in body


@pytest.mark.django_db
def test_supplier_pages_render_empty_states_and_coverage_warning(owner_client):
    supplier = Counterparty.objects.create(
        name="暂无业务供应商",
        normalized_name="暂无业务供应商",
        is_supplier=True,
    )

    summary = owner_client.get(
        reverse("reporting:suppliers"),
        {"as_of": "2026-07-31"},
    )
    detail = owner_client.get(
        reverse("reporting:supplier-detail", args=[supplier.id]),
        {"as_of": "2026-07-31"},
    )

    assert summary.status_code == 200
    assert summary.context["coverage"].code == "unregistered"
    assert "暂无供应商往来" in summary.content.decode()
    assert "完整性未登记" in summary.content.decode()

    assert detail.status_code == 200
    assert detail.context["coverage"].code == "unregistered"
    assert "暂无往来记录" in detail.content.decode()
    assert "完整性未登记" in detail.content.decode()

    _register_full_coverage()
    full = owner_client.get(
        reverse("reporting:suppliers"),
        {"as_of": "2026-07-31"},
    )
    full_body = full.content.decode()

    assert full.context["coverage"].code == "full"
    assert "完整性未登记" not in full_body
    assert 'class="message message-warning"' not in full_body
