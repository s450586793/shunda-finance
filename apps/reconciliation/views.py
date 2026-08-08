import logging
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import DatabaseError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.decorators import finance_required
from apps.ledger.choices import InvoiceDirection, InvoiceStatus
from apps.ledger.models import Invoice, MoneyTransaction

from .candidates import available_transactions_for_invoice, transaction_candidates
from .choices import ReconciliationDirection
from .forms import (
    DirectReconciliationForm,
    ReversalForm,
    SettlementConfirmForm,
    SettlementDraftForm,
)
from .models import Reconciliation, SettlementBatch
from .presenters import (
    SettlementItem,
    candidate_to_dict,
    direct_allocation_rows,
    money_cents,
    money_text,
    settlement_context,
)
from .queries import invoice_open_amount
from .services import create_reconciliation, reverse_reconciliation
from .settlements import confirm_settlement_batch, create_settlement_batch

logger = logging.getLogger(__name__)
CONFLICT_MARKERS = (
    "可核销金额不足",
    "版本已过期",
    "不能重复确认",
    "已经撤销",
    "无可核销金额",
    "页面数据已过期",
    "已被其他核销占用",
)


def _validation_message(exc):
    return exc.messages[0] if exc.messages else "提交内容不合法"


def _validation_status(exc):
    message = _validation_message(exc)
    return 409 if any(marker in message for marker in CONFLICT_MARKERS) else 400


def _parse_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _parse_uuid(value):
    try:
        return UUID(value) if value else None
    except (AttributeError, TypeError, ValueError):
        return None


def _default_window(invoice):
    reference = invoice.issue_date if invoice else timezone.localdate()
    month_start = reference.replace(day=1)
    previous_month = (month_start - timedelta(days=1)).replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(days=1)
    return previous_month, month_end


def _workbench_context(request, *, form=None, selected_id=None, stale=False):
    invoice_items = []
    selected_invoice = None
    selected_open = Decimal("0.00")
    requested_id = selected_id or _parse_uuid(request.GET.get("invoice"))
    invoices = Invoice.objects.select_related("counterparty").filter(
        status=InvoiceStatus.NORMAL
    ).order_by("issue_date", "id")
    for invoice in invoices:
        open_amount = invoice_open_amount(invoice.id)
        if open_amount <= 0:
            continue
        item = SettlementItem(invoice, open_amount, money_text(open_amount), False)
        invoice_items.append(item)
        if invoice.id == requested_id:
            selected_invoice = invoice
            selected_open = open_amount

    default_start, default_end = _default_window(selected_invoice)
    start = _parse_date(request.GET.get("start")) or default_start
    end = _parse_date(request.GET.get("end")) or default_end
    if start > end:
        start, end = default_start, default_end

    transactions = []
    if selected_invoice:
        available = list(
            available_transactions_for_invoice(selected_invoice, start=start, end=end)
        )
        records = MoneyTransaction.objects.select_related(
            "account", "counterparty"
        ).in_bulk(item.id for item in available)
        transactions = [
            SettlementItem(
                records[item.id],
                item.open_amount,
                money_text(item.open_amount),
                False,
            )
            for item in available
        ]
    money_total = sum((item.open_amount for item in transactions), Decimal("0.00"))
    allocated = min(selected_open, money_total)
    allocation_rows = direct_allocation_rows(transactions, selected_open)
    return {
        "invoice_items": invoice_items,
        "selected_invoice": selected_invoice,
        "selected_open": money_text(selected_open),
        "selected_open_input": f"{selected_open:.2f}",
        "selected_open_cents": money_cents(selected_open),
        "transactions": allocation_rows,
        "money_total": money_text(money_total),
        "allocated_total": money_text(allocated),
        "difference": money_text(max(selected_open - allocated, Decimal("0.00"))),
        "difference_cents": money_cents(
            max(selected_open - allocated, Decimal("0.00"))
        ),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "form": form or DirectReconciliationForm(),
        "stale": stale,
    }


