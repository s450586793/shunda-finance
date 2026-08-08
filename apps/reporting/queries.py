from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from django.db import models
from django.db.models import (
    CharField,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
    Sum,
    Value,
)
from django.db.models.functions import Cast, Coalesce, Replace
from django.utils import timezone

from apps.core.models import AuditLog
from apps.imports.choices import BatchStatus, SourceKind
from apps.imports.models import (
    CoverageStatus,
    DataCoveragePeriod,
    ImportBatch,
    StagedRow,
)
from apps.ledger.choices import InvoiceDirection, InvoiceStatus, MoneyDirection
from apps.ledger.models import Invoice, MoneyTransaction
from apps.parties.models import Counterparty
from apps.reconciliation.models import ReconciliationAllocation

MONEY_ZERO = Decimal("0.00")
MONEY_FIELD = models.DecimalField(max_digits=18, decimal_places=2)
AGING_LABELS = ("0-30", "31-60", "61-90", "90+")


@dataclass(frozen=True)
class OpenInvoiceRow:
    invoice_id: UUID
    counterparty_name: str
    issue_date: date
    due_date: date | None
    open_amount: Decimal
    aging_bucket: str
    import_batch_id: UUID
    source_file_id: UUID | None


@dataclass(frozen=True)
class OpenMoneyRow:
    transaction_id: UUID
    occurred_at: datetime
    counterparty_name: str
    open_amount: Decimal
    channel: str
    masked_account: str
    import_batch_id: UUID
    source_file_id: UUID | None


@dataclass(frozen=True)
class SupplierSummaryRow:
    counterparty_id: UUID
    counterparty_name: str
    invoiced_amount: Decimal
    paid_amount: Decimal
    balance: Decimal
    invoice_open_amount: Decimal
    payment_open_amount: Decimal
    latest_activity_on: date | None


@dataclass(frozen=True)
class SupplierSummaryTotals:
    supplier_count: int
    invoiced_amount: Decimal
    paid_amount: Decimal
    balance: Decimal


class SupplierLedgerKind(StrEnum):
    INVOICE = "invoice"
    PAYMENT = "payment"


@dataclass(frozen=True)
class SupplierLedgerRow:
    kind: SupplierLedgerKind
    reference_id: UUID
    occurred_on: date
    reference: str
    channel: str
    increase: Decimal
    decrease: Decimal
    running_balance: Decimal
    allocated_amount: Decimal
    open_amount: Decimal
    import_batch_id: UUID
    source_file_id: UUID | None
    can_reconcile: bool


@dataclass(frozen=True)
class SupplierCoverage:
    code: str
    label: str
    detail: str


SUPPLIER_SOURCE_KINDS = (
    SourceKind.INPUT_INVOICE,
    SourceKind.BANK,
    SourceKind.WECHAT,
)


class ExceptionType(StrEnum):
    RECEIVABLE_OPEN = "应收未收"
    PAYABLE_OPEN = "应付未付"
    INFLOW_UNMATCHED = "未匹配收款"
    OUTFLOW_UNMATCHED = "未匹配付款"
    COUNTERPARTY_UNKNOWN = "单位未识别"
    RECONCILIATION_DIFFERENCE = "核销差额"
    DUPLICATE_IMPORT = "疑似重复"
    RED_WITH_ACTIVE_ALLOCATION = "红冲待处理"
    STALE_OPEN_ITEM = "长期未核销"
    HISTORY_INCOMPLETE = "历史资料缺失"


@dataclass(frozen=True)
class ExceptionItem:
    type: ExceptionType
    reference_id: UUID
    occurred_on: date | None
    counterparty_name: str
    amount: Decimal | None
    detail: str


def _cutoff(as_of: date) -> datetime:
    next_day = datetime.combine(as_of + timedelta(days=1), time.min)
    return timezone.make_aware(next_day, timezone.get_current_timezone())


