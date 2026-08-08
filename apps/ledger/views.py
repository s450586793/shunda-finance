from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.core.paginator import Paginator
from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.accounts.decorators import owner_or_finance_required
from apps.parties.models import Counterparty

from .choices import InvoiceDirection, InvoiceStatus, MoneyChannel, MoneyDirection
from .models import Invoice, MoneyTransaction

MONEY_FIELD = DecimalField(max_digits=18, decimal_places=2)
MAX_MONEY = Decimal("9999999999999999.99")
RECONCILIATION_STATES = (
    ("unreconciled", "未核销"),
    ("partial", "部分核销"),
    ("reconciled", "已核销"),
)
OPEN_AMOUNT_STATES = (("open", "有未核金额"), ("closed", "已核清"))


def _with_open_amount(query, total_field):
    query = query.annotate(
        allocated_amount=Coalesce(
            Sum(
                "reconciliationallocation__amount",
                filter=Q(
                    reconciliationallocation__reconciliation__reversal__isnull=True
                ),
            ),
            Decimal("0.00"),
            output_field=MONEY_FIELD,
        )
    )
    return query.annotate(
        open_amount=ExpressionWrapper(
            F(total_field) - F("allocated_amount"), output_field=MONEY_FIELD
        )
    )


def _valid_uuid(value):
    try:
        return UUID(value) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


def _valid_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _valid_money(value):
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if (
        not amount.is_finite()
        or amount < 0
        or amount > MAX_MONEY
        or amount.as_tuple().exponent < -2
    ):
        return None
    return amount


def _querystring_without_page(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


def _counterparty_choices():
    return Counterparty.objects.order_by("-active", "name")


@owner_or_finance_required
@require_GET
def invoice_list(request):
    query = _with_open_amount(
        Invoice.objects.select_related("counterparty", "import_batch"),
        "total_amount",
    ).order_by("-issue_date", "-id")

    direction = request.GET.get("direction")
    if direction in InvoiceDirection.values:
        query = query.filter(direction=direction)
    counterparty_id = _valid_uuid(request.GET.get("counterparty"))
    if counterparty_id:
        query = query.filter(counterparty_id=counterparty_id)
    issue_start = _valid_date(request.GET.get("issue_start"))
    if issue_start:
        query = query.filter(issue_date__gte=issue_start)
    issue_end = _valid_date(request.GET.get("issue_end"))
    if issue_end:
        query = query.filter(issue_date__lte=issue_end)
    status = request.GET.get("status")
    if status in InvoiceStatus.values:
        query = query.filter(status=status)
    state = request.GET.get("reconciliation_state")
    if state == "unreconciled":
        query = query.filter(allocated_amount=Decimal("0.00"))
    elif state == "partial":
        query = query.filter(
            allocated_amount__gt=Decimal("0.00"), open_amount__gt=Decimal("0.00")
        )
    elif state == "reconciled":
        query = query.filter(open_amount=Decimal("0.00"))
    invoice_number = request.GET.get("invoice_number", "").strip()
    if invoice_number:
        query = query.filter(invoice_number__icontains=invoice_number)

    page = Paginator(query, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "ledger/invoice_list.html",
        {
            "page": page,
            "querystring": _querystring_without_page(request),
            "counterparties": _counterparty_choices(),
            "directions": InvoiceDirection.choices,
            "statuses": InvoiceStatus.choices,
            "reconciliation_states": RECONCILIATION_STATES,
        },
    )


@owner_or_finance_required
@require_GET
def transaction_list(request):
    query = _with_open_amount(
        MoneyTransaction.objects.select_related(
            "account", "counterparty", "import_batch"
        ),
        "amount",
    ).order_by("-occurred_at", "-id")

    channel = request.GET.get("channel")
    if channel in MoneyChannel.values:
        query = query.filter(channel=channel)
    direction = request.GET.get("direction")
    if direction in MoneyDirection.values:
        query = query.filter(direction=direction)
    counterparty_id = _valid_uuid(request.GET.get("counterparty"))
    if counterparty_id:
        query = query.filter(counterparty_id=counterparty_id)
    date_start = _valid_date(request.GET.get("date_start"))
    if date_start:
        query = query.filter(occurred_at__date__gte=date_start)
    date_end = _valid_date(request.GET.get("date_end"))
    if date_end:
        query = query.filter(occurred_at__date__lte=date_end)
    open_amount = request.GET.get("open_amount", "").strip()
    if open_amount == "open":
        query = query.filter(open_amount__gt=Decimal("0.00"))
    elif open_amount == "closed":
        query = query.filter(open_amount=Decimal("0.00"))
    elif open_amount:
        amount = _valid_money(open_amount)
        if amount is not None:
            query = query.filter(open_amount=amount)
    transaction_id = request.GET.get("transaction_id", "").strip()
    if transaction_id:
        query = query.filter(transaction_id__icontains=transaction_id)

    page = Paginator(query, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "ledger/transaction_list.html",
        {
            "page": page,
            "querystring": _querystring_without_page(request),
            "counterparties": _counterparty_choices(),
            "channels": MoneyChannel.choices,
            "directions": MoneyDirection.choices,
            "open_amount_states": OPEN_AMOUNT_STATES,
        },
    )
