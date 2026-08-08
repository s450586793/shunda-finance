from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from apps.imports.choices import BatchStatus, SourceKind
from apps.imports.models import CoverageStatus, DataCoveragePeriod, ImportBatch
from apps.ledger.choices import InvoiceDirection, MoneyDirection
from apps.ledger.models import AccountBalanceSnapshot, FundingAccount
from apps.reporting.dashboard import dashboard_payload
from tests.builders import make_invoice, make_transaction


@pytest.mark.django_db
def test_dashboard_uses_calendar_month_cashflow_and_open_invoice_totals(finance_user):
    receivable = make_invoice(
        finance_user,
        direction=InvoiceDirection.OUTPUT,
        total_amount=Decimal("1200.00"),
    )
    receivable.due_date = date(2026, 7, 20)
    receivable.save(update_fields=["due_date"])
    payable = make_invoice(
        finance_user,
        direction=InvoiceDirection.INPUT,
        total_amount=Decimal("700.00"),
    )
    payable.due_date = date(2026, 8, 5)
    payable.save(update_fields=["due_date"])
    inflow = make_transaction(
        finance_user,
        direction=MoneyDirection.INFLOW,
        amount=Decimal("300.00"),
        counterparty=receivable.counterparty,
    )
    outflow = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("80.00"),
        counterparty=payable.counterparty,
    )
    inflow.occurred_at = datetime(2026, 7, 15, 9, tzinfo=UTC)
    outflow.occurred_at = datetime(2026, 7, 31, 10, tzinfo=UTC)
    inflow.save(update_fields=["occurred_at"])
    outflow.save(update_fields=["occurred_at"])
    june = make_transaction(finance_user, amount=Decimal("999.00"))
    june.occurred_at = datetime(2026, 6, 30, 10, tzinfo=UTC)
    june.save(update_fields=["occurred_at"])

    payload = dashboard_payload(date(2026, 7, 1))

    assert payload.month_inflow == Decimal("300.00")
    assert payload.month_outflow == Decimal("80.00")
    assert payload.receivables == Decimal("1200.00")
    assert payload.overdue_receivables == Decimal("1200.00")
    assert payload.payables == Decimal("700.00")
    assert payload.due_within_7_days == Decimal("700.00")
    assert len(payload.daily_cashflow) == 31
    assert payload.daily_cashflow[14].inflow == Decimal("300.00")


@pytest.mark.django_db
def test_dashboard_current_funds_uses_latest_snapshot_per_active_account(finance_user):
    first = FundingAccount.objects.create(
        channel="bank",
        name="基本户",
        identifier="full-secret-1",
        masked_identifier="****0001",
    )
    second = FundingAccount.objects.create(
        channel="bank",
        name="一般户",
        identifier="full-secret-2",
        masked_identifier="****0002",
    )
    old_batch = ImportBatch.objects.create(source_kind=SourceKind.BANK, created_by=finance_user)
    AccountBalanceSnapshot.objects.create(
        account=first,
        as_of=datetime(2026, 7, 20, tzinfo=UTC),
        balance=Decimal("100.00"),
        source_batch=old_batch,
    )
    AccountBalanceSnapshot.objects.create(
        account=first,
        as_of=datetime(2026, 7, 31, 15, tzinfo=UTC),
        balance=Decimal("150.00"),
        source_batch=old_batch,
    )
    AccountBalanceSnapshot.objects.create(
        account=first,
        as_of=datetime(2026, 8, 1, tzinfo=UTC),
        balance=Decimal("999.00"),
        source_batch=old_batch,
    )

    payload = dashboard_payload(date(2026, 7, 12))

    assert payload.current_funds == Decimal("150.00")
    assert payload.data_incomplete is True
    assert "full-secret-1" not in repr(payload)
    assert second.balance_snapshots.count() == 0


@pytest.mark.django_db
def test_dashboard_returns_none_when_no_active_account_has_snapshot(finance_user):
    FundingAccount.objects.create(
        channel="bank",
        name="空账户",
        identifier="empty",
        masked_identifier="****",
    )

    payload = dashboard_payload(date(2026, 7, 1))

    assert payload.current_funds is None
    assert payload.data_incomplete is True


@pytest.mark.django_db
def test_dashboard_marks_partial_import_and_coverage_warning(finance_user):
    partial = ImportBatch.objects.create(
        source_kind=SourceKind.BANK,
        status=BatchStatus.PARTIAL,
        created_by=finance_user,
    )
    ImportBatch.objects.filter(pk=partial.pk).update(
        created_at=datetime(2026, 7, 1, tzinfo=UTC)
    )
    ImportBatch.objects.create(
        source_kind=SourceKind.BANK,
        status=BatchStatus.UPLOADED,
        created_by=finance_user,
    )
    DataCoveragePeriod.objects.create(
        year=2026,
        source_kind=SourceKind.INPUT_INVOICE,
        status=CoverageStatus.PARTIAL,
        expected_start=date(2026, 1, 1),
        expected_end=date(2026, 12, 31),
        actual_start=date(2026, 2, 1),
        actual_end=date(2026, 12, 1),
    )

    payload = dashboard_payload(date(2026, 7, 1))

    assert payload.data_incomplete is True
    assert payload.incomplete_batch_ids == (partial.id,)


@pytest.mark.django_db
def test_dashboard_has_stable_zero_filled_chart_buckets(finance_user):
    payload = dashboard_payload(date(2026, 2, 1))

    assert len(payload.daily_cashflow) == 28
    assert [item.label for item in payload.receivable_aging] == [
        "0-30", "31-60", "61-90", "90+"
    ]
    assert [item.label for item in payload.payable_due_buckets] == [
        "已逾期", "7日内", "8-30日", "30日以上"
    ]
    assert all(item.amount == Decimal("0.00") for item in payload.receivable_aging)