def _historically_valid_allocation(prefix: str, cutoff: datetime) -> Q:
    reconciliation = f"{prefix}reconciliation__"
    return Q(**{f"{reconciliation}created_at__lt": cutoff}) & (
        Q(**{f"{reconciliation}reversal__isnull": True})
        | Q(**{f"{reconciliation}reversal__created_at__gte": cutoff})
    )


def _with_historical_open_amount(query, total_field: str, as_of: date):
    cutoff = _cutoff(as_of)
    prefix = "reconciliationallocation__"
    query = query.annotate(
        allocated_amount=Coalesce(
            Sum(
                f"{prefix}amount",
                filter=_historically_valid_allocation(prefix, cutoff),
            ),
            MONEY_ZERO,
            output_field=MONEY_FIELD,
        )
    )
    return query.annotate(
        open_amount=ExpressionWrapper(
            F(total_field) - F("allocated_amount"),
            output_field=MONEY_FIELD,
        )
    )


def _with_current_open_amount(query, total_field: str):
    prefix = "reconciliationallocation__"
    query = query.annotate(
        current_allocated_amount=Coalesce(
            Sum(
                f"{prefix}amount",
                filter=Q(**{f"{prefix}reconciliation__reversal__isnull": True}),
            ),
            MONEY_ZERO,
            output_field=MONEY_FIELD,
        )
    )
    return query.annotate(
        current_open_amount=ExpressionWrapper(
            F(total_field) - F("current_allocated_amount"),
            output_field=MONEY_FIELD,
        )
    )


def aging_bucket(base_date: date, as_of: date) -> str:
    days = max((as_of - base_date).days, 0)
    if days <= 30:
        return AGING_LABELS[0]
    if days <= 60:
        return AGING_LABELS[1]
    if days <= 90:
        return AGING_LABELS[2]
    return AGING_LABELS[3]


def _open_invoice_rows(direction: str, as_of: date) -> tuple[OpenInvoiceRow, ...]:
    invoices = _with_historical_open_amount(
        Invoice.objects.filter(
            direction=direction,
            issue_date__lte=as_of,
            status=InvoiceStatus.NORMAL,
        ).select_related("counterparty", "import_batch").prefetch_related(
            "import_batch__source_files"
        ),
        "total_amount",
        as_of,
    ).filter(open_amount__gt=MONEY_ZERO).order_by("issue_date", "id")
    return tuple(
        OpenInvoiceRow(
            invoice_id=invoice.id,
            counterparty_name=invoice.counterparty.name,
            issue_date=invoice.issue_date,
            due_date=invoice.due_date,
            open_amount=invoice.open_amount,
            aging_bucket=aging_bucket(invoice.due_date or invoice.issue_date, as_of),
            import_batch_id=invoice.import_batch_id,
            source_file_id=_source_file_id(invoice.import_batch),
        )
        for invoice in invoices
    )


def receivables_as_of(as_of: date) -> tuple[OpenInvoiceRow, ...]:
    return _open_invoice_rows(InvoiceDirection.OUTPUT, as_of)


def payables_as_of(as_of: date) -> tuple[OpenInvoiceRow, ...]:
    return _open_invoice_rows(InvoiceDirection.INPUT, as_of)


def unmatched_funds_as_of(as_of: date) -> tuple[OpenMoneyRow, ...]:
    return tuple(
        OpenMoneyRow(
            transaction_id=money.id,
            occurred_at=money.occurred_at,
            counterparty_name=(
                money.counterparty.name if money.counterparty is not None else "未识别单位"
            ),
            open_amount=money.open_amount,
            channel=money.channel,
            masked_account=money.account.masked_identifier,
            import_batch_id=money.import_batch_id,
            source_file_id=_source_file_id(money.import_batch),
        )
        for money in _open_money(as_of)
        .select_related("account", "counterparty", "import_batch")
        .prefetch_related("import_batch__source_files")
    )


