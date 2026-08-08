from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.core.audit import record_audit
from apps.imports.choices import SourceKind
from apps.imports.models import CoverageStatus, DataCoveragePeriod, SourceFile
from apps.ledger.choices import (
    InvoiceDirection,
    InvoiceStatus,
    MoneyChannel,
    MoneyDirection,
)
from apps.parties.models import Counterparty
from apps.reconciliation.choices import ReconciliationDirection
from apps.reconciliation.models import ReconciliationReversal
from apps.reconciliation.services import AllocationInput, create_reconciliation
from apps.reporting.queries import (
    SupplierLedgerKind,
    SupplierSummaryRow,
    supplier_coverage_as_of,
    supplier_ledger_as_of,
    supplier_summaries_as_of,
    supplier_summary_as_of,
    supplier_summary_totals,
)
from tests.builders import make_invoice, make_transaction


def _set_reconciliation_time(reconciliation, value):
    type(reconciliation).objects.filter(pk=reconciliation.pk).update(created_at=value)


def _change_invoice_status(invoice, status, actor, changed_at):
    previous_status = invoice.status
    invoice.status = status
    invoice.save(update_fields=["status"])
    audit = record_audit(
        actor,
        "invoice.status_changed",
        invoice,
        {"from_status": previous_status, "to_status": status},
    )
    type(audit).objects.filter(pk=audit.pk).update(created_at=changed_at)


def _coverage(year, source_kind, status=CoverageStatus.FULL):
    return DataCoveragePeriod.objects.create(
        year=year,
        source_kind=source_kind,
        status=status,
        expected_start=date(year, 1, 1),
        expected_end=date(year, 12, 31),
        actual_start=date(year, 1, 1) if status != CoverageStatus.MISSING else None,
        actual_end=date(year, 12, 31) if status != CoverageStatus.MISSING else None,
    )


def _add_source_file(batch, name, digest):
    content = name.encode()
    return SourceFile.objects.create(
        batch=batch,
        file=SimpleUploadedFile(name, content),
        original_name=name,
        sha256=digest,
        size=len(content),
    )


@pytest.mark.django_db
def test_supplier_summaries_total_actual_invoices_and_payments(finance_user):
    supplier = Counterparty.objects.create(
        name="测试供应商",
        normalized_name="测试供应商",
        is_supplier=True,
    )
    make_invoice(
        finance_user,
        counterparty=supplier,
        total_amount=Decimal("17600.00"),
    )
    make_transaction(
        finance_user,
        counterparty=supplier,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("6600.00"),
    )

    rows = supplier_summaries_as_of(date(2026, 7, 31))

    assert len(rows) == 1
    assert rows[0].counterparty_id == supplier.id
    assert rows[0].counterparty_name == "测试供应商"
    assert rows[0].invoiced_amount == Decimal("17600.00")
    assert rows[0].paid_amount == Decimal("6600.00")
    assert rows[0].balance == Decimal("11000.00")
    assert rows[0].invoice_open_amount == Decimal("17600.00")
    assert rows[0].payment_open_amount == Decimal("6600.00")
    assert rows[0].latest_activity_on == date(2026, 7, 1)


@pytest.mark.django_db
def test_supplier_summaries_exclude_future_non_normal_and_wrong_direction(
    finance_user,
):
    supplier = Counterparty.objects.create(
        name="边界供应商",
        normalized_name="边界供应商",
        is_supplier=True,
    )
    valid = make_invoice(finance_user, counterparty=supplier)
    future = make_invoice(finance_user, counterparty=supplier)
    future.issue_date = date(2026, 8, 1)
    future.save(update_fields=["issue_date"])
    void = make_invoice(finance_user, counterparty=supplier)
    void.status = InvoiceStatus.VOID
    void.save(update_fields=["status"])
    red = make_invoice(finance_user, counterparty=supplier)
    red.status = InvoiceStatus.RED
    red.save(update_fields=["status"])
    make_invoice(
        finance_user,
        counterparty=supplier,
        direction=InvoiceDirection.OUTPUT,
    )
    make_transaction(
        finance_user,
        counterparty=supplier,
        direction=MoneyDirection.INFLOW,
    )
    future_payment = make_transaction(finance_user, counterparty=supplier)
    future_payment.occurred_at = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    future_payment.save(update_fields=["occurred_at"])

    row = supplier_summaries_as_of(date(2026, 7, 31))[0]

    assert row.invoiced_amount == valid.total_amount
    assert row.paid_amount == Decimal("0.00")


