from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.imports.choices import BatchStatus, SourceKind
from apps.imports.models import (
    CoverageStatus,
    DataCoveragePeriod,
    ImportBatch,
    StagedRow,
)
from apps.ledger.choices import InvoiceDirection, InvoiceStatus, MoneyDirection
from apps.ledger.models import AccountBalanceSnapshot
from apps.reconciliation.choices import ReconciliationDirection
from apps.reconciliation.models import ReconciliationReversal
from apps.reconciliation.services import AllocationInput, create_reconciliation
from apps.reporting.queries import (
    ExceptionType,
    aging_bucket,
    exception_items,
    payables_as_of,
    receivables_as_of,
)
from tests.builders import make_invoice, make_transaction


def _set_reconciliation_time(reconciliation, value):
    type(reconciliation).objects.filter(pk=reconciliation.pk).update(created_at=value)


def _set_batch_time(batch, value):
    type(batch).objects.filter(pk=batch.pk).update(created_at=value)


@pytest.mark.django_db
def test_receivables_use_historical_allocation_and_reversal_times(finance_user):
    invoice = make_invoice(
        finance_user,
        direction=InvoiceDirection.OUTPUT,
        total_amount=Decimal("1000.00"),
    )
    receipt = make_transaction(
        finance_user,
        direction=MoneyDirection.INFLOW,
        amount=Decimal("400.00"),
        counterparty=invoice.counterparty,
    )
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.SALES_RECEIPT,
        allocations=[AllocationInput(invoice.id, receipt.id, Decimal("400.00"))],
    )
    _set_reconciliation_time(reconciliation, datetime(2026, 7, 10, 8, tzinfo=UTC))
    reversal = ReconciliationReversal.objects.create(
        original=reconciliation,
        reversed_by=finance_user,
        reason="历史时点测试",
    )
    ReconciliationReversal.objects.filter(pk=reversal.pk).update(
        created_at=datetime(2026, 7, 20, 8, tzinfo=UTC)
    )

    assert receivables_as_of(date(2026, 7, 9))[0].open_amount == Decimal("1000.00")
    assert receivables_as_of(date(2026, 7, 10))[0].open_amount == Decimal("600.00")
    assert receivables_as_of(date(2026, 7, 19))[0].open_amount == Decimal("600.00")
    assert receivables_as_of(date(2026, 7, 20))[0].open_amount == Decimal("1000.00")


@pytest.mark.django_db
def test_open_invoice_queries_exclude_future_closed_void_and_red(finance_user):
    payable = make_invoice(finance_user, direction=InvoiceDirection.INPUT)
    future = make_invoice(finance_user, direction=InvoiceDirection.INPUT)
    future.issue_date = date(2026, 8, 1)
    future.save(update_fields=["issue_date"])
    void = make_invoice(finance_user, direction=InvoiceDirection.INPUT)
    void.status = InvoiceStatus.VOID
    void.save(update_fields=["status"])
    red = make_invoice(finance_user, direction=InvoiceDirection.OUTPUT)
    red.status = InvoiceStatus.RED
    red.save(update_fields=["status"])
    payment = make_transaction(
        finance_user,
        amount=payable.total_amount,
        counterparty=payable.counterparty,
    )
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(payable.id, payment.id, payable.total_amount)],
    )
    _set_reconciliation_time(reconciliation, datetime(2026, 7, 2, tzinfo=UTC))

    assert payables_as_of(date(2026, 7, 28)) == ()
    assert receivables_as_of(date(2026, 7, 28)) == ()


@pytest.mark.parametrize(
    ("days", "expected"),
    [(0, "0-30"), (30, "0-30"), (31, "31-60"), (60, "31-60"), (61, "61-90"), (90, "61-90"), (91, "90+"), (-5, "0-30")],
)
def test_aging_bucket_boundaries(days, expected):
    as_of = date(2026, 7, 28)
    assert aging_bucket(as_of - timedelta(days=days), as_of) == expected


@pytest.mark.django_db
def test_open_invoice_query_is_batched_and_stably_sorted(finance_user):
    for index in range(8):
        invoice = make_invoice(
            finance_user,
            direction=InvoiceDirection.OUTPUT,
            invoice_number=f"BATCHED-{index}",
        )
        invoice.issue_date = date(2026, 7, 1 + index)
        invoice.save(update_fields=["issue_date"])

    with CaptureQueriesContext(connection) as queries:
        rows = receivables_as_of(date(2026, 7, 28))

    assert len(queries) <= 2
    assert [row.issue_date for row in rows] == sorted(row.issue_date for row in rows)