def _open_money(as_of: date):
    return _with_historical_open_amount(
        MoneyTransaction.objects.filter(occurred_at__lt=_cutoff(as_of)).select_related(
            "counterparty"
        ),
        "amount",
        as_of,
    ).filter(open_amount__gt=MONEY_ZERO).order_by("occurred_at", "id")


def _source_file_id(batch: ImportBatch) -> UUID | None:
    source_file = next(iter(batch.source_files.all()), None)
    return source_file.id if source_file is not None else None


def _supplier_invoice_records(
    as_of: date,
    search: str = "",
    counterparty_id: UUID | None = None,
):
    cutoff = _cutoff(as_of)
    invoice_id = Replace(
        Cast(OuterRef("pk"), output_field=CharField()),
        Value("-"),
        Value(""),
    )
    future_status_change = (
        AuditLog.objects.annotate(
            normalized_target_id=Replace("target_id", Value("-"), Value(""))
        )
        .filter(
            action="invoice.status_changed",
            target_type="Invoice",
            normalized_target_id=invoice_id,
            created_at__gte=cutoff,
        )
        .order_by()
    )
    invoices = (
        Invoice.objects.filter(
            direction=InvoiceDirection.INPUT,
            issue_date__lte=as_of,
            counterparty__is_supplier=True,
        )
        .annotate(status_changes_after_cutoff=Exists(future_status_change))
        .filter(
            Q(status=InvoiceStatus.NORMAL) | Q(status_changes_after_cutoff=True)
        )
        .select_related("counterparty")
    )
    rows = _with_current_open_amount(
        _with_historical_open_amount(invoices, "total_amount", as_of),
        "total_amount",
    )
    if search:
        rows = rows.filter(counterparty__name__icontains=search)
    if counterparty_id is not None:
        rows = rows.filter(counterparty_id=counterparty_id)
    return rows.order_by("issue_date", "id")


def _supplier_payment_records(
    as_of: date,
    search: str = "",
    counterparty_id: UUID | None = None,
):
    rows = _with_historical_open_amount(
        MoneyTransaction.objects.filter(
            direction=MoneyDirection.OUTFLOW,
            occurred_at__lt=_cutoff(as_of),
            counterparty__is_supplier=True,
        ).select_related("counterparty"),
        "amount",
        as_of,
    )
    if search:
        rows = rows.filter(counterparty__name__icontains=search)
    if counterparty_id is not None:
        rows = rows.filter(counterparty_id=counterparty_id)
    return rows.order_by("occurred_at", "id")


def _empty_supplier_summary(counterparty_id: UUID, counterparty_name: str):
    return {
        "counterparty_id": counterparty_id,
        "counterparty_name": counterparty_name,
        "invoiced_amount": MONEY_ZERO,
        "paid_amount": MONEY_ZERO,
        "balance": MONEY_ZERO,
        "invoice_open_amount": MONEY_ZERO,
        "payment_open_amount": MONEY_ZERO,
        "latest_activity_on": None,
    }


def _latest_activity(current: date | None, candidate: date) -> date:
    if current is None or candidate > current:
        return candidate
    return current


def _supplier_summary_rows(invoice_records, payment_records) -> tuple[SupplierSummaryRow, ...]:
    rows_by_counterparty = {}
    for invoice in invoice_records:
        summary = rows_by_counterparty.setdefault(
            invoice.counterparty_id,
            _empty_supplier_summary(invoice.counterparty_id, invoice.counterparty.name),
        )
        summary["invoiced_amount"] += invoice.total_amount
        summary["balance"] += invoice.total_amount
        summary["invoice_open_amount"] += invoice.open_amount
        summary["latest_activity_on"] = _latest_activity(
            summary["latest_activity_on"],
            invoice.issue_date,
        )
    for payment in payment_records:
        summary = rows_by_counterparty.setdefault(
            payment.counterparty_id,
            _empty_supplier_summary(payment.counterparty_id, payment.counterparty.name),
        )
        summary["paid_amount"] += payment.amount
        summary["balance"] -= payment.amount
        summary["payment_open_amount"] += payment.open_amount
        summary["latest_activity_on"] = _latest_activity(
            summary["latest_activity_on"],
            timezone.localtime(payment.occurred_at).date(),
        )
    return tuple(
        SupplierSummaryRow(**summary)
        for summary in sorted(
            rows_by_counterparty.values(),
            key=lambda row: (row["counterparty_name"], str(row["counterparty_id"])),
        )
    )


