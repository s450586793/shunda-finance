# 顺达供应商对账模块 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有逐笔进项发票、银行/微信付款和人工核销之上，交付供应商汇总页与可追溯的往来明细页。

**Architecture:** 扩展 Django `reporting` 模块，不增加数据库表。查询层以截止日期重建当时有效的发票、付款和核销状态，返回不可变展示对象；Web 层负责筛选、权限和模板呈现，未付发票继续跳转现有人工核销台处理。

**Tech Stack:** Python 3.12、Django 5.2、PostgreSQL 16、Django Templates、原生 CSS、pytest、Docker Compose。

## Global Constraints

- 只使用已确认导入的逐笔进项发票和实际资金支出，不生成或推断人工期初余额。
- 往来余额等于累计正常进项发票减累计供应商付款。
- 未付发票与未匹配付款分别计算，系统不得自动凑平或自动确认核销。
- 作废、红冲发票不进入当前应付；已撤销核销恢复票款双方未核销金额。
- 财务和老板可以查看；只有财务可以进入现有人工核销台。
- 原始资金账户继续脱敏，老板不能下载财务专属的原始导入文件。
- 第一版不导入供应商外部账单、不生成付款申请、不增加人工调整项。
- 不增加第三方依赖，不新增数据库迁移。

## File Structure

- Modify `apps/reporting/queries.py`: 供应商汇总、逐笔时间线、历史核销金额和资料覆盖查询。
- Modify `apps/reporting/views.py`: 参数校验、权限保护、汇总和明细上下文。
- Modify `apps/reporting/urls.py`: 供应商汇总和明细路由。
- Create `templates/reporting/suppliers.html`: 供应商汇总页。
- Create `templates/reporting/supplier_detail.html`: 供应商逐笔往来页。
- Modify `templates/reporting/receivables.html`: 增加供应商页签。
- Modify `templates/reporting/payables.html`: 增加供应商页签。
- Modify `templates/reporting/exceptions.html`: 增加供应商页签。
- Modify `static/css/app.css`: 稳定汇总与明细表格尺寸和窄屏布局。
- Create `tests/reporting/test_supplier_queries.py`: 查询口径、历史时点和覆盖状态测试。
- Create `tests/web/test_supplier_reporting_views.py`: 权限、参数、模板和核销入口测试。
- Modify `tests/web/test_reporting_views.py`: 把供应商汇总纳入通用报表权限覆盖。

---

### Task 1: 供应商汇总查询

**Files:**
- Modify: `apps/reporting/queries.py`
- Create: `tests/reporting/test_supplier_queries.py`

**Interfaces:**
- Consumes: `_cutoff(as_of)`, `_with_historical_open_amount(...)`, `Invoice`, `MoneyTransaction`, `Counterparty`。
- Produces: `SupplierSummaryRow`, `SupplierSummaryTotals`, `supplier_summaries_as_of(as_of: date, search: str = "") -> tuple[SupplierSummaryRow, ...]`, `supplier_summary_as_of(counterparty_id: UUID, as_of: date) -> SupplierSummaryRow`, `supplier_summary_totals(rows: tuple[SupplierSummaryRow, ...]) -> SupplierSummaryTotals`。

- [ ] **Step 1: Write failing summary tests**

Create `tests/reporting/test_supplier_queries.py` with focused fixtures and these first assertions:

```python
from datetime import date
from decimal import Decimal

import pytest

from apps.ledger.choices import InvoiceDirection, InvoiceStatus, MoneyDirection
from apps.parties.models import Counterparty
from apps.reporting.queries import supplier_summaries_as_of
from tests.builders import make_invoice, make_transaction


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
    assert rows[0].invoiced_amount == Decimal("17600.00")
    assert rows[0].paid_amount == Decimal("6600.00")
    assert rows[0].balance == Decimal("11000.00")
    assert rows[0].invoice_open_amount == Decimal("17600.00")
    assert rows[0].payment_open_amount == Decimal("6600.00")


@pytest.mark.django_db
def test_supplier_summaries_exclude_future_non_normal_and_wrong_direction(finance_user):
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

    row = supplier_summaries_as_of(date(2026, 7, 31))[0]

    assert row.invoiced_amount == valid.total_amount
    assert row.paid_amount == Decimal("0.00")


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
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
.venv/bin/pytest tests/reporting/test_supplier_queries.py -q
```

Expected: collection fails because `supplier_summaries_as_of` does not exist.

- [ ] **Step 3: Add immutable summary type and batched queries**

Add to `apps/reporting/queries.py`:

```python
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


def _supplier_invoice_records(as_of: date, search: str = ""):
    rows = _with_historical_open_amount(
        Invoice.objects.filter(
            direction=InvoiceDirection.INPUT,
            status=InvoiceStatus.NORMAL,
            issue_date__lte=as_of,
            counterparty__is_supplier=True,
        ).select_related("counterparty"),
        "total_amount",
        as_of,
    )
    if search:
        rows = rows.filter(counterparty__name__icontains=search)
    return rows.order_by("issue_date", "id")


def _supplier_payment_records(as_of: date, search: str = ""):
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
    return rows.order_by("occurred_at", "id")
```

Implement `supplier_summaries_as_of` by aggregating the two evaluated querysets into a dictionary keyed by `counterparty_id`. Start all money fields at `MONEY_ZERO`, update latest activity using invoice date or the local payment date, then return rows sorted by `counterparty_name` and `counterparty_id`. Do not query per supplier.

Implement `supplier_summary_as_of` with the same filtered query helpers for one supplier. Return a zero-valued row with `latest_activity_on=None` when the supplier exists but has no activity before the cutoff. Implement `supplier_summary_totals` as a pure function over returned rows so views and templates never recalculate financial values.

- [ ] **Step 4: Add historical allocation and query-count assertions**

Extend the same test file:

```python
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.reconciliation.choices import ReconciliationDirection
from apps.reconciliation.services import AllocationInput, create_reconciliation


@pytest.mark.django_db
def test_supplier_summary_separates_open_invoice_and_open_payment(finance_user):
    invoice = make_invoice(finance_user, total_amount=Decimal("1000.00"))
    payment = make_transaction(
        finance_user,
        counterparty=invoice.counterparty,
        amount=Decimal("700.00"),
    )
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(invoice.id, payment.id, Decimal("600.00"))],
        allow_partial=True,
    )

    row = supplier_summaries_as_of(date(2026, 7, 31))[0]

    assert row.invoice_open_amount == Decimal("400.00")
    assert row.payment_open_amount == Decimal("100.00")
    assert row.balance == Decimal("300.00")


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
```

- [ ] **Step 5: Run focused tests to verify GREEN**

Run:

```bash
.venv/bin/pytest tests/reporting/test_supplier_queries.py -q
```

Expected: all summary tests pass.

- [ ] **Step 6: Commit summary query slice**

```bash
git add apps/reporting/queries.py tests/reporting/test_supplier_queries.py
git commit -m "feat: 增加供应商汇总查询"
```

---

### Task 2: 供应商逐笔明细与资料覆盖状态

**Files:**
- Modify: `apps/reporting/queries.py`
- Modify: `tests/reporting/test_supplier_queries.py`

**Interfaces:**
- Consumes: Task 1 的 `_supplier_invoice_records`, `_supplier_payment_records`, `SupplierSummaryRow`。
- Produces: `SupplierLedgerKind`, `SupplierLedgerRow`, `SupplierCoverage`, `supplier_ledger_as_of(counterparty_id: UUID, as_of: date)`, `supplier_coverage_as_of(as_of: date)`。

- [ ] **Step 1: Write failing ledger tests**

Append tests that require stable mixed ordering, rolling balances and open amounts:

```python
from datetime import UTC, datetime

from apps.ledger.choices import MoneyChannel
from apps.reporting.queries import SupplierLedgerKind, supplier_ledger_as_of


@pytest.mark.django_db
def test_supplier_ledger_mixes_invoices_and_payments_with_running_balance(finance_user):
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
    assert rows[0].running_balance == Decimal("8800.00")
    assert rows[1].decrease == Decimal("6500.00")
    assert rows[1].running_balance == Decimal("2300.00")
    assert rows[1].channel == MoneyChannel.BANK


@pytest.mark.django_db
def test_supplier_ledger_orders_invoice_before_payment_on_same_day(finance_user):
    invoice = make_invoice(finance_user)
    payment = make_transaction(
        finance_user,
        counterparty=invoice.counterparty,
    )

    rows = supplier_ledger_as_of(invoice.counterparty_id, date(2026, 7, 31))

    assert [row.kind for row in rows] == [
        SupplierLedgerKind.INVOICE,
        SupplierLedgerKind.PAYMENT,
    ]
```

- [ ] **Step 2: Run ledger tests to verify RED**

Run:

```bash
.venv/bin/pytest tests/reporting/test_supplier_queries.py -q
```

Expected: fails because ledger types and `supplier_ledger_as_of` do not exist.