@pytest.mark.django_db
def test_exception_items_cover_open_money_partial_red_stale_and_import_issues(finance_user):
    output = make_invoice(
        finance_user,
        direction=InvoiceDirection.OUTPUT,
        total_amount=Decimal("1000.00"),
    )
    output.due_date = date(2026, 4, 28)
    output.save(update_fields=["due_date"])
    input_invoice = make_invoice(finance_user, direction=InvoiceDirection.INPUT)
    receipt = make_transaction(
        finance_user,
        direction=MoneyDirection.INFLOW,
        amount=Decimal("400.00"),
        counterparty=output.counterparty,
    )
    unmatched_payment = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("250.00"),
        counterparty=input_invoice.counterparty,
    )
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.SALES_RECEIPT,
        allocations=[AllocationInput(output.id, receipt.id, Decimal("300.00"))],
    )
    _set_reconciliation_time(reconciliation, datetime(2026, 7, 3, tzinfo=UTC))
    red = make_invoice(finance_user, direction=InvoiceDirection.OUTPUT)
    red_receipt = make_transaction(
        finance_user,
        direction=MoneyDirection.INFLOW,
        counterparty=red.counterparty,
    )
    red_reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.SALES_RECEIPT,
        allocations=[AllocationInput(red.id, red_receipt.id, Decimal("100.00"))],
    )
    _set_reconciliation_time(red_reconciliation, datetime(2026, 7, 4, tzinfo=UTC))
    red.status = InvoiceStatus.RED
    red.save(update_fields=["status"])
    unknown = make_transaction(finance_user, amount=Decimal("50.00"))
    unknown.counterparty = None
    unknown.counterparty_raw_name = "无法识别的单位"
    unknown.save(update_fields=["counterparty", "counterparty_raw_name"])

    duplicate_batch = ImportBatch.objects.create(
        source_kind=SourceKind.BANK,
        status=BatchStatus.COMPLETED,
        duplicate_rows=1,
        created_by=finance_user,
    )
    _set_batch_time(duplicate_batch, datetime(2026, 7, 27, tzinfo=UTC))
    StagedRow.objects.create(
        batch=duplicate_batch,
        row_number=7,
        raw_data={"secret": "不得出现在异常描述"},
        is_duplicate=True,
    )
    issue_batch = ImportBatch.objects.create(
        source_kind=SourceKind.OUTPUT_INVOICE,
        status=BatchStatus.PARTIAL,
        error_rows=1,
        created_by=finance_user,
    )
    _set_batch_time(issue_batch, datetime(2026, 7, 27, tzinfo=UTC))
    StagedRow.objects.create(
        batch=issue_batch,
        row_number=3,
        raw_data={"customer": "敏感原文"},
        issues=[{"code": "counterparty", "message": "无法识别往来单位"}],
    )
    DataCoveragePeriod.objects.create(
        year=2026,
        source_kind=SourceKind.BANK,
        status=CoverageStatus.MISSING,
        expected_start=date(2026, 1, 1),
        expected_end=date(2026, 12, 31),
    )

    items = exception_items(date(2026, 7, 28))
    types = {item.type for item in items}

    assert set(ExceptionType) == {
        ExceptionType.RECEIVABLE_OPEN,
        ExceptionType.PAYABLE_OPEN,
        ExceptionType.INFLOW_UNMATCHED,
        ExceptionType.OUTFLOW_UNMATCHED,
        ExceptionType.COUNTERPARTY_UNKNOWN,
        ExceptionType.RECONCILIATION_DIFFERENCE,
        ExceptionType.DUPLICATE_IMPORT,
        ExceptionType.RED_WITH_ACTIVE_ALLOCATION,
        ExceptionType.STALE_OPEN_ITEM,
        ExceptionType.HISTORY_INCOMPLETE,
    }
    assert set(ExceptionType) <= types
    assert any(item.reference_id == unmatched_payment.id for item in items)
    assert all("secret" not in item.detail and "敏感原文" not in item.detail for item in items)