def supplier_summaries_as_of(as_of: date, search: str = "") -> tuple[SupplierSummaryRow, ...]:
    term = search.strip()
    return _supplier_summary_rows(
        _supplier_invoice_records(as_of, search=term),
        _supplier_payment_records(as_of, search=term),
    )


def supplier_summary_as_of(counterparty_id: UUID, as_of: date) -> SupplierSummaryRow:
    supplier = Counterparty.objects.get(id=counterparty_id, is_supplier=True)
    rows = _supplier_summary_rows(
        _supplier_invoice_records(as_of, counterparty_id=counterparty_id),
        _supplier_payment_records(as_of, counterparty_id=counterparty_id),
    )
    if rows:
        return rows[0]
    return SupplierSummaryRow(**_empty_supplier_summary(supplier.id, supplier.name))


def supplier_summary_totals(rows: tuple[SupplierSummaryRow, ...]) -> SupplierSummaryTotals:
    return SupplierSummaryTotals(
        supplier_count=len(rows),
        invoiced_amount=sum((row.invoiced_amount for row in rows), MONEY_ZERO),
        paid_amount=sum((row.paid_amount for row in rows), MONEY_ZERO),
        balance=sum((row.balance for row in rows), MONEY_ZERO),
    )


def supplier_ledger_as_of(
    counterparty_id: UUID,
    as_of: date,
) -> tuple[SupplierLedgerRow, ...]:
    invoice_records = (
        _supplier_invoice_records(as_of, counterparty_id=counterparty_id)
        .select_related("import_batch")
        .prefetch_related("import_batch__source_files")
    )
    payment_records = (
        _supplier_payment_records(as_of, counterparty_id=counterparty_id)
        .select_related("import_batch")
        .prefetch_related("import_batch__source_files")
    )
    rows = [
        SupplierLedgerRow(
            kind=SupplierLedgerKind.INVOICE,
            reference_id=invoice.id,
            occurred_on=invoice.issue_date,
            reference=invoice.invoice_number,
            channel="",
            increase=invoice.total_amount,
            decrease=MONEY_ZERO,
            running_balance=MONEY_ZERO,
            allocated_amount=invoice.allocated_amount,
            open_amount=invoice.open_amount,
            import_batch_id=invoice.import_batch_id,
            source_file_id=_source_file_id(invoice.import_batch),
            can_reconcile=(
                invoice.status == InvoiceStatus.NORMAL
                and invoice.current_open_amount > MONEY_ZERO
            ),
        )
        for invoice in invoice_records
    ]
    rows.extend(
        SupplierLedgerRow(
            kind=SupplierLedgerKind.PAYMENT,
            reference_id=payment.id,
            occurred_on=timezone.localtime(payment.occurred_at).date(),
            reference=payment.transaction_id or payment.fingerprint,
            channel=payment.channel,
            increase=MONEY_ZERO,
            decrease=payment.amount,
            running_balance=MONEY_ZERO,
            allocated_amount=payment.allocated_amount,
            open_amount=payment.open_amount,
            import_batch_id=payment.import_batch_id,
            source_file_id=_source_file_id(payment.import_batch),
            can_reconcile=False,
        )
        for payment in payment_records
    )
    rows.sort(
        key=lambda row: (
            row.occurred_on,
            0 if row.kind == SupplierLedgerKind.INVOICE else 1,
            str(row.reference_id),
        )
    )

    running_balance = MONEY_ZERO
    result = []
    for row in rows:
        running_balance += row.increase - row.decrease
        result.append(replace(row, running_balance=running_balance))
    return tuple(result)