def _render_workbench(
    request, *, form=None, selected_id=None, status=200, stale=False
):
    return render(
        request,
        "reconciliation/workbench.html",
        _workbench_context(
            request,
            form=form,
            selected_id=selected_id,
            stale=stale,
        ),
        status=status,
    )


@finance_required
@require_GET
def workbench(request):
    return _render_workbench(request)


@finance_required
@require_GET
def candidate_list(request):
    try:
        invoice_id = UUID(request.GET["invoice"])
        start = date.fromisoformat(request.GET["start"])
        end = date.fromisoformat(request.GET["end"])
        if start > end:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"error": "候选查询参数不合法。"}, status=400)
    try:
        items = transaction_candidates(invoice_id, start=start, end=end)
    except Invoice.DoesNotExist:
        return JsonResponse({"error": "发票不存在。"}, status=404)
    except ValueError:
        return JsonResponse({"error": "候选查询参数不合法。"}, status=400)
    return JsonResponse({"items": [candidate_to_dict(item) for item in items]})


@finance_required
@require_POST
def direct_confirm(request):
    form = DirectReconciliationForm(request.POST)
    selected_id = _parse_uuid(request.POST.get("invoice_id"))
    if not form.is_valid():
        return _render_workbench(
            request, form=form, selected_id=selected_id, status=400
        )
    try:
        invoice = Invoice.objects.get(pk=form.cleaned_data["invoice_id"])
    except Invoice.DoesNotExist:
        form.add_error("invoice_id", "发票不存在")
        return _render_workbench(
            request, form=form, selected_id=selected_id, status=400
        )

    direction = (
        ReconciliationDirection.PURCHASE_PAYMENT
        if invoice.direction == InvoiceDirection.INPUT
        else ReconciliationDirection.SALES_RECEIPT
    )
    try:
        reconciliation = create_reconciliation(
            actor=request.user,
            direction=direction,
            allocations=form.allocation_inputs(),
            note=form.cleaned_data["note"],
            expected_invoice_open_amounts=form.expected_invoice_open_amounts(),
            expected_transaction_open_amounts=(
                form.expected_transaction_open_amounts()
            ),
            allow_partial=form.cleaned_data["partial_confirm"],
        )
    except ValidationError as exc:
        message = _validation_message(exc)
        form.add_error(None, message)
        return _render_workbench(
            request,
            form=form,
            selected_id=invoice.id,
            status=_validation_status(exc),
            stale="页面数据已过期" in message,
        )
    except DatabaseError:
        logger.exception("Direct reconciliation failed")
        form.add_error(None, "核销提交冲突，请刷新后重试。")
        return _render_workbench(
            request,
            form=form,
            selected_id=invoice.id,
            status=409,
            stale=True,
        )
    return redirect("reconciliation:detail", pk=reconciliation.pk)


def _settlement_list_context(form=None):
    return {
        "form": form or SettlementDraftForm(),
        "batches": SettlementBatch.objects.select_related(
            "counterparty", "created_by"
        ).order_by("-id"),
    }


@finance_required
@require_GET
def settlement_list(request):
    return render(
        request, "reconciliation/settlement_list.html", _settlement_list_context()
    )


@finance_required
@require_POST
def settlement_create(request):
    form = SettlementDraftForm(request.POST)
    if form.is_valid():
        try:
            batch = create_settlement_batch(
                actor=request.user,
                counterparty_id=form.cleaned_data["counterparty"].id,
                direction=form.cleaned_data["direction"],
                period_start=form.cleaned_data["period_start"],
                period_end=form.cleaned_data["period_end"],
            )
        except ValidationError as exc:
            form.add_error(None, _validation_message(exc))
        except DatabaseError:
            logger.exception("Settlement draft creation failed")
            form.add_error(None, "结算草稿创建失败，请稍后重试。")
        else:
            return redirect("reconciliation:settlement-detail", pk=batch.pk)
    return render(
        request,
        "reconciliation/settlement_list.html",
        _settlement_list_context(form),
        status=400,
    )