@pytest.mark.django_db
def test_stale_exception_starts_at_91_days(finance_user):
    as_of = date(2026, 7, 28)
    stale = make_invoice(finance_user, direction=InvoiceDirection.OUTPUT)
    stale.due_date = as_of - timedelta(days=91)
    stale.save(update_fields=["due_date"])
    recent = make_invoice(finance_user, direction=InvoiceDirection.OUTPUT)
    recent.due_date = as_of - timedelta(days=90)
    recent.save(update_fields=["due_date"])

    stale_ids = {
        item.reference_id
        for item in exception_items(as_of)
        if item.type == ExceptionType.STALE_OPEN_ITEM
    }

    assert stale.id in stale_ids
    assert recent.id not in stale_ids


@pytest.mark.django_db
def test_unmatched_money_uses_historical_open_amount(finance_user):
    invoice = make_invoice(finance_user, direction=InvoiceDirection.OUTPUT)
    receipt = make_transaction(
        finance_user,
        direction=MoneyDirection.INFLOW,
        counterparty=invoice.counterparty,
    )
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.SALES_RECEIPT,
        allocations=[AllocationInput(invoice.id, receipt.id, receipt.amount)],
    )
    _set_reconciliation_time(reconciliation, datetime(2026, 8, 1, tzinfo=UTC))

    july_items = exception_items(date(2026, 7, 31))
    august_items = exception_items(date(2026, 8, 1))

    assert any(
        item.type == ExceptionType.INFLOW_UNMATCHED
        and item.reference_id == receipt.id
        and item.amount == Decimal("1000.00")
        for item in july_items
    )
    assert not any(
        item.type == ExceptionType.INFLOW_UNMATCHED
        and item.reference_id == receipt.id
        for item in august_items
    )


@pytest.mark.django_db
def test_balance_snapshot_never_changes_receivable_open_amount(finance_user):
    invoice = make_invoice(finance_user, direction=InvoiceDirection.OUTPUT)
    account = make_transaction(finance_user).account
    AccountBalanceSnapshot.objects.create(
        account=account,
        as_of=datetime(2026, 7, 20, tzinfo=UTC),
        balance=Decimal("999999.00"),
        source_batch=invoice.import_batch,
    )

    assert receivables_as_of(date(2026, 7, 28))[0].open_amount == Decimal("1000.00")


@pytest.mark.django_db
def test_fully_reconciled_items_are_not_reconciliation_differences(finance_user):
    invoice = make_invoice(finance_user, direction=InvoiceDirection.OUTPUT)
    receipt = make_transaction(
        finance_user,
        direction=MoneyDirection.INFLOW,
        counterparty=invoice.counterparty,
    )
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.SALES_RECEIPT,
        allocations=[AllocationInput(invoice.id, receipt.id, invoice.total_amount)],
    )
    _set_reconciliation_time(reconciliation, datetime(2026, 7, 3, tzinfo=UTC))

    assert not any(
        item.type == ExceptionType.RECONCILIATION_DIFFERENCE
        for item in exception_items(date(2026, 7, 28))
    )


@pytest.mark.django_db
def test_equal_partial_invoice_and_money_residuals_are_not_a_difference(finance_user):
    invoice = make_invoice(finance_user, direction=InvoiceDirection.OUTPUT)
    receipt = make_transaction(
        finance_user,
        direction=MoneyDirection.INFLOW,
        counterparty=invoice.counterparty,
    )
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.SALES_RECEIPT,
        allocations=[AllocationInput(invoice.id, receipt.id, Decimal("300.00"))],
    )
    _set_reconciliation_time(reconciliation, datetime(2026, 7, 3, tzinfo=UTC))

    assert not any(
        item.type == ExceptionType.RECONCILIATION_DIFFERENCE
        for item in exception_items(date(2026, 7, 28))
    )


@pytest.mark.django_db
def test_duplicate_batch_counter_is_reported_without_staged_duplicate_row(finance_user):
    batch = ImportBatch.objects.create(
        source_kind=SourceKind.BANK,
        status=BatchStatus.COMPLETED,
        duplicate_rows=2,
        created_by=finance_user,
    )
    _set_batch_time(batch, datetime(2026, 7, 27, tzinfo=UTC))

    duplicates = [
        item
        for item in exception_items(date(2026, 7, 28))
        if item.type == ExceptionType.DUPLICATE_IMPORT
    ]

    assert len(duplicates) == 1
    assert duplicates[0].reference_id == batch.id
    assert "2 行" in duplicates[0].detail