def _supplier_coverage_detail(periods: tuple[DataCoveragePeriod, ...]) -> str:
    source_labels = dict(SourceKind.choices)
    status_labels = dict(CoverageStatus.choices)
    return "；".join(
        f"{period.year}年{source_labels[period.source_kind]}："
        f"{status_labels[period.status]}"
        for period in periods
    )


def supplier_coverage_as_of(as_of: date) -> SupplierCoverage:
    periods = tuple(
        DataCoveragePeriod.objects.filter(
            source_kind__in=SUPPLIER_SOURCE_KINDS,
            expected_start__lte=as_of,
        ).order_by("year", "source_kind")
    )
    if not periods:
        return SupplierCoverage("unregistered", "完整性未登记", "")

    detail = _supplier_coverage_detail(periods)
    if any(
        period.status in {CoverageStatus.PARTIAL, CoverageStatus.MISSING}
        for period in periods
    ):
        return SupplierCoverage("incomplete", "资料不完整", detail)

    earliest_year = min(period.year for period in periods)
    registered = {(period.year, period.source_kind) for period in periods}
    required = {
        (year, source_kind)
        for year in range(earliest_year, as_of.year + 1)
        for source_kind in SUPPLIER_SOURCE_KINDS
    }
    missing = sorted(required - registered, key=lambda item: (item[0], item[1]))
    if missing:
        source_labels = dict(SourceKind.choices)
        missing_detail = "；".join(
            f"{year}年{source_labels[source_kind]}：未登记"
            for year, source_kind in missing
        )
        return SupplierCoverage("unregistered", "完整性未登记", missing_detail)

    return SupplierCoverage("full", "资料完整", detail)


def _open_invoice_exceptions(as_of: date) -> list[ExceptionItem]:
    items = []
    for exception_type, rows in (
        (ExceptionType.RECEIVABLE_OPEN, receivables_as_of(as_of)),
        (ExceptionType.PAYABLE_OPEN, payables_as_of(as_of)),
    ):
        for row in rows:
            items.append(
                ExceptionItem(
                    exception_type,
                    row.invoice_id,
                    row.issue_date,
                    row.counterparty_name,
                    row.open_amount,
                    f"账龄 {row.aging_bucket}",
                )
            )
    return items


def _money_exceptions(as_of: date) -> tuple[list[ExceptionItem], list[MoneyTransaction]]:
    items = []
    open_money = list(_open_money(as_of))
    for money in open_money:
        exception_type = (
            ExceptionType.INFLOW_UNMATCHED
            if money.direction == MoneyDirection.INFLOW
            else ExceptionType.OUTFLOW_UNMATCHED
        )
        occurred_on = timezone.localtime(money.occurred_at).date()
        items.append(
            ExceptionItem(
                exception_type,
                money.id,
                occurred_on,
                money.counterparty.name if money.counterparty else "未识别单位",
                money.open_amount,
                "存在未核销资金",
            )
        )
        if money.counterparty_id is None:
            items.append(
                ExceptionItem(
                    ExceptionType.COUNTERPARTY_UNKNOWN,
                    money.id,
                    occurred_on,
                    "未识别单位",
                    money.open_amount,
                    "资金往来单位未完成映射",
                )
            )
    return items, open_money


