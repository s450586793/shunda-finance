from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.accounts.decorators import owner_or_finance_required

from .models import Counterparty


def _querystring_without_page(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


@owner_or_finance_required
@require_GET
def counterparty_list(request):
    query = Counterparty.objects.order_by("name", "id")
    term = request.GET.get("query", "").strip()
    if term:
        query = query.filter(Q(name__icontains=term) | Q(tax_id__icontains=term))
    kind = request.GET.get("kind")
    if kind == "customer":
        query = query.filter(is_customer=True)
    elif kind == "supplier":
        query = query.filter(is_supplier=True)
    active = request.GET.get("active")
    if active == "true":
        query = query.filter(active=True)
    elif active == "false":
        query = query.filter(active=False)

    page = Paginator(query, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "parties/list.html",
        {"page": page, "querystring": _querystring_without_page(request)},
    )