@pytest.mark.django_db
@pytest.mark.parametrize("status", [InvoiceStatus.RED, InvoiceStatus.VOID])
def test_supplier_reports_apply_invoice_status_change_at_audit_time(
    finance_user,
    status,
):
    invoice = make_invoice(
        finance_user,
        total_amount=Decimal("1000.00"),
        invoice_number=f"HISTORICAL-{status}",
    )
    payment = make_transaction(
        finance_user,
        counterparty=invoice.counterparty,
        amount=Decimal("300.00"),
    )
    _change_invoice_status(
        invoice,
        status,
        finance_user,
        datetime(2026, 8, 1, 8, tzinfo=UTC),
    )

    historical_summary = supplier_summaries_as_of(date(2026, 7, 31))[0]
    historical_ledger = supplier_ledger_as_of(
        invoice.counterparty_id,
        date(2026, 7, 31),
    )
    changed_summary = supplier_summaries_as_of(date(2026, 8, 1))[0]
    changed_ledger = supplier_ledger_as_of(
        invoice.counterparty_id,
        date(2026, 8, 1),
    )

    assert historical_summary.invoiced_amount == Decimal("1000.00")
    assert historical_summary.balance == Decimal("700.00")
    assert [row.reference_id for row in historical_ledger] == [invoice.id, payment.id]
    assert [row.running_balance for row in historical_ledger] == [
        Decimal("1000.00"),
        Decimal("700.00"),
    ]
    assert changed_summary.invoiced_amount == Decimal("0.00")
    assert changed_summary.balance == Decimal("-300.00")
    assert [row.reference_id for row in changed_ledger] == [payment.id]
    assert changed_ledger[0].running_balance == Decimal("-300.00")


@pytest.mark.django_db
def test_supplier_summaries_filter_by_name_and_activity(finance_user):
    matched = Counterparty.objects.create(
        name="无锡测试供应商",
        normalized_name="无锡测试供应商",
        is_supplier=True,
    )
    inactive = Counterparty.objects.create(
        name="无业务供应商",
        normalized_name="无业务供应商",
        is_supplier=True,
    )
    make_invoice(finance_user, counterparty=matched)

    rows = supplier_summaries_as_of(date(2026, 7, 31), search="无锡")

    assert [row.counterparty_id for row in rows] == [matched.id]
    assert inactive.id not in {row.counterparty_id for row in rows}


@pytest.mark.django_db
def test_supplier_summaries_are_sorted_and_keep_supplier_amounts_isolated(
    finance_user,
):
    second = Counterparty.objects.create(
        name="乙供应商",
        normalized_name="乙供应商",
        is_supplier=True,
    )
    first = Counterparty.objects.create(
        name="甲供应商",
        normalized_name="甲供应商",
        is_supplier=True,
    )
    make_invoice(
        finance_user,
        counterparty=second,
        total_amount=Decimal("2000.00"),
    )
    make_invoice(
        finance_user,
        counterparty=first,
        total_amount=Decimal("1000.00"),
    )
    make_transaction(
        finance_user,
        counterparty=first,
        amount=Decimal("300.00"),
    )

    rows = supplier_summaries_as_of(date(2026, 7, 31))

    assert [row.counterparty_name for row in rows] == ["乙供应商", "甲供应商"]
    assert [(row.invoiced_amount, row.paid_amount) for row in rows] == [
        (Decimal("2000.00"), Decimal("0.00")),
        (Decimal("1000.00"), Decimal("300.00")),
    ]


@pytest.mark.django_db
def test_supplier_summary_separates_open_invoice_and_open_payment(finance_user):
    invoice = make_invoice(finance_user, total_amount=Decimal("1000.00"))
    payment = make_transaction(
        finance_user,
        counterparty=invoice.counterparty,
        amount=Decimal("700.00"),
    )
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(invoice.id, payment.id, Decimal("600.00"))],
        allow_partial=True,
    )
    _set_reconciliation_time(reconciliation, datetime(2026, 7, 31, 8, tzinfo=UTC))

    row = supplier_summaries_as_of(date(2026, 7, 31))[0]

    assert row.invoice_open_amount == Decimal("400.00")
    assert row.payment_open_amount == Decimal("100.00")
    assert row.balance == Decimal("300.00")