def _mapping_issue_exceptions(as_of: date) -> list[ExceptionItem]:
    rows = StagedRow.objects.filter(
        batch__created_at__lt=_cutoff(as_of),
        issues__isnull=False,
    ).select_related("batch").order_by("batch_id", "row_number")
    items = []
    for row in rows:
        codes = {issue.get("code") for issue in row.issues if isinstance(issue, dict)}
        if codes & {"counterparty", "counterparty_ambiguous"}:
            items.append(
                ExceptionItem(
                    ExceptionType.COUNTERPARTY_UNKNOWN,
                    row.id,
                    None,
                    "未识别单位",
                    None,
                    f"导入批次 {str(row.batch_id)[:8]} 第 {row.row_number} 行单位待处理",
                )
            )
    return items


def _partial_reconciliation_exceptions(as_of: date) -> list[ExceptionItem]:
    cutoff = _cutoff(as_of)
    allocations = list(
        ReconciliationAllocation.objects.filter(
            reconciliation__created_at__lt=cutoff,
        ).filter(
            Q(reconciliation__reversal__isnull=True)
            | Q(reconciliation__reversal__created_at__gte=cutoff)
        ).select_related("reconciliation")
        .order_by("reconciliation_id", "id")
    )
    if not allocations:
        return []
    invoice_ids = {allocation.invoice_id for allocation in allocations}
    transaction_ids = {allocation.transaction_id for allocation in allocations}
    invoice_open = {
        item.id: item.open_amount
        for item in _with_historical_open_amount(
            Invoice.objects.filter(id__in=invoice_ids), "total_amount", as_of
        )
    }
    money_open = {
        item.id: item.open_amount
        for item in _with_historical_open_amount(
            MoneyTransaction.objects.filter(id__in=transaction_ids), "amount", as_of
        )
    }
    grouped: dict[UUID, list[ReconciliationAllocation]] = {}
    for allocation in allocations:
        grouped.setdefault(allocation.reconciliation_id, []).append(allocation)
    items = []
    for reconciliation_id, group in grouped.items():
        invoice_remaining = sum(
            (invoice_open[item_id] for item_id in {item.invoice_id for item in group}),
            MONEY_ZERO,
        )
        money_remaining = sum(
            (money_open[item_id] for item_id in {item.transaction_id for item in group}),
            MONEY_ZERO,
        )
        difference = abs(invoice_remaining - money_remaining)
        if difference <= MONEY_ZERO:
            continue
        items.append(
            ExceptionItem(
                ExceptionType.RECONCILIATION_DIFFERENCE,
                reconciliation_id,
                timezone.localtime(group[0].reconciliation.created_at).date(),
                "多笔往来",
                difference,
                "有效核销后票款仍有差额",
            )
        )
    return items


def _duplicate_exceptions(as_of: date) -> list[ExceptionItem]:
    cutoff = _cutoff(as_of)
    rows = list(
        StagedRow.objects.filter(
            is_duplicate=True,
            batch__created_at__lt=cutoff,
        ).order_by("batch_id", "row_number")
    )
    items = [
        ExceptionItem(
            ExceptionType.DUPLICATE_IMPORT,
            row.id,
            None,
            "导入资料",
            None,
            f"导入批次 {str(row.batch_id)[:8]} 第 {row.row_number} 行疑似重复",
        )
        for row in rows
    ]
    row_batch_ids = {row.batch_id for row in rows}
    items.extend(
        ExceptionItem(
            ExceptionType.DUPLICATE_IMPORT,
            batch.id,
            timezone.localtime(batch.created_at).date(),
            "导入资料",
            None,
            f"导入批次 {str(batch.id)[:8]} 有 {batch.duplicate_rows} 行疑似重复",
        )
        for batch in ImportBatch.objects.filter(
            duplicate_rows__gt=0,
            created_at__lt=cutoff,
        ).exclude(id__in=row_batch_ids).order_by("created_at", "id")
    )
    return items