def _settlement_detail_context(batch, form=None):
    context = settlement_context(batch)
    context.update(
        {
            "batch": batch,
            "form": form or SettlementConfirmForm(initial={"version": batch.version}),
        }
    )
    return context


@finance_required
@require_GET
def settlement_detail(request, pk):
    batch = get_object_or_404(
        SettlementBatch.objects.select_related("counterparty", "created_by"), pk=pk
    )
    return render(
        request,
        "reconciliation/settlement_detail.html",
        _settlement_detail_context(batch),
    )


@finance_required
@require_POST
def settlement_confirm(request, pk):
    batch = get_object_or_404(
        SettlementBatch.objects.select_related("counterparty", "created_by"), pk=pk
    )
    form = SettlementConfirmForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "reconciliation/settlement_detail.html",
            _settlement_detail_context(batch, form),
            status=400,
        )
    try:
        reconciliation = confirm_settlement_batch(
            batch.id,
            request.user,
            form.allocation_inputs(),
            version=form.cleaned_data["version"],
        )
    except ValidationError as exc:
        form.add_error(None, _validation_message(exc))
        batch.refresh_from_db()
        return render(
            request,
            "reconciliation/settlement_detail.html",
            _settlement_detail_context(batch, form),
            status=_validation_status(exc),
        )
    except DatabaseError:
        logger.exception("Settlement confirmation failed", extra={"batch_id": str(pk)})
        form.add_error(None, "结算确认冲突，请刷新后重试。")
        batch.refresh_from_db()
        return render(
            request,
            "reconciliation/settlement_detail.html",
            _settlement_detail_context(batch, form),
            status=409,
        )
    return redirect("reconciliation:detail", pk=reconciliation.pk)


@finance_required
@require_GET
def reconciliation_detail(request, pk):
    reconciliation = get_object_or_404(
        Reconciliation.objects.select_related(
            "created_by", "settlement_batch", "reversal__reversed_by"
        ).prefetch_related(
            "allocations__invoice__counterparty",
            "allocations__transaction__account",
        ),
        pk=pk,
    )
    return render(
        request,
        "reconciliation/detail.html",
        {
            "reconciliation": reconciliation,
            "allocation_rows": [
                {"record": item, "amount_text": money_text(item.amount)}
                for item in reconciliation.allocations.all()
            ],
        },
    )


def _render_reversal(request, original, form, status=200):
    return render(
        request,
        "reconciliation/reversal_form.html",
        {
            "original": original,
            "form": form,
            "allocation_rows": [
                {"record": item, "amount_text": money_text(item.amount)}
                for item in original.allocations.all()
            ],
        },
        status=status,
    )


@finance_required
@require_http_methods(["GET", "POST"])
def reverse(request, pk):
    original = get_object_or_404(
        Reconciliation.objects.select_related("created_by").prefetch_related(
            "allocations__invoice", "allocations__transaction"
        ),
        pk=pk,
    )
    form = ReversalForm(request.POST or None)
    if request.method == "GET":
        return _render_reversal(request, original, form)
    if not form.is_valid():
        return _render_reversal(request, original, form, status=400)
    try:
        reverse_reconciliation(
            actor=request.user,
            reconciliation_id=original.id,
            reason=form.cleaned_data["reason"],
        )
    except ValidationError as exc:
        form.add_error(None, _validation_message(exc))
        return _render_reversal(
            request, original, form, status=_validation_status(exc)
        )
    except (DatabaseError, ObjectDoesNotExist):
        logger.exception("Reconciliation reversal failed", extra={"id": str(pk)})
        form.add_error(None, "核销撤销冲突，请刷新后重试。")
        return _render_reversal(request, original, form, status=409)
    return redirect("reconciliation:detail", pk=original.pk)