- [ ] **Step 3: Implement ledger row construction**

Add these types to `apps/reporting/queries.py`:

```python
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
```

Implement `supplier_ledger_as_of` using the Task 1 query helpers filtered by `counterparty_id`. Prefetch `import_batch__source_files`; create invoice rows with `increase=total_amount`, payment rows with `decrease=amount`, and calculate `allocated_amount = total - open_amount`. Sort by `(occurred_on, invoice_before_payment, stable_reference_id)`, then use `dataclasses.replace` to populate running balances without mutating frozen rows.

- [ ] **Step 4: Write failing coverage tests**

Append:

```python
from apps.imports.choices import SourceKind
from apps.imports.models import CoverageStatus, DataCoveragePeriod
from apps.reporting.queries import supplier_coverage_as_of


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
    assert supplier_coverage_as_of(date(2026, 7, 31)).code == "unregistered"
    _coverage(2026, SourceKind.INPUT_INVOICE, CoverageStatus.PARTIAL)
    assert supplier_coverage_as_of(date(2026, 7, 31)).code == "incomplete"
```

- [ ] **Step 5: Implement coverage evaluation**

Add:

```python
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
```

`supplier_coverage_as_of` must:

1. Load required-source coverage rows whose `expected_start <= as_of`.
2. Return `unregistered / 完整性未登记` when no rows exist.
3. Return `incomplete / 资料不完整` when any explicit row is `partial` or `missing`.
4. Determine the earliest registered expected year and require every source for every year through `as_of.year`; missing combinations return `unregistered`.
5. Return `full / 资料完整` only when every required combination exists and is `full`.
6. Build `detail` from source/year coverage facts only; never include raw uploaded row payloads.

- [ ] **Step 6: Add historical reversal and source tracing tests**

Use the existing reporting test pattern that updates reconciliation and reversal timestamps. Assert that a ledger row's `open_amount` changes at the correct historical cutoff and that rows with a stored `SourceFile` expose its id while rows without one return `None`.

- [ ] **Step 7: Run focused tests and existing reporting regression**

Run:

```bash
.venv/bin/pytest tests/reporting/test_supplier_queries.py tests/reporting/test_queries.py -q
```

Expected: all supplier and existing reporting query tests pass.

- [ ] **Step 8: Commit ledger query slice**

```bash
git add apps/reporting/queries.py tests/reporting/test_supplier_queries.py
git commit -m "feat: 增加供应商逐笔往来查询"
```

---

### Task 3: 供应商汇总与明细 Web 页面

**Files:**
- Modify: `apps/reporting/views.py`
- Modify: `apps/reporting/urls.py`
- Create: `templates/reporting/suppliers.html`
- Create: `templates/reporting/supplier_detail.html`
- Modify: `templates/reporting/receivables.html`
- Modify: `templates/reporting/payables.html`
- Modify: `templates/reporting/exceptions.html`
- Modify: `static/css/app.css`
- Create: `tests/web/test_supplier_reporting_views.py`
- Modify: `tests/web/test_reporting_views.py`

**Interfaces:**
- Consumes: Task 1 and 2 query functions and data classes; existing `owner_or_finance_required`; existing `/reconciliation/?invoice=<uuid>` behavior.
- Produces: routes `reporting:suppliers` and `reporting:supplier-detail`.

- [ ] **Step 1: Write failing route, permission and parameter tests**

Create `tests/web/test_supplier_reporting_views.py`:

```python
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.imports.models import SourceFile

from apps.parties.models import Counterparty
from tests.builders import make_invoice, make_transaction


@pytest.mark.django_db
def test_supplier_pages_require_reporting_role(client, finance_client, owner_client):
    supplier = Counterparty.objects.create(
        name="页面供应商",
        normalized_name="页面供应商",
        is_supplier=True,
    )
    summary_url = reverse("reporting:suppliers")
    detail_url = reverse("reporting:supplier-detail", args=[supplier.id])

    assert client.get(summary_url).status_code == 302
    assert client.get(detail_url).status_code == 302
    assert finance_client.get(summary_url).status_code == 200
    assert owner_client.get(summary_url).status_code == 200
    assert finance_client.get(detail_url).status_code == 200
    assert owner_client.get(detail_url).status_code == 200

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

    assert owner_client.get(reverse("reporting:suppliers"), {"as_of": "bad"}).status_code == 400
    assert owner_client.get(
        reverse("reporting:supplier-detail", args=[customer.id])
    ).status_code == 404
```