@pytest.mark.django_db
def test_supplier_summary_applies_reconciliation_reversal_at_reversal_time(
    finance_user,
):
    invoice = make_invoice(finance_user, total_amount=Decimal("1000.00"))
    payment = make_transaction(
        finance_user,
        counterparty=invoice.counterparty,
        amount=Decimal("700.00"),
    )
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(invoice.id, payment.id, Decimal("600.00"))],
        allow_partial=True,
    )
    _set_reconciliation_time(reconciliation, datetime(2026, 7, 10, 8, tzinfo=UTC))
    reversal = ReconciliationReversal.objects.create(
        original=reconciliation,
        reversed_by=finance_user,
        reason="供应商汇总历史时点测试",
    )
    ReconciliationReversal.objects.filter(pk=reversal.pk).update(
        created_at=datetime(2026, 7, 20, 8, tzinfo=UTC)
    )

    before_reversal = supplier_summaries_as_of(date(2026, 7, 19))[0]
    after_reversal = supplier_summaries_as_of(date(2026, 7, 20))[0]

    assert before_reversal.invoice_open_amount == Decimal("400.00")
    assert before_reversal.payment_open_amount == Decimal("100.00")
    assert after_reversal.invoice_open_amount == Decimal("1000.00")
    assert after_reversal.payment_open_amount == Decimal("700.00")


@pytest.mark.django_db
def test_supplier_summary_as_of_returns_zero_row_for_inactive_supplier(finance_user):
    supplier = Counterparty.objects.create(
        name="暂无业务供应商",
        normalized_name="暂无业务供应商",
        is_supplier=True,
    )

    row = supplier_summary_as_of(supplier.id, date(2026, 7, 31))

    assert row == SupplierSummaryRow(
        counterparty_id=supplier.id,
        counterparty_name="暂无业务供应商",
        invoiced_amount=Decimal("0.00"),
        paid_amount=Decimal("0.00"),
        balance=Decimal("0.00"),
        invoice_open_amount=Decimal("0.00"),
        payment_open_amount=Decimal("0.00"),
        latest_activity_on=None,
    )


def test_supplier_summary_totals_aggregate_rows():
    rows = (
        SupplierSummaryRow(
            counterparty_id=uuid4(),
            counterparty_name="甲供应商",
            invoiced_amount=Decimal("1000.00"),
            paid_amount=Decimal("300.00"),
            balance=Decimal("700.00"),
            invoice_open_amount=Decimal("800.00"),
            payment_open_amount=Decimal("100.00"),
            latest_activity_on=date(2026, 7, 1),
        ),
        SupplierSummaryRow(
            counterparty_id=uuid4(),
            counterparty_name="乙供应商",
            invoiced_amount=Decimal("2500.00"),
            paid_amount=Decimal("900.00"),
            balance=Decimal("1600.00"),
            invoice_open_amount=Decimal("1700.00"),
            payment_open_amount=Decimal("100.00"),
            latest_activity_on=date(2026, 7, 2),
        ),
    )

    totals = supplier_summary_totals(rows)

    assert totals.supplier_count == 2
    assert totals.invoiced_amount == Decimal("3500.00")
    assert totals.paid_amount == Decimal("1200.00")
    assert totals.balance == Decimal("2300.00")


@pytest.mark.django_db
def test_supplier_summary_is_batched(finance_user):
    for index in range(8):
        supplier = Counterparty.objects.create(
            name=f"供应商 {index}",
            normalized_name=f"supplier-{index}",
            is_supplier=True,
        )
        make_invoice(finance_user, counterparty=supplier)
        make_transaction(finance_user, counterparty=supplier)

    with CaptureQueriesContext(connection) as queries:
        rows = supplier_summaries_as_of(date(2026, 7, 31))

    assert len(rows) == 8
    assert len(queries) <= 3