def _red_allocation_exceptions(as_of: date) -> list[ExceptionItem]:
    invoices = _with_historical_open_amount(
        Invoice.objects.filter(
            status__in=[InvoiceStatus.RED, InvoiceStatus.VOID],
            issue_date__lte=as_of,
        ).select_related("counterparty"),
        "total_amount",
        as_of,
    ).filter(allocated_amount__gt=MONEY_ZERO).order_by("issue_date", "id")
    return [
        ExceptionItem(
            ExceptionType.RED_WITH_ACTIVE_ALLOCATION,
            invoice.id,
            invoice.issue_date,
            invoice.counterparty.name,
            invoice.allocated_amount,
            (
                "红冲发票仍存在有效核销"
                if invoice.status == InvoiceStatus.RED
                else "作废发票仍存在有效核销"
            ),
        )
        for invoice in invoices
    ]


def _stale_exceptions(
    as_of: date,
    invoice_items: list[ExceptionItem],
    open_money: list[MoneyTransaction],
) -> list[ExceptionItem]:
    items = []
    open_invoice_ids = {
        item.reference_id: item
        for item in invoice_items
        if item.type in {ExceptionType.RECEIVABLE_OPEN, ExceptionType.PAYABLE_OPEN}
    }
    invoices = Invoice.objects.filter(id__in=open_invoice_ids).select_related("counterparty")
    for invoice in invoices:
        base_date = invoice.due_date or invoice.issue_date
        if (as_of - base_date).days >= 91:
            open_item = open_invoice_ids[invoice.id]
            items.append(
                ExceptionItem(
                    ExceptionType.STALE_OPEN_ITEM,
                    invoice.id,
                    base_date,
                    invoice.counterparty.name,
                    open_item.amount,
                    "票据超过 90 天未核销",
                )
            )
    for money in open_money:
        occurred_on = timezone.localtime(money.occurred_at).date()
        if (as_of - occurred_on).days >= 91:
            items.append(
                ExceptionItem(
                    ExceptionType.STALE_OPEN_ITEM,
                    money.id,
                    occurred_on,
                    money.counterparty.name if money.counterparty else "未识别单位",
                    money.open_amount,
                    "资金超过 90 天未核销",
                )
            )
    return items


def _history_exceptions(as_of: date) -> list[ExceptionItem]:
    items = [
        ExceptionItem(
            ExceptionType.HISTORY_INCOMPLETE,
            period.id,
            period.expected_start,
            "历史资料",
            None,
            f"{period.year} 年 {period.get_source_kind_display()}资料{period.get_status_display()}",
        )
        for period in DataCoveragePeriod.objects.filter(
            year__lte=as_of.year,
            status__in=[CoverageStatus.PARTIAL, CoverageStatus.MISSING],
        ).order_by("year", "source_kind", "id")
    ]
    items.extend(
        ExceptionItem(
            ExceptionType.HISTORY_INCOMPLETE,
            batch.id,
            timezone.localtime(batch.created_at).date(),
            "导入资料",
            None,
            f"导入批次 {str(batch.id)[:8]} 部分完成",
        )
        for batch in ImportBatch.objects.filter(
            status=BatchStatus.PARTIAL,
            created_at__lt=_cutoff(as_of),
        ).order_by("created_at", "id")
    )
    return items


def exception_items(as_of: date) -> tuple[ExceptionItem, ...]:
    invoice_items = _open_invoice_exceptions(as_of)
    money_items, open_money = _money_exceptions(as_of)
    items = invoice_items + money_items
    items.extend(_mapping_issue_exceptions(as_of))
    items.extend(_partial_reconciliation_exceptions(as_of))
    items.extend(_duplicate_exceptions(as_of))
    items.extend(_red_allocation_exceptions(as_of))
    items.extend(_stale_exceptions(as_of, invoice_items, open_money))
    items.extend(_history_exceptions(as_of))
    type_order = {item: index for index, item in enumerate(ExceptionType)}
    return tuple(
        sorted(
            items,
            key=lambda item: (
                type_order[item.type],
                item.occurred_on or date.min,
                str(item.reference_id),
            ),
        )
    )