- [ ] **Step 2: Run Web tests to verify RED**

Run:

```bash
.venv/bin/pytest tests/web/test_supplier_reporting_views.py -q
```

Expected: URL reversal fails because supplier routes do not exist.

- [ ] **Step 3: Add routes and read-only views**

Add to `apps/reporting/urls.py`:

```python
path("suppliers/", views.suppliers, name="suppliers"),
path("suppliers/<uuid:pk>/", views.supplier_detail, name="supplier-detail"),
```

Add to `apps/reporting/views.py`:

```python
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
```

Use the tested `supplier_summary_totals(rows)` and `supplier_summary_as_of(counterparty_id, as_of)` interfaces from Task 1; do not calculate financial values in views or templates.

- [ ] **Step 4: Write failing template-behavior tests**

Append:

```python
@pytest.mark.django_db
def test_supplier_summary_search_and_detail_amounts(finance_client, finance_user):
    invoice = make_invoice(
        finance_user,
        total_amount=Decimal("1000.00"),
        invoice_number="SUPPLIER-INV-1",
    )
    make_transaction(
        finance_user,
        counterparty=invoice.counterparty,
        amount=Decimal("400.00"),
    )

    response = finance_client.get(
        reverse("reporting:suppliers"),
        {"q": invoice.counterparty.name, "as_of": "2026-07-31"},
    )
    detail = finance_client.get(
        reverse("reporting:supplier-detail", args=[invoice.counterparty_id]),
        {"as_of": "2026-07-31"},
    )

    assert response.status_code == 200
    assert invoice.counterparty.name in response.content.decode()
    assert response.context["search"] == invoice.counterparty.name
    assert detail.status_code == 200
    assert "1000.00" in detail.content.decode()
    assert "400.00" in detail.content.decode()


@pytest.mark.django_db
def test_only_finance_sees_reconciliation_and_source_actions(
    finance_client,
    owner_client,
    finance_user,
):
    invoice = make_invoice(finance_user, invoice_number="ACTION-INVOICE")
    SourceFile.objects.create(
        batch=invoice.import_batch,
        file=SimpleUploadedFile("source.xlsx", b"source"),
        original_name="source.xlsx",
        sha256="a" * 64,
        size=6,
    )
    url = reverse("reporting:supplier-detail", args=[invoice.counterparty_id])

    finance_body = finance_client.get(url).content.decode()
    owner_body = owner_client.get(url).content.decode()
    source_url = reverse("imports:source", args=[invoice.import_batch_id])

    assert f"/reconciliation/?invoice={invoice.id}" in finance_body
    assert "去核销" in finance_body
    assert source_url in finance_body
    assert f"/reconciliation/?invoice={invoice.id}" not in owner_body
    assert "去核销" not in owner_body
    assert source_url not in owner_body
```

- [ ] **Step 5: Build the supplier summary template**

Create `templates/reporting/suppliers.html` using the existing page header, toolbar, summary band, table panel and Lucide icon patterns. Required fields:

- `as_of` date input and `q` search input;
- summary values: supplier count, cumulative invoices, cumulative payments and balance;
- one stable table row per supplier with all six financial values and latest activity;
- supplier name links to `reporting:supplier-detail` while preserving `as_of`;
- explicit empty state;
- coverage warning outside the table when coverage is not `full`.

Add a “供应商” tab to the view tabs in `receivables.html`, `payables.html`, `exceptions.html` and both new templates. Do not add another sidebar item; keep “应收应付” as the parent navigation concept.

- [ ] **Step 6: Build the supplier detail template**

Create `templates/reporting/supplier_detail.html` with:

- compact page header and back link to the supplier summary preserving `as_of`;
- four summary values: balance, unpaid invoices, unmatched payments, coverage label;
- unframed coverage warning band when status is not `full`;
- a horizontally scrollable, fixed-column table for date, type, reference, channel, increase, decrease, running balance, allocated, open amount, status and action;
- invoice rows use `receipt-text`, payment rows use `banknote` or `message-square` based on channel;
- finance-only source download and “去核销” links guarded with `{% if is_finance %}`;
- owner view remains fully readable but contains no write path.

Do not hide negative balances; format them with the existing warning color and a leading minus sign from `Decimal` formatting.

- [ ] **Step 7: Add restrained responsive CSS**

Extend `static/css/app.css` with supplier-specific table column widths and summary grid constraints. Reuse current colors, spacing, 8px-or-less radii and typography. Keep the table dense and operational; do not introduce decorative cards, gradients or viewport-scaled typography. At 390px width, the page header and toolbar must wrap without overlapping, while the ledger table scrolls horizontally instead of shrinking columns until text collides.