@pytest.mark.django_db
def test_supplier_ledger_mixes_invoices_and_payments_with_running_balance(
    finance_user,
):
    supplier = Counterparty.objects.create(
        name="时间线供应商",
        normalized_name="时间线供应商",
        is_supplier=True,
    )
    invoice = make_invoice(
        finance_user,
        counterparty=supplier,
        total_amount=Decimal("8800.00"),
        invoice_number="INV-8800",
    )
    invoice.issue_date = date(2026, 4, 1)
    invoice.save(update_fields=["issue_date"])
    payment = make_transaction(
        finance_user,
        counterparty=supplier,
        amount=Decimal("6500.00"),
    )
    payment.occurred_at = datetime(2026, 4, 2, 9, tzinfo=UTC)
    payment.save(update_fields=["occurred_at"])

    rows = supplier_ledger_as_of(supplier.id, date(2026, 7, 31))

    assert [row.kind for row in rows] == [
        SupplierLedgerKind.INVOICE,
        SupplierLedgerKind.PAYMENT,
    ]
    assert rows[0].increase == Decimal("8800.00")
    assert rows[0].decrease == Decimal("0.00")
    assert rows[0].running_balance == Decimal("8800.00")
    assert rows[0].allocated_amount == Decimal("0.00")
    assert rows[0].open_amount == Decimal("8800.00")
    assert rows[1].increase == Decimal("0.00")
    assert rows[1].decrease == Decimal("6500.00")
    assert rows[1].running_balance == Decimal("2300.00")
    assert rows[1].allocated_amount == Decimal("0.00")
    assert rows[1].open_amount == Decimal("6500.00")
    assert rows[1].channel == MoneyChannel.BANK


@pytest.mark.django_db
def test_supplier_ledger_orders_invoice_before_payment_on_same_day(finance_user):
    invoice = make_invoice(finance_user)
    invoice.issue_date = date(2026, 4, 1)
    invoice.save(update_fields=["issue_date"])
    payment = make_transaction(
        finance_user,
        counterparty=invoice.counterparty,
    )
    payment.occurred_at = datetime(2026, 4, 1, 9, tzinfo=UTC)
    payment.save(update_fields=["occurred_at"])

    rows = supplier_ledger_as_of(invoice.counterparty_id, date(2026, 7, 31))

    assert [row.kind for row in rows] == [
        SupplierLedgerKind.INVOICE,
        SupplierLedgerKind.PAYMENT,
    ]


@pytest.mark.django_db
def test_supplier_ledger_batches_current_reconciliation_state(finance_user):
    supplier = Counterparty.objects.create(
        name="批量核销状态供应商",
        normalized_name="批量核销状态供应商",
        is_supplier=True,
    )
    for index in range(6):
        invoice = make_invoice(
            finance_user,
            counterparty=supplier,
            invoice_number=f"CURRENT-STATE-{index}",
        )
        payment = make_transaction(
            finance_user,
            counterparty=supplier,
        )
        reconciliation = create_reconciliation(
            actor=finance_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[AllocationInput(invoice.id, payment.id, Decimal("1000.00"))],
        )
        _set_reconciliation_time(
            reconciliation,
            datetime(2026, 8, 1, 8, tzinfo=UTC),
        )

    with CaptureQueriesContext(connection) as queries:
        rows = supplier_ledger_as_of(supplier.id, date(2026, 7, 31))

    invoice_rows = [row for row in rows if row.kind == SupplierLedgerKind.INVOICE]
    assert len(invoice_rows) == 6
    assert all(row.open_amount == Decimal("1000.00") for row in invoice_rows)
    assert all(not row.can_reconcile for row in invoice_rows)
    assert len(queries) <= 4


@pytest.mark.django_db
def test_supplier_ledger_uses_historical_allocation_and_reversal_times(finance_user):
    invoice = make_invoice(
        finance_user,
        total_amount=Decimal("1000.00"),
        invoice_number="INV-HISTORY",
    )
    invoice.issue_date = date(2026, 7, 1)
    invoice.save(update_fields=["issue_date"])
    payment = make_transaction(
        finance_user,
        counterparty=invoice.counterparty,
        amount=Decimal("700.00"),
    )
    payment.occurred_at = datetime(2026, 7, 1, 9, tzinfo=UTC)
    payment.save(update_fields=["occurred_at"])
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(invoice.id, payment.id, Decimal("600.00"))],
        allow_partial=True,
    )
    _set_reconciliation_time(
        reconciliation,
        datetime(2026, 7, 10, 8, tzinfo=UTC),
    )
    reversal = ReconciliationReversal.objects.create(
        original=reconciliation,
        reversed_by=finance_user,
        reason="供应商逐笔历史时点测试",
    )
    ReconciliationReversal.objects.filter(pk=reversal.pk).update(
        created_at=datetime(2026, 7, 20, 8, tzinfo=UTC)
    )

    before_allocation = supplier_ledger_as_of(
        invoice.counterparty_id,
        date(2026, 7, 9),
    )
    active_allocation = supplier_ledger_as_of(
        invoice.counterparty_id,
        date(2026, 7, 10),
    )
    before_reversal = supplier_ledger_as_of(
        invoice.counterparty_id,
        date(2026, 7, 19),
    )
    after_reversal = supplier_ledger_as_of(
        invoice.counterparty_id,
        date(2026, 7, 20),
    )

    assert [
        (row.allocated_amount, row.open_amount) for row in before_allocation
    ] == [
        (Decimal("0.00"), Decimal("1000.00")),
        (Decimal("0.00"), Decimal("700.00")),
    ]
    assert [
        (row.allocated_amount, row.open_amount) for row in active_allocation
    ] == [
        (Decimal("600.00"), Decimal("400.00")),
        (Decimal("600.00"), Decimal("100.00")),
    ]
    assert [
        (row.allocated_amount, row.open_amount) for row in before_reversal
    ] == [
        (Decimal("600.00"), Decimal("400.00")),
        (Decimal("600.00"), Decimal("100.00")),
    ]
    assert [
        (row.allocated_amount, row.open_amount) for row in after_reversal
    ] == [
        (Decimal("0.00"), Decimal("1000.00")),
        (Decimal("0.00"), Decimal("700.00")),
    ]


