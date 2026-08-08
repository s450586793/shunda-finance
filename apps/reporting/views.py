import json
from datetime import date, timedelta
from decimal import Decimal

from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.accounts.decorators import owner_or_finance_required
from apps.parties.models import Counterparty

from .dashboard import dashboard_payload
from .queries import (
    exception_items,
    payables_as_of,
    receivables_as_of,
    supplier_coverage_as_of,
    supplier_ledger_as_of,
    supplier_summaries_as_of,
    supplier_summary_as_of,
    supplier_summary_totals,
)


def _parse_as_of(value: str | None) -> date | None:
    if not value:
        return timezone.localdate()
    try:
        parsed = date.fromisoformat(value)
        parsed + timedelta(days=1)
        return parsed
    except (OverflowError, TypeError, ValueError):
        return None


def _parse_month(value: str | None) -> date | None:
    if not value:
        today = timezone.localdate()
        return today.replace(day=1)
    try:
        return date.fromisoformat(f"{value}-01")
    except (TypeError, ValueError):
        return None


def _money_text(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


@owner_or_finance_required
@require_GET
def dashboard(request):
    month = _parse_month(request.GET.get("month"))
    if month is None:
        return HttpResponseBadRequest("月份参数不合法")
    payload = dashboard_payload(month)
    chart_data = {
        "dailyCashflow": [
            {
                "date": item.date.isoformat(),
                "inflow": _money_text(item.inflow),
                "outflow": _money_text(item.outflow),
            }
            for item in payload.daily_cashflow
        ],
        "receivableAging": [
            {"label": item.label, "amount": _money_text(item.amount)}
            for item in payload.receivable_aging
        ],
        "payableDue": [
            {"label": item.label, "amount": _money_text(item.amount)}
            for item in payload.payable_due_buckets
        ],
    }
    return render(
        request,
        "reporting/dashboard.html",
        {
            "payload": payload,
            "month": month.strftime("%Y-%m"),
            "dashboard_chart_data": chart_data,
            "dashboard_chart_json": json.dumps(chart_data, ensure_ascii=True),
            "exception_counts": [
                (label, count)
                for label, count in payload.exception_counts.items()
                if count
            ],
        },
    )


def _open_invoice_view(request, *, payable: bool):
    as_of = _parse_as_of(request.GET.get("as_of"))
    if as_of is None:
        return HttpResponseBadRequest("日期参数不合法")
    rows = payables_as_of(as_of) if payable else receivables_as_of(as_of)
    return render(
        request,
        "reporting/payables.html" if payable else "reporting/receivables.html",
        {
            "rows": rows,
            "as_of": as_of,
            "total": sum((row.open_amount for row in rows), Decimal("0.00")),
        },
    )


@owner_or_finance_required
@require_GET
def receivables(request):
    return _open_invoice_view(request, payable=False)


@owner_or_finance_required
@require_GET
def payables(request):
    return _open_invoice_view(request, payable=True)


@owner_or_finance_required
@require_GET
def exceptions(request):
    as_of = _parse_as_of(request.GET.get("as_of"))
    if as_of is None:
        return HttpResponseBadRequest("日期参数不合法")
    return render(
        request,
        "reporting/exceptions.html",
        {"items": exception_items(as_of), "as_of": as_of},
    )


@owner_or_finance_required
@require_GET
def suppliers(request):
    as_of = _parse_as_of(request.GET.get("as_of"))
    if as_of is None:
        return HttpResponseBadRequest("日期参数不合法")
    search = request.GET.get("q", "").strip()[:100]
    rows = supplier_summaries_as_of(as_of, search=search)
    return render(
        request,
        "reporting/suppliers.html",
        {
            "rows": rows,
            "as_of": as_of,
            "search": search,
            "coverage": supplier_coverage_as_of(as_of),
            "totals": supplier_summary_totals(rows),
        },
    )


@owner_or_finance_required
@require_GET
def supplier_detail(request, pk):
    as_of = _parse_as_of(request.GET.get("as_of"))
    if as_of is None:
        return HttpResponseBadRequest("日期参数不合法")
    supplier = get_object_or_404(Counterparty, pk=pk, is_supplier=True)
    rows = supplier_ledger_as_of(supplier.id, as_of)
    return render(
        request,
        "reporting/supplier_detail.html",
        {
            "supplier": supplier,
            "as_of": as_of,
            "rows": rows,
            "summary": supplier_summary_as_of(supplier.id, as_of),
            "coverage": supplier_coverage_as_of(as_of),
        },
    )