- [ ] **Step 8: Extend common reporting permission tests**

Add `"/reporting/suppliers/"` to `REPORTING_URLS` in `tests/web/test_reporting_views.py`. Add assertions that all reporting view tabs contain the supplier URL and that owner navigation still exposes no import or reconciliation write links.

- [ ] **Step 9: Run Web and reporting tests to verify GREEN**

Run:

```bash
.venv/bin/pytest tests/web/test_supplier_reporting_views.py tests/web/test_reporting_views.py tests/reporting/test_supplier_queries.py -q
```

Expected: all supplier query and page tests pass.

- [ ] **Step 10: Commit Web slice**

```bash
git add apps/reporting/views.py apps/reporting/urls.py templates/reporting/suppliers.html templates/reporting/supplier_detail.html templates/reporting/receivables.html templates/reporting/payables.html templates/reporting/exceptions.html static/css/app.css tests/web/test_supplier_reporting_views.py tests/web/test_reporting_views.py
git commit -m "feat: 增加供应商对账页面"
```

---

### Task 4: 回归验证、视觉检查与 DSM 部署

**Files:**
- Verify: all files from Tasks 1-3
- Modify only when verification finds a defect; any fix must start with a failing regression test.

**Interfaces:**
- Consumes: completed supplier reporting feature.
- Produces: tested commit and Compose-managed DSM deployment.

- [ ] **Step 1: Run formatting and deployment checks**

Run:

```bash
.venv/bin/ruff check apps/reporting tests/reporting tests/web
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/pytest tests/reporting tests/web/test_reporting_views.py tests/web/test_supplier_reporting_views.py -q
```

Expected: Ruff clean, Django reports no issues, no migration changes, targeted tests all pass.

- [ ] **Step 2: Run the complete suite**

Run:

```bash
.venv/bin/pytest
```

Expected: all tests pass with zero failures.

- [ ] **Step 3: Perform authenticated desktop and mobile visual checks**

Start the isolated Django test server using the repository's existing E2E runner. Log in as finance and owner, then inspect `/reporting/suppliers/` and one supplier detail at `1440x900` and `390x844`.

Verify:

- no overlapping header, filter, summary or table text;
- long supplier names wrap or truncate without changing row height unpredictably;
- every amount remains inside its numeric column;
- the ledger table scrolls horizontally on mobile;
- finance sees “去核销”; owner does not;
- coverage warning is visible but does not obscure content;
- browser console contains no errors.

If any defect is found, add a failing Web test where feasible, fix it, rerun targeted tests, and repeat both viewport checks.

- [ ] **Step 4: Confirm clean implementation scope**

Run:

```bash
git status --short
git log -4 --oneline
```

Expected: only pre-existing `.venv/` and `.workflow/` remain untracked; supplier feature commits are present.

- [ ] **Step 5: Sync changed runtime files to DSM with a dated backup**

Use the existing DSM target and application paths without storing credentials in the repository:

```bash
ssh -p 9099 jarvis@ace-station.top
```

On DSM, back up the files changed by Tasks 1-3 under:

```text
/volume4/docker/docker/shunda-finance/deploy-backups/supplier-reconciliation-20260731/
```

Sync only the changed runtime files into:

```text
/volume4/docker/docker/shunda-finance/app/
```

Do not copy `.env`, database files, test databases, `.venv/` or `.workflow/`.

- [ ] **Step 6: Rebuild and recreate only the Web service with Compose**

Run on DSM from `/volume4/docker/docker/shunda-finance/app`:

```bash
/usr/local/bin/docker compose -f compose.yml -f compose.dsm.yml build web
/usr/local/bin/docker compose -f compose.yml -f compose.dsm.yml up -d --no-deps --force-recreate web
```

Do not recreate the PostgreSQL service.

- [ ] **Step 7: Verify production after deployment**

Run:

```bash
/usr/local/bin/docker compose -f compose.yml -f compose.dsm.yml ps
/usr/local/bin/docker compose -f compose.yml -f compose.dsm.yml exec -T web python manage.py check
/usr/local/bin/docker compose -f compose.yml -f compose.dsm.yml exec -T web python manage.py migrate --check
curl --fail --silent --show-error http://sd.ace-station.top:1111/health/
```

Then log in as finance and owner and verify the same permissions and amounts on the public HTTP endpoint. Confirm recent Web and database logs contain no traceback or migration error. HTTPS remains out of scope.