@pytest.mark.django_db
def test_supplier_ledger_traces_source_files_and_missing_sources(
    finance_user,
    settings,
    tmp_path,
):
    settings.MEDIA_ROOT = tmp_path
    invoice = make_invoice(finance_user, invoice_number="INV-SOURCE")
    sourced_payment = make_transaction(
        finance_user,
        counterparty=invoice.counterparty,
        amount=Decimal("300.00"),
    )
    unsourced_payment = make_transaction(
        finance_user,
        counterparty=invoice.counterparty,
        amount=Decimal("200.00"),
    )
    invoice_source = _add_source_file(
        invoice.import_batch,
        "invoice-source.csv",
        "a" * 64,
    )
    payment_source = _add_source_file(
        sourced_payment.import_batch,
        "payment-source.csv",
        "b" * 64,
    )

    rows = supplier_ledger_as_of(invoice.counterparty_id, date(2026, 7, 31))
    rows_by_reference = {row.reference_id: row for row in rows}

    assert rows_by_reference[invoice.id].import_batch_id == invoice.import_batch_id
    assert rows_by_reference[invoice.id].source_file_id == invoice_source.id
    assert (
        rows_by_reference[sourced_payment.id].import_batch_id
        == sourced_payment.import_batch_id
    )
    assert rows_by_reference[sourced_payment.id].source_file_id == payment_source.id
    assert (
        rows_by_reference[unsourced_payment.id].import_batch_id
        == unsourced_payment.import_batch_id
    )
    assert rows_by_reference[unsourced_payment.id].source_file_id is None


@pytest.mark.django_db
def test_supplier_coverage_requires_all_sources_and_years():
    for year in (2025, 2026):
        for source_kind in (
            SourceKind.INPUT_INVOICE,
            SourceKind.BANK,
            SourceKind.WECHAT,
        ):
            _coverage(year, source_kind)

    result = supplier_coverage_as_of(date(2026, 7, 31))

    assert result.code == "full"
    assert result.label == "资料完整"


@pytest.mark.django_db
def test_supplier_coverage_reports_incomplete_and_unregistered():
    unregistered = supplier_coverage_as_of(date(2026, 7, 31))
    assert unregistered.code == "unregistered"
    assert unregistered.label == "完整性未登记"

    _coverage(2026, SourceKind.INPUT_INVOICE, CoverageStatus.PARTIAL)

    incomplete = supplier_coverage_as_of(date(2026, 7, 31))
    assert incomplete.code == "incomplete"
    assert incomplete.label == "资料不完整"


@pytest.mark.django_db
def test_supplier_coverage_reports_incomplete_for_explicit_missing_period():
    _coverage(2026, SourceKind.BANK, CoverageStatus.MISSING)

    result = supplier_coverage_as_of(date(2026, 7, 31))

    assert result.code == "incomplete"
    assert result.label == "资料不完整"


@pytest.mark.django_db
def test_supplier_coverage_reports_unregistered_for_missing_required_combination():
    for source_kind in (
        SourceKind.INPUT_INVOICE,
        SourceKind.BANK,
        SourceKind.WECHAT,
    ):
        _coverage(2025, source_kind)
    _coverage(2026, SourceKind.INPUT_INVOICE)
    _coverage(2026, SourceKind.BANK)

    result = supplier_coverage_as_of(date(2026, 7, 31))

    assert result.code == "unregistered"
    assert result.label == "完整性未登记"
