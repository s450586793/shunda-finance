from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from django.db.models import OuterRef, Subquery, Sum
from django.db.models.functions import TruncDate

from apps.imports.choices import BatchStatus
from apps.imports.models import CoverageStatus, DataCoveragePeriod, ImportBatch
from apps.ledger.choices import MoneyDirection
from apps.ledger.models import AccountBalanceSnapshot, FundingAccount, MoneyTransaction

from .queries import (
    AGING_LABELS,
    ExceptionType,
    _cutoff,
    exception_items,
    payables_as_of,
    receivables_as_of,
)

MONEY_ZERO = Decimal("0.00")
DUE_LABELS = ("已逾期", "7日内", "8-30日", "30日以上")


@dataclass(frozen=True)
class DailyCashflow:
    date: date
    inflow: Decimal
    outflow: Decimal


@dataclass(frozen=True)
class AgingBucket:
    label: str
    amount: Decimal


@dataclass(frozen=True)
class DueBucket:
    label: str
    amount: Decimal


@dataclass(frozen=True)
class DashboardPayload:
    current_funds: Decimal | None
    month_inflow: Decimal
    month_outflow: Decimal
    receivables: Decimal
    overdue_receivables: Decimal
    payables: Decimal
    due_within_7_days: Decimal
    daily_cashflow: tuple[DailyCashflow, ...]
    receivable_aging: tuple[AgingBucket, ...]
    payable_due_buckets: tuple[DueBucket, ...]
    exception_counts: dict[str, int]
    data_incomplete: bool
    incomplete_batch_ids: tuple[UUID, ...]


def _month_bounds(month: date) -> tuple[date, date]:
    start = month.replace(day=1)
    return start, start.replace(day=monthrange(start.year, start.month)[1])


def _cashflow(start: date, end: date):
    rows = (
        MoneyTransaction.objects.filter(
            occurred_at__gte=_cutoff(start - timedelta(days=1)),
            occurred_at__lt=_cutoff(end),
        )
        .annotate(day=TruncDate("occurred_at"))
        .values("day", "direction")
        .annotate(total=Sum("amount"))
        .order_by("day", "direction")
    )
    totals = {(row["day"], row["direction"]): row["total"] for row in rows}
    daily = []
    day = start
    while day <= end:
        daily.append(
            DailyCashflow(
                day,
                totals.get((day, MoneyDirection.INFLOW), MONEY_ZERO),
                totals.get((day, MoneyDirection.OUTFLOW), MONEY_ZERO),
            )
        )
        day += timedelta(days=1)
    return tuple(daily)


def _current_funds(as_of: date) -> tuple[Decimal | None, bool]:
    latest = AccountBalanceSnapshot.objects.filter(
        account_id=OuterRef("pk"),
        as_of__lt=_cutoff(as_of),
    ).order_by("-as_of", "-id")
    balances = list(
        FundingAccount.objects.filter(active=True)
        .annotate(latest_balance=Subquery(latest.values("balance")[:1]))
        .values_list("latest_balance", flat=True)
    )
    known = [balance for balance in balances if balance is not None]
    current = sum(known, MONEY_ZERO) if known else None
    return current, any(balance is None for balance in balances)


def _due_label(due_date: date | None, issue_date: date, as_of: date) -> str:
    days = ((due_date or issue_date) - as_of).days
    if days < 0:
        return DUE_LABELS[0]
    if days <= 7:
        return DUE_LABELS[1]
    if days <= 30:
        return DUE_LABELS[2]
    return DUE_LABELS[3]


def dashboard_payload(month: date) -> DashboardPayload:
    start, as_of = _month_bounds(month)
    daily_cashflow = _cashflow(start, as_of)
    receivable_rows = receivables_as_of(as_of)
    payable_rows = payables_as_of(as_of)
    receivable_totals = {label: MONEY_ZERO for label in AGING_LABELS}
    for row in receivable_rows:
        receivable_totals[row.aging_bucket] += row.open_amount
    due_totals = {label: MONEY_ZERO for label in DUE_LABELS}
    for row in payable_rows:
        due_totals[_due_label(row.due_date, row.issue_date, as_of)] += row.open_amount

    exceptions = exception_items(as_of)
    exception_counts = {item.value: 0 for item in ExceptionType}
    for item in exceptions:
        exception_counts[item.type.value] += 1
    incomplete_batch_ids = tuple(
        ImportBatch.objects.filter(
            status=BatchStatus.PARTIAL,
            created_at__lt=_cutoff(as_of),
        ).order_by("created_at", "id").values_list("id", flat=True)
    )
    coverage_incomplete = DataCoveragePeriod.objects.filter(
        year__lte=as_of.year,
        status__in=[CoverageStatus.PARTIAL, CoverageStatus.MISSING],
    ).exists()
    current_funds, account_incomplete = _current_funds(as_of)
    return DashboardPayload(
        current_funds=current_funds,
        month_inflow=sum((item.inflow for item in daily_cashflow), MONEY_ZERO),
        month_outflow=sum((item.outflow for item in daily_cashflow), MONEY_ZERO),
        receivables=sum((item.open_amount for item in receivable_rows), MONEY_ZERO),
        overdue_receivables=sum(
            (
                item.open_amount
                for item in receivable_rows
                if (item.due_date or item.issue_date) < as_of
            ),
            MONEY_ZERO,
        ),
        payables=sum((item.open_amount for item in payable_rows), MONEY_ZERO),
        due_within_7_days=due_totals[DUE_LABELS[1]],
        daily_cashflow=daily_cashflow,
        receivable_aging=tuple(
            AgingBucket(label, receivable_totals[label]) for label in AGING_LABELS
        ),
        payable_due_buckets=tuple(
            DueBucket(label, due_totals[label]) for label in DUE_LABELS
        ),
        exception_counts=exception_counts,
        data_incomplete=bool(
            incomplete_batch_ids or coverage_incomplete or account_incomplete
        ),
        incomplete_batch_ids=incomplete_batch_ids,
    )
