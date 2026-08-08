# 顺达财务核销系统第一期 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在群晖 DSM 上交付一套可追溯、人工确认的财务核销 Web 系统，覆盖全部历史发票、银行及微信流水导入，逐笔与结算批次核销，异常处理和老板 dashboard。

**Architecture:** 使用 Django 单体应用承载导入、核销、报表和权限，PostgreSQL 保存正式台账与不可变核销记录，DSM 持久化目录保存原始文件和附件。所有导入先暂存、预检再确认；所有核销都落为发票与实际资金之间的分配明细，不使用期初或期末余额生成对应关系。

**Tech Stack:** Python 3.12、Django 5.2、PostgreSQL 16、Gunicorn、openpyxl、xlrd、pypdf、Django templates、vanilla JavaScript、ECharts、Lucide、pytest、pytest-django、pytest-cov、Ruff、Playwright、Docker Compose。

## Global Constraints

- 生产部署必须使用 DSM Docker Compose，核心运行服务只有 `web` 与 `db`。
- 第一阶段不得引入 Redis、消息队列、独立 SPA、银行 API 或税务 API。
- 所有金额使用 `Decimal` 和数据库 `numeric(18,2)`，禁止使用浮点数参与财务计算。
- 系统只能提供候选；任何核销必须由财务人工确认。
- 发票只能与银行或微信的实际资金发生额核销，期初/期末余额不得参与候选算法。
- 原始文件、正式台账、核销及撤销记录不得物理删除。
- 同一发票或资金的累计有效分配金额不得超过其可核销金额。
- 财务拥有导入和核销权限；老板只读，完整银行账号与受限附件必须脱敏或拒绝访问。
- 支持逐笔核销与结算批次核销，两者必须生成相同类型的底层分配记录。
- 全部历史资料按年度迁移；不完整期间必须显示资料缺口，不允许系统猜测。
- UI 使用简体中文，保持紧凑、安静、适合重复操作；dashboard 使用本地打包的 ECharts，不依赖公网 CDN。
- 公共 Python 函数必须测试正常、边界和错误路径；总覆盖率不得低于 80%。
- 每个任务只提交本任务文件，使用中文 Git 提交信息。

---

## Planned File Structure

```text
.
├── manage.py
├── pyproject.toml
├── package.json
├── Dockerfile
├── compose.yml
├── .env.example
├── config/
│   ├── settings/{base,dev,prod,test}.py
│   ├── urls.py
│   └── wsgi.py
├── apps/
│   ├── core/              # health check、审计日志、公共金额与时间工具
│   ├── accounts/          # 财务/老板角色与权限
│   ├── parties/           # 往来单位、别名和资金账户
│   ├── imports/           # 源文件、暂存行、解析器和确认服务
│   ├── ledger/            # 发票、资金流水、余额快照和附件
│   ├── reconciliation/    # 逐笔核销、结算批次、候选和撤销
│   └── reporting/         # 应收应付、异常、dashboard 和导出
├── templates/
│   ├── base.html
│   ├── imports/
│   ├── ledger/
│   ├── reconciliation/
│   └── reporting/
├── static/
│   ├── css/app.css
│   ├── js/reconciliation-workbench.js
│   ├── js/dashboard.js
│   └── vendor/{echarts.min.js,lucide.min.js}
├── scripts/{backup.sh,restore.sh,wait_for_db.py}
├── tests/
│   ├── fixtures/
│   ├── e2e/
│   └── builders.py
└── docs/deployment-dsm.md
```

---

### Task 1: Django、PostgreSQL 与 Docker 基础

**Files:**
- Create: `pyproject.toml`
- Create: `manage.py`
- Create: `config/__init__.py`
- Create: `config/settings/base.py`
- Create: `config/settings/__init__.py`
- Create: `config/settings/dev.py`
- Create: `config/settings/prod.py`
- Create: `config/settings/test.py`
- Create: `config/urls.py`
- Create: `config/wsgi.py`
- Create: `apps/core/apps.py`
- Create: `apps/__init__.py`
- Create: `apps/core/__init__.py`
- Create: `apps/core/views.py`
- Create: `apps/core/urls.py`
- Create: `tests/test_health.py`
- Create: `Dockerfile`
- Create: `compose.yml`
- Create: `.env.example`
- Create: `scripts/wait_for_db.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: Django project settings, `GET /health/`, PostgreSQL connection and reproducible `docker compose` test command.
- Consumes: none.

- [ ] **Step 1: Write the failing health-check test**

```python
def test_health_check(client):
    response = client.get("/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 2: Run the test and verify the project is absent**

Run: `docker compose run --rm web pytest tests/test_health.py -q`

Expected: FAIL because `compose.yml`, Django settings, or `/health/` does not exist.

- [ ] **Step 3: Create pinned project dependencies**

```toml
[project]
name = "shunda-finance"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "Django~=5.2.0",
  "dj-database-url~=2.3",
  "psycopg[binary]~=3.2",
  "gunicorn~=23.0",
  "whitenoise~=6.9",
  "openpyxl~=3.1",
  "xlrd~=2.0",
  "pypdf~=5.4",
]

[project.optional-dependencies]
dev = [
  "pytest~=8.3",
  "pytest-django~=4.11",
  "pytest-cov~=6.1",
  "ruff~=0.11",
]

[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "config.settings.test"
python_files = ["test_*.py"]

[tool.coverage.run]
branch = true
source = ["apps"]

[tool.coverage.report]
fail_under = 80
show_missing = true
```

- [ ] **Step 4: Implement settings, health endpoint and container services**

```python
# apps/core/views.py
from django.http import JsonResponse


def health_check(request):
    return JsonResponse({"status": "ok"})
```

```yaml
# compose.yml
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-shunda_finance}
      POSTGRES_USER: ${POSTGRES_USER:-shunda}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-shunda_dev}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 5s
      timeout: 3s
      retries: 20
  web:
    build: .
    command: python manage.py runserver 0.0.0.0:8000
    env_file: .env
    volumes:
      - .:/app
      - uploads:/data/uploads
      - exports:/data/exports
    ports: ["8000:8000"]
    depends_on:
      db:
        condition: service_healthy
volumes:
  postgres_data:
  uploads:
  exports:
```

- [ ] **Step 5: Build, migrate and run the health test**

Run: `cp .env.example .env && docker compose build web && docker compose run --rm web python manage.py migrate && docker compose run --rm web pytest tests/test_health.py -q`

Expected: `1 passed`.

- [ ] **Step 6: Run static checks**

Run: `docker compose run --rm web ruff check .`

Expected: no Ruff errors.

- [ ] **Step 7: Commit**

```bash
git add .gitignore .env.example Dockerfile compose.yml manage.py pyproject.toml config apps/core scripts/wait_for_db.py tests/test_health.py
git commit -m "feat: 初始化财务系统项目与容器环境"
```

---

### Task 2: 用户角色、权限与审计基础

**Files:**
- Create: `apps/accounts/apps.py`
- Create: `apps/accounts/roles.py`
- Create: `apps/accounts/signals.py`
- Create: `apps/core/models.py`
- Create: `apps/core/audit.py`
- Create: `apps/core/migrations/0001_initial.py`
- Create: `tests/accounts/test_roles.py`
- Create: `tests/core/test_audit.py`
- Create: `tests/conftest.py`
- Modify: `config/settings/base.py`

**Interfaces:**
- Produces: `Role.FINANCE`, `Role.OWNER`, `user_has_role(user, role)`, `record_audit(actor, action, target, changes)` and immutable `AuditLog`.
- Consumes: Task 1 Django project.

- [ ] **Step 1: Write failing role and audit tests**

```python
import pytest
from django.contrib.auth.models import User
from apps.accounts.roles import Role, assign_role, user_has_role
from apps.core.audit import record_audit
from apps.core.models import AuditLog


@pytest.mark.django_db
def test_finance_and_owner_roles_are_distinct():
    user = User.objects.create_user("finance", password="secret")
    assign_role(user, Role.FINANCE)
    assert user_has_role(user, Role.FINANCE)
    assert not user_has_role(user, Role.OWNER)


@pytest.mark.django_db
def test_audit_log_records_actor_action_and_changes():
    user = User.objects.create_user("finance")
    entry = record_audit(user, "counterparty.alias.created", user, {"value": "铁路专户"})
    assert AuditLog.objects.get(pk=entry.pk).changes["value"] == "铁路专户"
```

- [ ] **Step 2: Verify the tests fail**

Run: `docker compose run --rm web pytest tests/accounts/test_roles.py tests/core/test_audit.py -q`

Expected: FAIL because the roles and audit APIs do not exist.

- [ ] **Step 3: Implement roles and post-migrate group creation**

```python
# apps/accounts/roles.py
from enum import StrEnum
from django.contrib.auth.models import Group


class Role(StrEnum):
    FINANCE = "财务"
    OWNER = "老板"


def assign_role(user, role: Role) -> None:
    group, _ = Group.objects.get_or_create(name=role.value)
    user.groups.add(group)


def user_has_role(user, role: Role) -> bool:
    return user.is_authenticated and user.groups.filter(name=role.value).exists()
```

```python
# tests/conftest.py
@pytest.fixture
def finance_user(db):
    user = User.objects.create_user("finance", password="secret")
    assign_role(user, Role.FINANCE)
    return user


@pytest.fixture
def owner_user(db):
    user = User.objects.create_user("owner", password="secret")
    assign_role(user, Role.OWNER)
    return user


@pytest.fixture
def finance_client(client, finance_user):
    client.force_login(finance_user)
    return client


@pytest.fixture
def owner_client(client, owner_user):
    client.force_login(owner_user)
    return client
```

- [ ] **Step 4: Implement immutable audit records**

```python
# apps/core/models.py
class UUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)

    class Meta:
        abstract = True


class ImmutableQuerySet(models.QuerySet):
    def delete(self):
        raise RuntimeError("正式财务记录不允许物理删除")


class ImmutableLedgerModel(UUIDModel):
    objects = ImmutableQuerySet.as_manager()

    class Meta:
        abstract = True

    def delete(self, *args, **kwargs):
        raise RuntimeError("正式财务记录不允许物理删除")


class AuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=100)
    target_type = models.CharField(max_length=100)
    target_id = models.CharField(max_length=64)
    changes = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    def delete(self, *args, **kwargs):
        raise RuntimeError("审计日志不允许删除")
```

`ImportBatch`, `SourceFile`, `Invoice`, `MoneyTransaction`, `Reconciliation`, `ReconciliationAllocation` and `ReconciliationReversal` must inherit `ImmutableLedgerModel`. `StagedRow` remains deletable only before batch confirmation.

- [ ] **Step 5: Create migrations and run tests**

Run: `docker compose run --rm web python manage.py makemigrations core && docker compose run --rm web pytest tests/accounts/test_roles.py tests/core/test_audit.py -q`

Expected: all tests pass, including a test that `AuditLog.delete()` raises `RuntimeError`.

- [ ] **Step 6: Commit**

```bash
git add apps/accounts apps/core config/settings/base.py tests/accounts tests/core
git commit -m "feat: 增加财务老板角色与审计日志"
```

---

### Task 3: 导入批次与原始文件暂存模型

**Files:**
- Create: `apps/imports/apps.py`
- Create: `apps/imports/choices.py`
- Create: `apps/imports/models.py`
- Create: `apps/imports/storage.py`
- Create: `apps/imports/migrations/0001_initial.py`
- Create: `tests/imports/test_models.py`
- Create: `tests/imports/test_storage.py`
- Modify: `config/settings/base.py`

**Interfaces:**
- Produces: `ImportBatch`, `SourceFile`, `StagedRow`, `sha256_file(file_obj) -> str`, `source_upload_path(instance, filename) -> str`.
- Consumes: Task 2 users and audit foundation.

- [ ] **Step 1: Write failing storage and model tests**

```python
from io import BytesIO
import pytest
from apps.imports.choices import BatchStatus, SourceKind
from apps.imports.models import ImportBatch
from apps.imports.storage import sha256_file


def test_sha256_file_is_stable_and_rewinds_stream():
    stream = BytesIO(b"same-content")
    first = sha256_file(stream)
    second = sha256_file(stream)
    assert first == second
    assert stream.tell() == 0


@pytest.mark.django_db
def test_new_batch_starts_in_uploaded_state(finance_user):
    batch = ImportBatch.objects.create(source_kind=SourceKind.BANK, created_by=finance_user)
    assert batch.status == BatchStatus.UPLOADED
```

- [ ] **Step 2: Verify tests fail**

Run: `docker compose run --rm web pytest tests/imports/test_models.py tests/imports/test_storage.py -q`

Expected: FAIL because import persistence does not exist.

- [ ] **Step 3: Implement import persistence and status choices**

```python
class ImportBatch(ImmutableLedgerModel):
    source_kind = models.CharField(max_length=20, choices=SourceKind.choices)
    status = models.CharField(max_length=20, choices=BatchStatus.choices, default=BatchStatus.UPLOADED)
    total_rows = models.PositiveIntegerField(default=0)
    valid_rows = models.PositiveIntegerField(default=0)
    duplicate_rows = models.PositiveIntegerField(default=0)
    error_rows = models.PositiveIntegerField(default=0)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)


class SourceFile(ImmutableLedgerModel):
    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="source_files")
    file = models.FileField(upload_to=source_upload_path)
    original_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64, unique=True)
    size = models.PositiveBigIntegerField()


class StagedRow(UUIDModel):
    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="rows")
    row_number = models.PositiveIntegerField()
    raw_data = models.JSONField()
    normalized_data = models.JSONField(default=dict)
    issues = models.JSONField(default=list)
    is_duplicate = models.BooleanField(default=False)
    posted_at = models.DateTimeField(null=True, blank=True)
```

- [ ] **Step 4: Add constraints and migrations**

Add a unique constraint on `(batch, row_number)` and database indexes on `status`, `source_kind`, `period_start`, and `period_end`.

```python
class Meta:
    constraints = [
        models.UniqueConstraint(fields=["batch", "row_number"], name="uniq_staged_row_number"),
    ]
    indexes = [
        models.Index(fields=["batch", "posted_at"]),
        models.Index(fields=["is_duplicate"]),
    ]
```

Run: `docker compose run --rm web python manage.py makemigrations imports && docker compose run --rm web python manage.py migrate`.

- [ ] **Step 5: Run tests**

Run: `docker compose run --rm web pytest tests/imports -q`

Expected: all import persistence tests pass.

- [ ] **Step 6: Commit**

```bash
git add apps/imports config/settings/base.py tests/imports
git commit -m "feat: 建立导入批次与原始文件暂存"
```

---

### Task 4: 往来单位、发票、资金与余额快照

**Files:**
- Create: `apps/parties/apps.py`
- Create: `apps/parties/models.py`
- Create: `apps/parties/normalization.py`
- Create: `apps/parties/migrations/0001_initial.py`
- Create: `apps/ledger/apps.py`
- Create: `apps/ledger/choices.py`
- Create: `apps/ledger/models.py`
- Create: `apps/ledger/migrations/0001_initial.py`
- Create: `tests/builders.py`
- Create: `tests/parties/test_models.py`
- Create: `tests/ledger/test_models.py`
- Modify: `config/settings/base.py`

**Interfaces:**
- Produces: `Counterparty`, `CounterpartyAlias`, `FundingAccount`, `Invoice`, `MoneyTransaction`, `AccountBalanceSnapshot`, `normalize_party_text(value) -> str` and reusable builders in `tests/builders.py`.
- Consumes: Task 3 `ImportBatch`.

- [ ] **Step 1: Write failing model invariant tests**

```python
from decimal import Decimal
import pytest
from django.db import IntegrityError
from apps.ledger.choices import InvoiceDirection, MoneyDirection
from tests.builders import make_invoice, make_transaction


@pytest.mark.django_db
def test_invoice_amount_must_be_positive(finance_user):
    with pytest.raises(IntegrityError):
        make_invoice(finance_user, total_amount=Decimal("0.00"))


@pytest.mark.django_db
def test_transaction_direction_is_explicit(finance_user):
    tx = make_transaction(finance_user, direction=MoneyDirection.OUTFLOW, amount=Decimal("2000.00"))
    assert tx.amount == Decimal("2000.00")
    assert tx.direction == MoneyDirection.OUTFLOW
```

- [ ] **Step 2: Verify tests fail**

Run: `docker compose run --rm web pytest tests/parties tests/ledger -q`

Expected: FAIL because ledger models and builders are missing.

- [ ] **Step 3: Implement parties and aliases**

```python
class Counterparty(UUIDModel):
    name = models.CharField(max_length=255)
    normalized_name = models.CharField(max_length=255, db_index=True)
    tax_id = models.CharField(max_length=32, blank=True, db_index=True)
    is_customer = models.BooleanField(default=False)
    is_supplier = models.BooleanField(default=False)
    active = models.BooleanField(default=True)


class CounterpartyAlias(UUIDModel):
    counterparty = models.ForeignKey(Counterparty, on_delete=models.PROTECT, related_name="aliases")
    kind = models.CharField(max_length=20, choices=AliasKind.choices)
    value = models.CharField(max_length=255)
    normalized_value = models.CharField(max_length=255)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
```

Add a unique constraint on `(kind, normalized_value)` so one bank account or alias cannot silently point to two units.

- [ ] **Step 4: Implement ledger models and database checks**

```python
class Invoice(ImmutableLedgerModel):
    direction = models.CharField(max_length=10, choices=InvoiceDirection.choices)
    invoice_number = models.CharField(max_length=30)
    seller_tax_id = models.CharField(max_length=32)
    buyer_tax_id = models.CharField(max_length=32)
    issue_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=18, decimal_places=2)
    status = models.CharField(max_length=12, choices=InvoiceStatus.choices)
    counterparty = models.ForeignKey(Counterparty, on_delete=models.PROTECT)
    import_batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT)
    source_row = models.PositiveIntegerField()
    source_payload = models.JSONField(default=dict)


class MoneyTransaction(ImmutableLedgerModel):
    account = models.ForeignKey("FundingAccount", on_delete=models.PROTECT)
    channel = models.CharField(max_length=12, choices=MoneyChannel.choices)
    direction = models.CharField(max_length=10, choices=MoneyDirection.choices)
    occurred_at = models.DateTimeField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    balance_after = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    transaction_id = models.CharField(max_length=100, blank=True)
    fingerprint = models.CharField(max_length=64, unique=True)
    counterparty = models.ForeignKey(Counterparty, null=True, blank=True, on_delete=models.PROTECT)
    counterparty_raw_name = models.CharField(max_length=255, blank=True)
    counterparty_account = models.CharField(max_length=100, blank=True)
    import_batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT)
    source_row = models.PositiveIntegerField()
    source_payload = models.JSONField(default=dict)
```

Add `CheckConstraint` rules for positive invoice and transaction amounts and a unique constraint on `(invoice_number, seller_tax_id)`.

```python
# tests/builders.py
def make_invoice(actor, *, direction=InvoiceDirection.INPUT, total_amount=Decimal("1000.00"), counterparty=None):
    source_kind = SourceKind.INPUT_INVOICE if direction == InvoiceDirection.INPUT else SourceKind.OUTPUT_INVOICE
    batch = ImportBatch.objects.create(source_kind=source_kind, created_by=actor)
    party = counterparty or Counterparty.objects.create(name="测试单位", normalized_name="测试单位", is_supplier=True)
    return Invoice.objects.create(
        direction=direction,
        invoice_number=uuid4().hex[:20],
        seller_tax_id="913200000000000001",
        buyer_tax_id="91320281TEST000001",
        issue_date=date(2026, 7, 1),
        total_amount=total_amount,
        status=InvoiceStatus.NORMAL,
        counterparty=party,
        import_batch=batch,
        source_row=1,
    )


def make_transaction(actor, *, direction=MoneyDirection.OUTFLOW, amount=Decimal("1000.00"), counterparty=None):
    batch = ImportBatch.objects.create(source_kind=SourceKind.BANK, created_by=actor)
    party = counterparty or Counterparty.objects.create(name="测试收款方", normalized_name=uuid4().hex, is_supplier=True)
    account, _ = FundingAccount.objects.get_or_create(
        identifier="1064330104009859",
        defaults={"channel": MoneyChannel.BANK, "name": "测试银行账户", "masked_identifier": "************9859"},
    )
    return MoneyTransaction.objects.create(
        account=account,
        channel=MoneyChannel.BANK,
        direction=direction,
        occurred_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        amount=amount,
        fingerprint=sha256(uuid4().bytes).hexdigest(),
        counterparty=party,
        counterparty_raw_name=party.name,
        import_batch=batch,
        source_row=1,
    )
```

- [ ] **Step 5: Implement funding accounts and dashboard-only snapshots**

`FundingAccount` stores channel, account name and masked identifier. `AccountBalanceSnapshot` stores an account, `as_of`, balance and source batch. The reconciliation package must not import this model.

```python
class FundingAccount(UUIDModel):
    channel = models.CharField(max_length=12, choices=MoneyChannel.choices)
    name = models.CharField(max_length=100)
    identifier = models.CharField(max_length=100)
    masked_identifier = models.CharField(max_length=100)
    active = models.BooleanField(default=True)


class AccountBalanceSnapshot(ImmutableLedgerModel):
    account = models.ForeignKey(FundingAccount, on_delete=models.PROTECT, related_name="balance_snapshots")
    as_of = models.DateTimeField()
    balance = models.DecimalField(max_digits=18, decimal_places=2)
    source_batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "as_of"], name="uniq_account_balance_snapshot"),
        ]
```

- [ ] **Step 6: Create migrations and run tests**

Run: `docker compose run --rm web python manage.py makemigrations parties ledger && docker compose run --rm web python manage.py migrate && docker compose run --rm web pytest tests/parties tests/ledger -q`

Expected: positive amount, uniqueness, normalization and account snapshot tests all pass.

- [ ] **Step 7: Commit**

```bash
git add apps/parties apps/ledger config/settings/base.py tests/builders.py tests/parties tests/ledger
git commit -m "feat: 建立往来单位发票与资金台账"
```

---

### Task 5: 不可超额的逐笔核销领域服务

**Files:**
- Create: `apps/reconciliation/apps.py`
- Create: `apps/reconciliation/choices.py`
- Create: `apps/reconciliation/models.py`
- Create: `apps/reconciliation/services.py`
- Create: `apps/reconciliation/queries.py`
- Create: `apps/reconciliation/migrations/0001_initial.py`
- Create: `tests/reconciliation/test_services.py`
- Create: `tests/reconciliation/test_queries.py`
- Modify: `tests/conftest.py`
- Modify: `config/settings/base.py`

**Interfaces:**
- Produces: `AllocationInput`, `create_reconciliation(...)`, `reverse_reconciliation(...)`, `invoice_open_amount(invoice_id)`, `transaction_open_amount(transaction_id)`.
- Consumes: Task 4 `Invoice`, `MoneyTransaction`, `Counterparty` and Task 2 audit service.

- [ ] **Step 1: Write failing one-to-many and over-allocation tests**

```python
from decimal import Decimal
import pytest
from django.core.exceptions import ValidationError
from apps.reconciliation.services import AllocationInput, create_reconciliation
from apps.reconciliation.choices import ReconciliationDirection


@pytest.mark.django_db(transaction=True)
def test_one_invoice_can_use_multiple_payments(finance_user, input_invoice, two_outflows):
    result = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[
            AllocationInput(input_invoice.id, two_outflows[0].id, Decimal("400.00")),
            AllocationInput(input_invoice.id, two_outflows[1].id, Decimal("600.00")),
        ],
        note="分两次支付",
    )
    assert result.allocations.count() == 2


@pytest.mark.django_db(transaction=True)
def test_payment_cannot_be_allocated_twice(finance_user, input_invoice, outflow):
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(input_invoice.id, outflow.id, outflow.amount)],
    )
    with pytest.raises(ValidationError, match="资金可核销金额不足"):
        create_reconciliation(
            actor=finance_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[AllocationInput(input_invoice.id, outflow.id, Decimal("0.01"))],
        )
```

```python
# tests/conftest.py additions
@pytest.fixture
def input_invoice(finance_user):
    return make_invoice(finance_user, direction=InvoiceDirection.INPUT, total_amount=Decimal("1000.00"))


@pytest.fixture
def two_outflows(finance_user, input_invoice):
    return [
        make_transaction(finance_user, direction=MoneyDirection.OUTFLOW, amount=Decimal("400.00"), counterparty=input_invoice.counterparty),
        make_transaction(finance_user, direction=MoneyDirection.OUTFLOW, amount=Decimal("600.00"), counterparty=input_invoice.counterparty),
    ]


@pytest.fixture
def outflow(finance_user, input_invoice):
    return make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("1000.00"),
        counterparty=input_invoice.counterparty,
    )
```

- [ ] **Step 2: Verify tests fail**

Run: `docker compose run --rm web pytest tests/reconciliation/test_services.py -q`

Expected: FAIL because reconciliation models and services do not exist.

- [ ] **Step 3: Implement reconciliation and reversal records**

```python
class Reconciliation(ImmutableLedgerModel):
    direction = models.CharField(max_length=20, choices=ReconciliationDirection.choices)
    mode = models.CharField(max_length=10, choices=ReconciliationMode.choices)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ReconciliationAllocation(ImmutableLedgerModel):
    reconciliation = models.ForeignKey(Reconciliation, on_delete=models.PROTECT, related_name="allocations")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT)
    transaction = models.ForeignKey(MoneyTransaction, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=18, decimal_places=2)


class ReconciliationReversal(ImmutableLedgerModel):
    original = models.OneToOneField(Reconciliation, on_delete=models.PROTECT, related_name="reversal")
    reversed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
```

- [ ] **Step 4: Implement transaction-safe allocation**

```python
@dataclass(frozen=True)
class AllocationInput:
    invoice_id: UUID
    transaction_id: UUID
    amount: Decimal


@transaction.atomic
def create_reconciliation(*, actor, direction, allocations, note="", mode=ReconciliationMode.DIRECT):
    invoice_ids = {item.invoice_id for item in allocations}
    transaction_ids = {item.transaction_id for item in allocations}
    invoices = {obj.id: obj for obj in Invoice.objects.select_for_update().filter(id__in=invoice_ids)}
    transactions = {obj.id: obj for obj in MoneyTransaction.objects.select_for_update().filter(id__in=transaction_ids)}
    _validate_allocations(direction, allocations, invoices, transactions)
    reconciliation = Reconciliation.objects.create(
        direction=direction, mode=mode, created_by=actor, note=note
    )
    ReconciliationAllocation.objects.bulk_create([
        ReconciliationAllocation(
            reconciliation=reconciliation,
            invoice=invoices[item.invoice_id],
            transaction=transactions[item.transaction_id],
            amount=item.amount,
        )
        for item in allocations
    ])
    record_audit(actor, "reconciliation.created", reconciliation, {"allocation_count": len(allocations)})
    return reconciliation
```

Validation must reject zero/negative amounts, void invoices, direction mismatch, counterparty mismatch, insufficient invoice amount and insufficient transaction amount.

- [ ] **Step 5: Implement open-amount queries that exclude reversed reconciliations**

```python
MONEY_FIELD = models.DecimalField(max_digits=18, decimal_places=2)


def invoice_open_amount(invoice_id):
    invoice = Invoice.objects.annotate(
        allocated=Coalesce(
            Sum(
                "reconciliationallocation__amount",
                filter=Q(reconciliationallocation__reconciliation__reversal__isnull=True),
            ),
            Decimal("0.00"),
            output_field=MONEY_FIELD,
        )
    ).get(pk=invoice_id)
    return invoice.total_amount - invoice.allocated


def transaction_open_amount(transaction_id):
    money = MoneyTransaction.objects.annotate(
        allocated=Coalesce(
            Sum(
                "reconciliationallocation__amount",
                filter=Q(reconciliationallocation__reconciliation__reversal__isnull=True),
            ),
            Decimal("0.00"),
            output_field=MONEY_FIELD,
        )
    ).get(pk=transaction_id)
    return money.amount - money.allocated
```

Add tests for unused, partial, fully used and reversed states.

- [ ] **Step 6: Implement reversal and audit behavior**

```python
@transaction.atomic
def reverse_reconciliation(*, actor, reconciliation_id, reason):
    if not reason.strip():
        raise ValidationError("撤销原因不能为空")
    original = Reconciliation.objects.select_for_update().get(pk=reconciliation_id)
    if ReconciliationReversal.objects.filter(original=original).exists():
        raise ValidationError("该核销已经撤销")
    reversal = ReconciliationReversal.objects.create(
        original=original,
        reversed_by=actor,
        reason=reason.strip(),
    )
    record_audit(actor, "reconciliation.reversed", original, {"reason": reversal.reason})
    return reversal
```

- [ ] **Step 7: Run migration and tests**

Run: `docker compose run --rm web python manage.py makemigrations reconciliation && docker compose run --rm web python manage.py migrate && docker compose run --rm web pytest tests/reconciliation -q`

Expected: all direct, partial, over-allocation, direction, void and reversal tests pass.

- [ ] **Step 8: Commit**

```bash
git add apps/reconciliation config/settings/base.py tests/reconciliation
git commit -m "feat: 实现安全可撤销的逐笔核销"
```

---

### Task 6: 候选组合与结算批次核销

**Files:**
- Modify: `apps/reconciliation/models.py`
- Modify: `apps/reconciliation/services.py`
- Create: `apps/reconciliation/candidates.py`
- Create: `apps/reconciliation/settlements.py`
- Create: `apps/reconciliation/migrations/0002_settlement_batch.py`
- Create: `tests/reconciliation/test_candidates.py`
- Create: `tests/reconciliation/test_settlements.py`
- Create: `tests/reconciliation/conftest.py`

**Interfaces:**
- Produces: `Candidate`, `transaction_candidates(invoice_id, start, end)`, `invoice_candidates(transaction_id, start, end)`, `confirm_settlement_batch(batch_id, actor, allocations)`.
- Consumes: Task 5 allocation service and open-amount queries.

- [ ] **Step 1: Write failing contiguous-date candidate test**

```python
from decimal import Decimal
import pytest
from apps.reconciliation.candidates import transaction_candidates


@pytest.mark.django_db
def test_candidate_finds_only_exact_contiguous_payment_window(synthetic_railway_invoice, synthetic_railway_june_transactions):
    candidates = transaction_candidates(
        synthetic_railway_invoice.id,
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    )
    exact = [item for item in candidates if item.difference == Decimal("0.00")]
    assert [(item.start_at.date(), item.end_at.date(), item.total) for item in exact] == [
        (date(2026, 6, 2), date(2026, 6, 27), Decimal("46050.00"))
    ]
```

```python
# tests/reconciliation/conftest.py
JUNE_PAYMENTS = [
    (date(2026, 6, 2), "4400.00"), (date(2026, 6, 3), "2700.00"),
    (date(2026, 6, 4), "2900.00"), (date(2026, 6, 9), "850.00"),
    (date(2026, 6, 14), "3750.00"), (date(2026, 6, 16), "2000.00"),
    (date(2026, 6, 17), "8000.00"), (date(2026, 6, 18), "8100.00"),
    (date(2026, 6, 19), "2750.00"), (date(2026, 6, 23), "4150.00"),
    (date(2026, 6, 24), "5800.00"), (date(2026, 6, 27), "850.00"),
]


@pytest.fixture
def synthetic_railway_party():
    return Counterparty.objects.create(
        name="测试铁路物流有限公司",
        normalized_name="测试铁路物流有限公司",
        is_supplier=True,
    )


@pytest.fixture
def synthetic_railway_invoice(finance_user, synthetic_railway_party):
    return make_invoice(
        finance_user,
        direction=InvoiceDirection.INPUT,
        total_amount=Decimal("46050.00"),
        counterparty=synthetic_railway_party,
    )


@pytest.fixture
def synthetic_railway_june_transactions(finance_user, synthetic_railway_party):
    transactions = []
    for paid_on, amount in JUNE_PAYMENTS:
        item = make_transaction(
            finance_user,
            direction=MoneyDirection.OUTFLOW,
            amount=Decimal(amount),
            counterparty=synthetic_railway_party,
        )
        item.occurred_at = datetime.combine(paid_on, time(9, 0), tzinfo=UTC)
        item.save(update_fields=["occurred_at"])
        transactions.append(item)
    return transactions
```

- [ ] **Step 2: Write failing no-balance test**

Create an `AccountBalanceSnapshot` containing `50000.00` without matching transactions and assert `transaction_candidates(...)` returns no candidate derived from that snapshot.

- [ ] **Step 3: Verify candidate tests fail**

Run: `docker compose run --rm web pytest tests/reconciliation/test_candidates.py -q`

Expected: FAIL because candidate services do not exist.

- [ ] **Step 4: Implement exact and contiguous candidate generation**

```python
@dataclass(frozen=True)
class Candidate:
    kind: str
    transaction_ids: tuple[UUID, ...]
    invoice_ids: tuple[UUID, ...]
    total: Decimal
    difference: Decimal
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class AvailableTransaction:
    id: UUID
    occurred_at: datetime
    open_amount: Decimal


def available_transactions_for_invoice(invoice, *, start, end):
    expected_direction = (
        MoneyDirection.OUTFLOW if invoice.direction == InvoiceDirection.INPUT else MoneyDirection.INFLOW
    )
    query = MoneyTransaction.objects.filter(
        counterparty=invoice.counterparty,
        direction=expected_direction,
        occurred_at__date__range=(start, end),
    ).order_by("occurred_at", "id")
    for money in query:
        open_amount = transaction_open_amount(money.id)
        if open_amount > 0:
            yield AvailableTransaction(money.id, money.occurred_at, open_amount)


def _contiguous_windows(items, target: Decimal):
    for start_index in range(len(items)):
        total = Decimal("0.00")
        for end_index in range(start_index, len(items)):
            total += items[end_index].open_amount
            if total == target:
                yield items[start_index:end_index + 1], total
            if total > target:
                break


def transaction_candidates(invoice_id, *, start, end):
    invoice = Invoice.objects.select_related("counterparty").get(pk=invoice_id)
    target = invoice_open_amount(invoice.id)
    items = list(available_transactions_for_invoice(invoice, start=start, end=end))
    exact = [
        Candidate(
            kind="CONTIGUOUS_EXACT",
            transaction_ids=tuple(item.id for item in window),
            invoice_ids=(invoice.id,),
            total=total,
            difference=Decimal("0.00"),
            start_at=window[0].occurred_at,
            end_at=window[-1].occurred_at,
        )
        for window, total in _contiguous_windows(items, target)
    ]
    if exact:
        return exact
    available_total = sum((item.open_amount for item in items), Decimal("0.00"))
    return [Candidate(
        kind="PARTIAL",
        transaction_ids=tuple(item.id for item in items),
        invoice_ids=(invoice.id,),
        total=min(target, available_total),
        difference=max(target - available_total, Decimal("0.00")),
        start_at=items[0].occurred_at,
        end_at=items[-1].occurred_at,
    )] if items else []


def invoice_candidates(transaction_id, *, start, end):
    money = MoneyTransaction.objects.select_related("counterparty").get(pk=transaction_id)
    direction = InvoiceDirection.INPUT if money.direction == MoneyDirection.OUTFLOW else InvoiceDirection.OUTPUT
    target = transaction_open_amount(money.id)
    invoices = Invoice.objects.filter(
        counterparty=money.counterparty,
        direction=direction,
        issue_date__range=(start, end),
        status=InvoiceStatus.NORMAL,
    ).order_by("issue_date", "id")
    available = [(invoice, invoice_open_amount(invoice.id)) for invoice in invoices]
    available = [(invoice, amount) for invoice, amount in available if amount > 0]
    candidates = []
    for start_index in range(len(available)):
        total = Decimal("0.00")
        for end_index in range(start_index, len(available)):
            total += available[end_index][1]
            if total == target:
                window = available[start_index:end_index + 1]
                candidates.append(Candidate(
                    kind="MULTI_INVOICE_EXACT",
                    transaction_ids=(money.id,),
                    invoice_ids=tuple(invoice.id for invoice, _ in window),
                    total=total,
                    difference=Decimal("0.00"),
                    start_at=datetime.combine(window[0][0].issue_date, time.min, tzinfo=UTC),
                    end_at=datetime.combine(window[-1][0].issue_date, time.max, tzinfo=UTC),
                ))
            if total > target:
                break
    return candidates
```

Candidate queries must filter by confirmed counterparty, correct money direction, date window and positive open amount. They must never import or query `AccountBalanceSnapshot`.

- [ ] **Step 5: Implement settlement batch models and confirmation**

```python
class SettlementBatch(UUIDModel):
    counterparty = models.ForeignKey(Counterparty, on_delete=models.PROTECT)
    direction = models.CharField(max_length=20, choices=ReconciliationDirection.choices)
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=12, choices=SettlementStatus.choices, default=SettlementStatus.DRAFT)
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)


class SettlementBatchInvoice(UUIDModel):
    batch = models.ForeignKey(SettlementBatch, on_delete=models.PROTECT, related_name="invoice_items")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT)


class SettlementBatchTransaction(UUIDModel):
    batch = models.ForeignKey(SettlementBatch, on_delete=models.PROTECT, related_name="transaction_items")
    transaction = models.ForeignKey(MoneyTransaction, on_delete=models.PROTECT)


# Add this nullable field to Reconciliation after SettlementBatch exists.
settlement_batch = models.ForeignKey(
    SettlementBatch,
    null=True,
    blank=True,
    on_delete=models.PROTECT,
    related_name="reconciliations",
)
```

Extend the Task 5 service only after `SettlementBatch` exists:

```python
@transaction.atomic
def create_reconciliation(
    *, actor, direction, allocations, note="",
    mode=ReconciliationMode.DIRECT,
    settlement_batch: SettlementBatch | None = None,
):
    invoice_ids = {item.invoice_id for item in allocations}
    transaction_ids = {item.transaction_id for item in allocations}
    invoices = {obj.id: obj for obj in Invoice.objects.select_for_update().filter(id__in=invoice_ids)}
    transactions = {obj.id: obj for obj in MoneyTransaction.objects.select_for_update().filter(id__in=transaction_ids)}
    _validate_allocations(direction, allocations, invoices, transactions)
    reconciliation = Reconciliation.objects.create(
        direction=direction,
        mode=mode,
        created_by=actor,
        note=note,
        settlement_batch=settlement_batch,
    )
    ReconciliationAllocation.objects.bulk_create([
        ReconciliationAllocation(
            reconciliation=reconciliation,
            invoice=invoices[item.invoice_id],
            transaction=transactions[item.transaction_id],
            amount=item.amount,
        )
        for item in allocations
    ])
    record_audit(actor, "reconciliation.created", reconciliation, {"allocation_count": len(allocations)})
    return reconciliation
```

`confirm_settlement_batch` must verify version, item ownership and allocation totals, call `create_reconciliation(..., mode=BATCH, settlement_batch=batch)`, then mark the batch confirmed in one database transaction.

- [ ] **Step 6: Run candidate and batch tests**

Run: `docker compose run --rm web pytest tests/reconciliation/test_candidates.py tests/reconciliation/test_settlements.py -q`

Expected: exact single, contiguous, partial, overlapping-used-record, stale-version and confirmed-batch tests all pass.

- [ ] **Step 7: Commit**

```bash
git add apps/reconciliation tests/reconciliation
git commit -m "feat: 增加候选组合与结算批次核销"
```

---

### Task 7: 通用导入解析、暂存与确认引擎

**Files:**
- Create: `apps/imports/types.py`
- Create: `apps/imports/registry.py`
- Create: `apps/imports/validation.py`
- Create: `apps/imports/services.py`
- Create: `apps/imports/fingerprints.py`
- Create: `tests/imports/test_registry.py`
- Create: `tests/imports/test_services.py`
- Create: `tests/imports/test_fingerprints.py`
- Create: `tests/imports/fakes.py`

**Interfaces:**
- Produces: `NormalizedInvoiceRow`, `NormalizedTransactionRow`, `RowIssue`, `Importer` protocol, `stage_upload(...)`, `confirm_batch(...)`, `transaction_fingerprint(...)`.
- Consumes: Task 3 staging models and Task 4 ledger models.

- [ ] **Step 1: Write failing registry and idempotency tests**

```python
def test_registry_rejects_unknown_headers(importer_registry):
    with pytest.raises(UnsupportedTemplateError, match="无法识别文件模板"):
        importer_registry.detect("unknown.xlsx", ["A", "B"])


@pytest.mark.django_db
def test_same_file_hash_cannot_create_second_source_file(finance_user, fake_import_file):
    first = stage_upload(fake_import_file, source_kind=SourceKind.INPUT_INVOICE, actor=finance_user)
    fake_import_file.seek(0)
    with pytest.raises(DuplicateSourceFileError):
        stage_upload(fake_import_file, source_kind=SourceKind.INPUT_INVOICE, actor=finance_user)
```

- [ ] **Step 2: Verify tests fail**

Run: `docker compose run --rm web pytest tests/imports/test_registry.py tests/imports/test_services.py -q`

Expected: FAIL because registry and services do not exist.

- [ ] **Step 3: Define normalized row contracts**

```python
@dataclass(frozen=True)
class NormalizedInvoiceRow:
    direction: str
    invoice_number: str
    seller_tax_id: str
    seller_name: str
    buyer_tax_id: str
    buyer_name: str
    issue_date: date
    total_amount: Decimal
    status: str


@dataclass(frozen=True)
class NormalizedTransactionRow:
    channel: str
    direction: str
    occurred_at: datetime
    amount: Decimal
    balance_after: Decimal | None
    account_identifier: str
    counterparty_name: str
    counterparty_account: str
    transaction_id: str
    summary: str


@dataclass(frozen=True)
class RowIssue:
    code: str
    message: str
    field: str = ""


@dataclass(frozen=True)
class ParsedRow:
    row_number: int
    raw_data: dict[str, object]
    normalized: NormalizedInvoiceRow | NormalizedTransactionRow | None
    issues: tuple[RowIssue, ...] = ()


@dataclass(frozen=True)
class ImportResult:
    batch_id: UUID
    posted_rows: int
    duplicate_rows: int
    error_rows: int

    @classmethod
    def from_batch(cls, batch):
        return cls(batch.id, batch.rows.filter(posted_at__isnull=False).count(), batch.duplicate_rows, batch.error_rows)

    def as_dict(self):
        return dataclasses.asdict(self)


class UnsupportedTemplateError(ValueError):
    pass


class DuplicateSourceFileError(ValueError):
    pass


class RowValidationError(ValueError):
    pass
```

- [ ] **Step 4: Implement parser registry and row validation**

```python
class Importer(Protocol):
    source_kinds: frozenset[str]

    def supports(self, filename: str, headers: list[str]) -> bool: ...
    def parse(self, file_obj) -> Iterable[ParsedRow]: ...


class ImporterRegistry:
    def detect(self, filename, headers):
        matches = [parser for parser in self.parsers if parser.supports(filename, headers)]
        if len(matches) != 1:
            raise UnsupportedTemplateError("无法识别文件模板")
        return matches[0]
```

- [ ] **Step 5: Implement staged upload and atomic confirmation**

`stage_upload` must store the source file, create `StagedRow` objects, count valid/duplicate/error rows and keep the batch out of formal ledgers. `confirm_batch` must lock the batch, post only valid non-duplicate rows, leave bad rows visible, set status to `PARTIAL` when errors remain, and be idempotent on a second call.

```python
@transaction.atomic
def confirm_batch(batch_id, actor):
    batch = ImportBatch.objects.select_for_update().get(pk=batch_id)
    if batch.confirmed_at:
        return ImportResult.from_batch(batch)
    for row in batch.rows.select_for_update().filter(posted_at__isnull=True, is_duplicate=False):
        if row.issues:
            continue
        _post_normalized_row(batch, row)
        row.posted_at = timezone.now()
        row.save(update_fields=["posted_at"])
    batch.confirmed_at = timezone.now()
    batch.status = BatchStatus.PARTIAL if batch.error_rows else BatchStatus.COMPLETED
    batch.save(update_fields=["confirmed_at", "status"])
    record_audit(actor, "import.confirmed", batch, ImportResult.from_batch(batch).as_dict())
    return ImportResult.from_batch(batch)
```

- [ ] **Step 6: Run import engine tests**

Run: `docker compose run --rm web pytest tests/imports -q`

Expected: unknown template, duplicate file, bad row isolation, partial status, idempotent confirmation and rollback tests pass.

- [ ] **Step 7: Commit**

```bash
git add apps/imports tests/imports
git commit -m "feat: 实现导入暂存校验与确认引擎"
```

---

### Task 8: 税务平台销项与进项发票导入

**Files:**
- Create: `apps/imports/parsers/tax_invoice.py`
- Create: `apps/imports/parsers/excel.py`
- Create: `tests/imports/test_tax_invoice_parser.py`
- Create: `tests/fixtures/tax_input_invoices.xlsx`
- Create: `tests/fixtures/tax_output_invoices.xlsx`
- Modify: `apps/imports/registry.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `TaxInvoiceImporter.parse(file_obj) -> Iterable[ParsedRow]` for both invoice directions.
- Consumes: Task 7 normalized types and registry.

- [ ] **Step 1: Create sanitized fixtures from the known tax export schema**

Fixtures must include sheets `发票基础信息` and rows containing these columns: `发票号码`、`销售方纳税人识别号`、`销售方名称`、`购买方纳税人识别号`、`购买方名称`、`开票日期`、`价税合计`、`发票状态`.

- [ ] **Step 2: Write failing direction and amount tests**

```python
def test_company_as_buyer_creates_input_invoice(tax_input_fixture, settings):
    settings.COMPANY_TAX_ID = "91320281TEST000001"
    row = list(TaxInvoiceImporter().parse(tax_input_fixture))[0].normalized
    assert row.direction == InvoiceDirection.INPUT
    assert row.total_amount == Decimal("2000.00")


def test_company_as_seller_creates_output_invoice(tax_output_fixture, settings):
    settings.COMPANY_TAX_ID = "91320281TEST000001"
    row = list(TaxInvoiceImporter().parse(tax_output_fixture))[0].normalized
    assert row.direction == InvoiceDirection.OUTPUT
```

- [ ] **Step 3: Verify tests fail**

Run: `docker compose run --rm web pytest tests/imports/test_tax_invoice_parser.py -q`

Expected: FAIL because the tax importer does not exist.

- [ ] **Step 4: Implement column mapping, direction and status normalization**

```python
COLUMNS = {
    "invoice_number": "发票号码",
    "seller_tax_id": "销售方纳税人识别号",
    "seller_name": "销售方名称",
    "buyer_tax_id": "购买方纳税人识别号",
    "buyer_name": "购买方名称",
    "issue_date": "开票日期",
    "total_amount": "价税合计",
    "status": "发票状态",
}


def direction_for(seller_tax_id, buyer_tax_id, company_tax_id):
    if seller_tax_id == company_tax_id:
        return InvoiceDirection.OUTPUT
    if buyer_tax_id == company_tax_id:
        return InvoiceDirection.INPUT
    raise RowValidationError("发票购销双方均不是当前公司")
```

Normalize `正常`, `作废`, and red-letter statuses. Reject missing invoice number, invalid amount, invalid date, and unknown company direction with row-numbered issues.

- [ ] **Step 5: Test staging and confirmed ledger rows**

Run: `docker compose run --rm web pytest tests/imports/test_tax_invoice_parser.py tests/imports/test_services.py -q`

Expected: fixture rows stage correctly, duplicate invoice numbers are skipped, and confirmed rows preserve source row and raw payload.

- [ ] **Step 6: Commit**

```bash
git add apps/imports/parsers apps/imports/registry.py tests/imports/test_tax_invoice_parser.py tests/fixtures .env.example
git commit -m "feat: 支持税务平台销项进项发票导入"
```

---

### Task 9: 农业银行与微信资金流水导入

**Files:**
- Create: `apps/imports/parsers/agricultural_bank.py`
- Create: `apps/imports/parsers/wechat.py`
- Create: `apps/imports/parsers/csv_utils.py`
- Create: `tests/imports/test_agricultural_bank_parser.py`
- Create: `tests/imports/test_wechat_parser.py`
- Create: `tests/fixtures/agricultural_bank.xls`
- Create: `tests/fixtures/wechat_transactions.csv`
- Modify: `apps/imports/registry.py`

**Interfaces:**
- Produces: `AgriculturalBankImporter`, `WechatImporter` and deterministic money fingerprints.
- Consumes: Task 7 normalized transaction type and Task 4 funding accounts.

- [ ] **Step 1: Write failing Agricultural Bank tests**

```python
def test_bank_row_uses_income_or_expense_as_direction(bank_fixture):
    rows = [item.normalized for item in AgriculturalBankImporter().parse(bank_fixture)]
    assert rows[0].direction == MoneyDirection.OUTFLOW
    assert rows[0].amount == Decimal("2000.00")
    assert rows[0].counterparty_name == "测试铁路物流收款专户"
    assert rows[1].direction == MoneyDirection.INFLOW
```

- [ ] **Step 2: Write failing WeChat tests**

```python
def test_wechat_amount_strips_currency_symbol_and_preserves_ids(wechat_fixture):
    row = list(WechatImporter().parse(wechat_fixture))[0].normalized
    assert row.amount == Decimal("3200.00")
    assert row.transaction_id == "420000000000000001"
    assert row.channel == MoneyChannel.WECHAT
```

- [ ] **Step 3: Verify tests fail**

Run: `docker compose run --rm web pytest tests/imports/test_agricultural_bank_parser.py tests/imports/test_wechat_parser.py -q`

Expected: FAIL because both parsers are missing.

- [ ] **Step 4: Implement Agricultural Bank header and row parsing**

Recognize the exact header sequence:

```python
BANK_HEADERS = [
    "交易时间", "收入金额", "支出金额", "账户余额",
    "对方账号", "对方户名", "对方开户行", "摘要",
]
```

Require exactly one of income or expense to be positive, use `xlrd` for `.xls`, save the statement account from the metadata row, and create/update `AccountBalanceSnapshot` from the latest valid `账户余额`.

```python
def parse_bank_amount(income, expense):
    income_amount = parse_decimal(income)
    expense_amount = parse_decimal(expense)
    if (income_amount > 0) == (expense_amount > 0):
        raise RowValidationError("收入金额和支出金额必须且只能有一个大于零")
    if income_amount > 0:
        return MoneyDirection.INFLOW, income_amount
    return MoneyDirection.OUTFLOW, expense_amount
```

- [ ] **Step 5: Implement official WeChat CSV mapping**

Support these headers: `交易时间`、`交易类型`、`交易对方`、`商品`、`收/支`、`金额(元)`、`支付方式`、`当前状态`、`交易单号`、`商户单号`、`备注`.

Map `收入` to inflow and `支出` to outflow. Reject neutral records and non-success states from formal posting while preserving them in staging with an explanatory issue.

```python
WECHAT_DIRECTION = {"收入": MoneyDirection.INFLOW, "支出": MoneyDirection.OUTFLOW}
SUCCESS_STATES = {"支付成功", "已收钱", "已转账", "对方已收钱"}


def normalize_wechat_row(row):
    direction = WECHAT_DIRECTION.get(row["收/支"].strip())
    if direction is None:
        raise RowValidationError("微信记录不是收入或支出")
    if row["当前状态"].strip() not in SUCCESS_STATES:
        raise RowValidationError(f"微信交易尚未成功：{row['当前状态']}")
    return NormalizedTransactionRow(
        channel=MoneyChannel.WECHAT,
        direction=direction,
        occurred_at=parse_datetime(row["交易时间"]),
        amount=parse_decimal(row["金额(元)"].replace("¥", "")),
        balance_after=None,
        account_identifier=row["支付方式"].strip(),
        counterparty_name=row["交易对方"].strip(),
        counterparty_account="",
        transaction_id=row["交易单号"].strip(),
        summary=row["备注"].strip(),
    )
```

- [ ] **Step 6: Verify overlapping exports remain idempotent**

Create two fixtures with overlapping dates but different file hashes. Confirm both batches and assert duplicate transaction fingerprints are skipped rather than inserted twice.

Run: `docker compose run --rm web pytest tests/imports/test_agricultural_bank_parser.py tests/imports/test_wechat_parser.py tests/imports/test_fingerprints.py -q`.

- [ ] **Step 7: Commit**

```bash
git add apps/imports/parsers apps/imports/registry.py tests/imports tests/fixtures
git commit -m "feat: 支持银行与微信资金流水导入"
```

---

### Task 10: PDF 附件自动关联与历史迁移完整性

**Files:**
- Create: `apps/ledger/attachments.py`
- Create: `apps/ledger/migrations/0002_attachment.py`
- Modify: `apps/ledger/models.py`
- Create: `apps/imports/history.py`
- Modify: `apps/imports/models.py`
- Create: `apps/imports/migrations/0002_data_coverage.py`
- Create: `tests/ledger/test_attachments.py`
- Create: `tests/imports/test_history.py`
- Create: `tests/fixtures/invoice_00000000000000000001.pdf`

**Interfaces:**
- Produces: `attach_invoice_pdf(uploaded_file, actor)`, `Attachment`, `DataCoveragePeriod`, `coverage_matrix()`.
- Consumes: Task 4 invoices and Task 3 source-file storage.

- [ ] **Step 1: Write failing PDF link tests**

```python
@pytest.mark.django_db
def test_pdf_filename_links_unique_invoice(finance_user, invoice_2000, invoice_pdf):
    attachment = attach_invoice_pdf(invoice_pdf, actor=finance_user)
    assert attachment.target == invoice_2000
    assert attachment.status == AttachmentStatus.LINKED


@pytest.mark.django_db
def test_unknown_invoice_pdf_enters_claim_queue(finance_user, unknown_pdf):
    attachment = attach_invoice_pdf(unknown_pdf, actor=finance_user)
    assert attachment.status == AttachmentStatus.UNCLAIMED
```

- [ ] **Step 2: Verify tests fail**

Run: `docker compose run --rm web pytest tests/ledger/test_attachments.py tests/imports/test_history.py -q`

Expected: FAIL because attachment and coverage models are missing.

- [ ] **Step 3: Implement attachment storage and invoice-number extraction**

```python
class Attachment(ImmutableLedgerModel):
    file = models.FileField(upload_to="attachments/%Y/%m/")
    original_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=12, choices=AttachmentStatus.choices)
    content_type = models.ForeignKey(ContentType, null=True, blank=True, on_delete=models.PROTECT)
    object_id = models.UUIDField(null=True, blank=True)
    target = GenericForeignKey("content_type", "object_id")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    disabled_at = models.DateTimeField(null=True, blank=True)


INVOICE_NUMBER_PATTERN = re.compile(r"(?<!\d)\d{20}(?!\d)")


def extract_invoice_numbers(filename: str, file_obj) -> set[str]:
    numbers = set(INVOICE_NUMBER_PATTERN.findall(filename))
    if not numbers:
        reader = PdfReader(file_obj)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        numbers.update(INVOICE_NUMBER_PATTERN.findall(text))
    file_obj.seek(0)
    return numbers
```

Store SHA-256, original name, uploader, timestamp and soft-disabled state. Link automatically only when exactly one imported invoice matches exactly one extracted number.

- [ ] **Step 4: Implement yearly coverage tracking**

```python
class DataCoveragePeriod(UUIDModel):
    year = models.PositiveSmallIntegerField()
    source_kind = models.CharField(max_length=20, choices=SourceKind.choices)
    status = models.CharField(max_length=12, choices=CoverageStatus.choices)
    expected_start = models.DateField()
    expected_end = models.DateField()
    actual_start = models.DateField(null=True, blank=True)
    actual_end = models.DateField(null=True, blank=True)
    missing_notes = models.TextField(blank=True)
```

`coverage_matrix()` must return every year/source combination and explicitly mark missing or partial periods. It must not infer reconciliations from missing periods.

- [ ] **Step 5: Run tests**

Run: `docker compose run --rm web pytest tests/ledger/test_attachments.py tests/imports/test_history.py -q`

Expected: filename extraction, PDF text fallback, ambiguous match, duplicate PDF, soft-disable, full/partial/missing coverage tests pass.

- [ ] **Step 6: Commit**

```bash
git add apps/ledger apps/imports tests/ledger tests/imports tests/fixtures
git commit -m "feat: 增加发票附件与历史资料完整性"
```

---

### Task 11: 基础界面、导入中心与台账查询

**Files:**
- Create: `apps/core/context_processors.py`
- Create: `apps/accounts/decorators.py`
- Create: `apps/imports/forms.py`
- Create: `apps/imports/views.py`
- Create: `apps/imports/urls.py`
- Create: `apps/ledger/views.py`
- Create: `apps/ledger/urls.py`
- Create: `apps/parties/views.py`
- Create: `apps/parties/urls.py`
- Create: `templates/base.html`
- Create: `templates/registration/login.html`
- Create: `templates/imports/index.html`
- Create: `templates/imports/preview.html`
- Create: `templates/ledger/invoice_list.html`
- Create: `templates/ledger/transaction_list.html`
- Create: `templates/parties/list.html`
- Create: `static/css/app.css`
- Create: `static/vendor/lucide.min.js`
- Create: `package.json`
- Create: `package-lock.json`
- Create: `tests/web/test_import_views.py`
- Create: `tests/web/test_ledger_views.py`
- Modify: `config/urls.py`

**Interfaces:**
- Produces: authenticated navigation, upload/preview/confirm flow, searchable invoices, transactions and counterparties.
- Consumes: Tasks 7-10 services and Task 2 roles.

- [ ] **Step 1: Write failing permission and import workflow tests**

```python
@pytest.mark.django_db
def test_owner_cannot_post_import(owner_client, invoice_file):
    response = owner_client.post("/imports/", {"source_kind": "input_invoice", "file": invoice_file})
    assert response.status_code == 403


@pytest.mark.django_db
def test_finance_sees_preview_before_confirm(finance_client, invoice_file):
    response = finance_client.post("/imports/", {"source_kind": "input_invoice", "file": invoice_file})
    assert response.status_code == 302
    preview = finance_client.get(response.headers["Location"])
    assert "有效数据" in preview.content.decode()
    assert "确认导入" in preview.content.decode()
```

- [ ] **Step 2: Verify view tests fail**

Run: `docker compose run --rm web pytest tests/web/test_import_views.py tests/web/test_ledger_views.py -q`

Expected: FAIL because URLs and templates do not exist.

- [ ] **Step 3: Implement role decorators and app navigation**

`finance_required` must return HTTP 403 for authenticated non-finance users. `owner_or_finance_required` allows both roles. Base navigation contains: 总览、导入中心、人工核销、结算批次、应收应付、往来单位、操作记录.

```python
def finance_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not user_has_role(request.user, Role.FINANCE):
            raise PermissionDenied("仅财务可以执行此操作")
        return view(request, *args, **kwargs)
    return wrapped
```

- [ ] **Step 4: Implement upload, preview and confirm views**

```python
@finance_required
def upload(request):
    form = ImportUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        batch = stage_upload(
            form.cleaned_data["file"],
            source_kind=form.cleaned_data["source_kind"],
            actor=request.user,
        )
        return redirect("imports:preview", pk=batch.pk)
    return render(request, "imports/index.html", {"form": form, "recent_batches": recent_batches()})
```

Preview must show valid, duplicate and error counts, source period, row-level issue download and original file link. Confirmation is a separate POST protected by CSRF.

- [ ] **Step 5: Implement ledger filters and pagination**

Invoice filters: direction, counterparty, issue-date range, status, reconciliation state and invoice number. Transaction filters: channel, direction, counterparty, date range, open amount and transaction ID. Default page size is 50 and query parameters persist across pagination.

```python
def invoice_list(request):
    query = Invoice.objects.select_related("counterparty", "import_batch").order_by("-issue_date", "-id")
    if value := request.GET.get("direction"):
        query = query.filter(direction=value)
    if value := request.GET.get("counterparty"):
        query = query.filter(counterparty_id=value)
    if value := request.GET.get("invoice_number"):
        query = query.filter(invoice_number__icontains=value.strip())
    page = Paginator(query, 50).get_page(request.GET.get("page"))
    return render(request, "ledger/invoice_list.html", {"page": page, "querystring": request.GET.urlencode()})
```

- [ ] **Step 6: Apply the approved compact visual system**

Use fixed-height toolbar controls, tables with stable columns, 7px-or-less panel radii, no nested cards, `font-size` independent of viewport width, and responsive two-column-to-one-column behavior below 900px. Use locally bundled Lucide icons for upload, search, download, undo and view actions.

```json
{
  "private": true,
  "scripts": {
    "vendor": "mkdir -p static/vendor && cp node_modules/lucide/dist/umd/lucide.min.js static/vendor/lucide.min.js"
  },
  "dependencies": {
    "lucide": "0.468.0"
  }
}
```

```css
.toolbar { display:grid; grid-template-columns:180px 220px 1fr auto; gap:8px; min-height:42px; }
.panel { border:1px solid var(--border); border-radius:7px; background:#fff; }
.data-table { width:100%; table-layout:fixed; border-collapse:collapse; }
.icon-button { width:36px; height:36px; display:inline-grid; place-items:center; }
@media (max-width:900px) {
  .toolbar { grid-template-columns:1fr 1fr; }
  .reconciliation-layout { grid-template-columns:1fr; }
}
```

- [ ] **Step 7: Run tests and inspect rendered HTML**

Run: `docker compose run --rm web pytest tests/web/test_import_views.py tests/web/test_ledger_views.py -q`.

Expected: finance flows pass, owner writes return 403, filters preserve query parameters and all pages require authentication.

- [ ] **Step 8: Commit**

```bash
git add apps/accounts apps/core apps/imports apps/ledger apps/parties config/urls.py templates static/css static/vendor package.json package-lock.json tests/web
git commit -m "feat: 建立导入中心与财务台账界面"
```

---

### Task 12: 人工核销工作台与结算批次页面

**Files:**
- Create: `apps/reconciliation/forms.py`
- Create: `apps/reconciliation/views.py`
- Create: `apps/reconciliation/urls.py`
- Create: `apps/reconciliation/presenters.py`
- Create: `templates/reconciliation/workbench.html`
- Create: `templates/reconciliation/settlement_list.html`
- Create: `templates/reconciliation/settlement_detail.html`
- Create: `templates/reconciliation/reversal_form.html`
- Create: `static/js/reconciliation-workbench.js`
- Create: `tests/web/test_reconciliation_views.py`
- Create: `tests/web/test_settlement_views.py`
- Modify: `config/urls.py`

**Interfaces:**
- Produces: finance-only direct reconciliation, candidate JSON, settlement draft/confirm/reverse pages.
- Consumes: Tasks 5-6 services and Task 11 layout.

- [ ] **Step 1: Write failing workbench tests**

```python
@pytest.mark.django_db
def test_workbench_shows_selected_totals_and_difference(finance_client, invoice_46050, june_payments_without_2000):
    response = finance_client.get(f"/reconciliation/workbench/?invoice={invoice_46050.id}")
    body = response.content.decode()
    assert "46,050.00" in body
    assert "45,050.00" in body
    assert "1,000.00" in body


@pytest.mark.django_db
def test_owner_cannot_confirm_reconciliation(owner_client, reconciliation_payload):
    response = owner_client.post("/reconciliation/confirm/", reconciliation_payload)
    assert response.status_code == 403
```

- [ ] **Step 2: Verify view tests fail**

Run: `docker compose run --rm web pytest tests/web/test_reconciliation_views.py tests/web/test_settlement_views.py -q`

Expected: FAIL because workbench views do not exist.

- [ ] **Step 3: Implement workbench filters and candidate endpoint**

The page must keep invoice selection on the left and money selection on the right. `GET /reconciliation/candidates/?invoice=<uuid>&start=YYYY-MM-DD&end=YYYY-MM-DD` returns candidate kind, transaction IDs, amount, date range and difference. It must never return balance snapshots.

```python
@finance_required
def candidate_list(request):
    invoice_id = UUID(request.GET["invoice"])
    start = date.fromisoformat(request.GET["start"])
    end = date.fromisoformat(request.GET["end"])
    items = transaction_candidates(invoice_id, start=start, end=end)
    return JsonResponse({"items": [candidate_to_dict(item) for item in items]})
```

- [ ] **Step 4: Implement browser-side selection math**

```javascript
export function calculateAllocation(invoiceAmount, selectedTransactions) {
  const selected = selectedTransactions.reduce((sum, item) => sum + item.availableCents, 0);
  const allocated = Math.min(invoiceAmount, selected);
  return { selected, allocated, difference: invoiceAmount - allocated };
}
```

Represent amounts as integer cents in JavaScript. The confirm button stays disabled for zero selections, invalid allocations, stale record versions or duplicate transaction IDs. Partial reconciliation requires a visible confirmation sentence containing the remaining amount.

- [ ] **Step 5: Implement settlement draft and confirmation pages**

Draft creation accepts counterparty, direction, period start and period end. The detail page shows invoice total, money total, allocated amount, unallocated amounts and records occupied elsewhere. Confirmation posts explicit allocation rows and current batch version to `confirm_settlement_batch`.

```python
@finance_required
@require_POST
def settlement_confirm(request, pk):
    form = SettlementConfirmForm(request.POST)
    if not form.is_valid():
        batch = get_object_or_404(SettlementBatch, pk=pk)
        return render(
            request,
            "reconciliation/settlement_detail.html",
            {"batch": batch, "form": form},
            status=400,
        )
    reconciliation = confirm_settlement_batch(
        batch_id=pk,
        actor=request.user,
        version=form.cleaned_data["version"],
        allocations=form.allocation_inputs(),
    )
    return redirect("reconciliation:detail", pk=reconciliation.pk)
```

- [ ] **Step 6: Implement reversal page**

Require a non-empty reason, show original allocation lines, and call `reverse_reconciliation`. The original page remains accessible with a “已撤销” status and link to the reversal record.

```python
@finance_required
@require_POST
def reverse(request, pk):
    form = ReversalForm(request.POST)
    if form.is_valid():
        reverse_reconciliation(
            actor=request.user,
            reconciliation_id=pk,
            reason=form.cleaned_data["reason"],
        )
        return redirect("reconciliation:detail", pk=pk)
    original = get_object_or_404(Reconciliation, pk=pk)
    return render(request, "reconciliation/reversal_form.html", {"form": form, "original": original}, status=400)
```

- [ ] **Step 7: Run tests**

Run: `docker compose run --rm web pytest tests/web/test_reconciliation_views.py tests/web/test_settlement_views.py tests/reconciliation -q`

Expected: direct, partial, batch, owner denial, stale version, duplicate use and reversal UI tests pass.

- [ ] **Step 8: Commit**

```bash
git add apps/reconciliation config/urls.py templates/reconciliation static/js/reconciliation-workbench.js tests/web
git commit -m "feat: 完成人工核销与结算批次页面"
```

---

### Task 13: 应收应付、异常清单与老板 Dashboard

**Files:**
- Create: `apps/reporting/apps.py`
- Create: `apps/reporting/queries.py`
- Create: `apps/reporting/dashboard.py`
- Create: `apps/reporting/views.py`
- Create: `apps/reporting/urls.py`
- Create: `templates/reporting/exceptions.html`
- Create: `templates/reporting/receivables.html`
- Create: `templates/reporting/payables.html`
- Create: `templates/reporting/dashboard.html`
- Create: `static/js/dashboard.js`
- Create: `static/vendor/echarts.min.js`
- Modify: `package.json`
- Modify: `package-lock.json`
- Create: `tests/reporting/test_queries.py`
- Create: `tests/reporting/test_dashboard.py`
- Create: `tests/web/test_reporting_views.py`
- Modify: `config/settings/base.py`
- Modify: `config/urls.py`

**Interfaces:**
- Produces: `receivables_as_of(date)`, `payables_as_of(date)`, `exception_items(as_of)`, `dashboard_payload(month)` and owner dashboard routes.
- Consumes: Tasks 4-6 ledgers and allocation queries.

- [ ] **Step 1: Write failing report tests**

```python
@pytest.mark.django_db
def test_receivables_use_open_invoice_amount_not_account_balance(output_invoice, partial_receipt, unrelated_balance_snapshot):
    rows = receivables_as_of(date(2026, 7, 28))
    row = next(item for item in rows if item.invoice_id == output_invoice.id)
    assert row.open_amount == output_invoice.total_amount - partial_receipt.amount


@pytest.mark.django_db
def test_dashboard_marks_partial_import_warning(partial_import_batch):
    payload = dashboard_payload(date(2026, 7, 1))
    assert payload.data_incomplete is True
    assert partial_import_batch.id in payload.incomplete_batch_ids
```

- [ ] **Step 2: Verify report tests fail**

Run: `docker compose run --rm web pytest tests/reporting tests/web/test_reporting_views.py -q`

Expected: FAIL because reporting services are missing.

- [ ] **Step 3: Implement receivable, payable and aging queries**

Use invoice direction and active allocations only. Aging base date is `due_date` when present, otherwise `issue_date`. Return buckets `0-30`, `31-60`, `61-90`, `90+`. Do not read account balance snapshots for invoice open amounts.

```python
@dataclass(frozen=True)
class OpenInvoiceRow:
    invoice_id: UUID
    counterparty_name: str
    issue_date: date
    due_date: date | None
    open_amount: Decimal
    aging_bucket: str


def aging_bucket(invoice, as_of):
    base = invoice.due_date or invoice.issue_date
    days = max((as_of - base).days, 0)
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


def _open_invoice_rows(direction, as_of):
    rows = []
    invoices = Invoice.objects.filter(
        direction=direction,
        issue_date__lte=as_of,
        status=InvoiceStatus.NORMAL,
    ).select_related("counterparty")
    for invoice in invoices:
        open_amount = invoice_open_amount(invoice.id)
        if open_amount > 0:
            rows.append(OpenInvoiceRow(
                invoice_id=invoice.id,
                counterparty_name=invoice.counterparty.name,
                issue_date=invoice.issue_date,
                due_date=invoice.due_date,
                open_amount=open_amount,
                aging_bucket=aging_bucket(invoice, as_of),
            ))
    return tuple(rows)


def receivables_as_of(as_of):
    return _open_invoice_rows(InvoiceDirection.OUTPUT, as_of)


def payables_as_of(as_of):
    return _open_invoice_rows(InvoiceDirection.INPUT, as_of)
```

- [ ] **Step 4: Implement exception classification**

Return explicit types for: output invoice with open amount, input invoice with open amount, unmatched inflow, unmatched outflow, unknown counterparty, partial reconciliation difference, duplicate import, red invoice with active allocation, stale open item and incomplete history coverage.

```python
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
```

- [ ] **Step 5: Implement dashboard payload**

```python
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
```

Current funds may use the latest balance snapshot for each funding account because it is a dashboard metric. That value must not be exposed to candidate or allocation services.

- [ ] **Step 6: Bundle ECharts and Lucide locally**

Extend `package.json` with pinned ECharts while preserving the Task 11 Lucide dependency. The production browser must load only local static URLs.

```json
{
  "private": true,
  "scripts": {
    "vendor": "mkdir -p static/vendor && cp node_modules/echarts/dist/echarts.min.js static/vendor/echarts.min.js && cp node_modules/lucide/dist/umd/lucide.min.js static/vendor/lucide.min.js"
  },
  "dependencies": {
    "echarts": "5.6.0",
    "lucide": "0.468.0"
  }
}
```

- [ ] **Step 7: Implement dashboard charts and role restrictions**

Use the approved layout: four KPI blocks, daily inflow/outflow line chart, receivable aging doughnut chart, payable due bar chart and a risk list. Use `json_script` to serialize chart data safely. Owners can view dashboard and drill-down summaries; they cannot access import, reconciliation, full account identifiers or restricted attachments.

```html
{{ dashboard_chart_data|json_script:"dashboard-data" }}
<div id="cashflow-chart" class="chart" aria-label="每日资金流入与流出"></div>
<div id="receivable-aging-chart" class="chart" aria-label="应收账龄分布"></div>
<div id="payable-due-chart" class="chart" aria-label="应付到期分布"></div>
<script src="{% static 'vendor/echarts.min.js' %}"></script>
<script src="{% static 'js/dashboard.js' %}" defer></script>
```

```javascript
const data = JSON.parse(document.getElementById('dashboard-data').textContent);
const cashflow = echarts.init(document.getElementById('cashflow-chart'));
cashflow.setOption({
  tooltip: { trigger: 'axis' },
  legend: { data: ['收款', '付款'] },
  xAxis: { type: 'category', data: data.dailyCashflow.map(item => item.date) },
  yAxis: { type: 'value' },
  series: [
    { name: '收款', type: 'line', data: data.dailyCashflow.map(item => item.inflow) },
    { name: '付款', type: 'line', data: data.dailyCashflow.map(item => item.outflow) }
  ]
});
```

- [ ] **Step 8: Run tests**

Run: `docker compose run --rm web pytest tests/reporting tests/web/test_reporting_views.py -q`.

Expected: report math, aging boundaries, incomplete-data warning, role permissions and dashboard JSON tests pass.

- [ ] **Step 9: Commit**

```bash
git add apps/reporting config package.json package-lock.json templates/reporting static/js/dashboard.js static/vendor tests/reporting tests/web
git commit -m "feat: 增加应收应付异常与老板看板"
```

---

### Task 14: Excel 导出、安全加固、备份与 DSM 部署

**Files:**
- Create: `apps/reporting/exports.py`
- Create: `apps/core/masking.py`
- Create: `tests/reporting/test_exports.py`
- Create: `tests/core/test_masking.py`
- Create: `scripts/backup.sh`
- Create: `scripts/restore.sh`
- Create: `docs/deployment-dsm.md`
- Modify: `Dockerfile`
- Modify: `compose.yml`
- Modify: `.env.example`
- Modify: `config/settings/prod.py`

**Interfaces:**
- Produces: finance Excel exports, masked identifiers, production container, backup and restore commands.
- Consumes: all prior domain and reporting services.

- [ ] **Step 1: Write failing export and masking tests**

```python
def test_mask_account_keeps_only_last_four_digits():
    assert mask_account("121902307610001") == "***********0001"


@pytest.mark.django_db
def test_reconciliation_export_contains_source_links(finance_user, reconciliation):
    workbook = build_reconciliation_export([reconciliation.id], actor=finance_user)
    sheet = workbook["核销明细"]
    assert sheet["A2"].value == str(reconciliation.id)
    assert sheet["H2"].value == reconciliation.allocations.first().invoice.invoice_number
```

- [ ] **Step 2: Verify tests fail**

Run: `docker compose run --rm web pytest tests/reporting/test_exports.py tests/core/test_masking.py -q`

Expected: FAIL because export and masking helpers are missing.

- [ ] **Step 3: Implement styled Excel exports**

Use openpyxl to produce separate sheets for 应收、应付、未匹配资金、核销明细 and 导入异常. Set explicit widths, freeze row 1, use `0.00` amount formatting, include source batch/file IDs, and reject owner requests for exports containing full account identifiers.

```python
def reconciliation_export_rows(reconciliation_ids):
    allocations = ReconciliationAllocation.objects.filter(
        reconciliation_id__in=reconciliation_ids,
    ).select_related(
        "reconciliation", "invoice", "transaction", "transaction__counterparty", "transaction__import_batch"
    )
    for allocation in allocations.iterator():
        yield [
            str(allocation.reconciliation_id),
            allocation.reconciliation.created_at,
            allocation.invoice.counterparty.name,
            allocation.transaction.occurred_at,
            allocation.transaction.amount,
            allocation.transaction.channel,
            str(allocation.transaction.import_batch_id),
            allocation.invoice.invoice_number,
            allocation.invoice.total_amount,
            allocation.amount,
        ]


def build_reconciliation_export(reconciliation_ids, actor):
    if not user_has_role(actor, Role.FINANCE):
        raise PermissionDenied("仅财务可以导出完整核销明细")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "核销明细"
    sheet.freeze_panes = "A2"
    sheet.append(["核销ID", "核销日期", "单位", "资金日期", "资金金额", "渠道", "来源批次", "发票号码", "发票金额", "分配金额"])
    for row in reconciliation_export_rows(reconciliation_ids):
        sheet.append(row)
    for column in ("E", "I", "J"):
        for cell in sheet[column][1:]:
            cell.number_format = "0.00"
    return workbook
```

- [ ] **Step 4: Harden production settings**

Require `SECRET_KEY`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, strong PostgreSQL credentials and `DEBUG=false`. Enable secure cookies behind DSM HTTPS proxy, WhiteNoise static files, upload size limits, PDF/Excel/CSV extension plus content-signature validation, and non-root container user.

```python
DEBUG = False
SECRET_KEY = os.environ["SECRET_KEY"]
ALLOWED_HOSTS = [item for item in os.environ["ALLOWED_HOSTS"].split(",") if item]
CSRF_TRUSTED_ORIGINS = [item for item in os.environ["CSRF_TRUSTED_ORIGINS"].split(",") if item]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
```

```python
ALLOWED_UPLOAD_SIGNATURES = {
    ".pdf": (b"%PDF-",),
    ".xlsx": (b"PK\x03\x04",),
    ".xls": (bytes.fromhex("D0CF11E0A1B11AE1"),),
}


def validate_upload_signature(upload):
    suffix = Path(upload.name).suffix.lower()
    if suffix == ".csv":
        return
    signatures = ALLOWED_UPLOAD_SIGNATURES.get(suffix)
    head = upload.read(8)
    upload.seek(0)
    if not signatures or not any(head.startswith(signature) for signature in signatures):
        raise ValidationError("文件扩展名与实际内容不一致")
```

- [ ] **Step 5: Implement deterministic backup and restore scripts**

```bash
#!/usr/bin/env sh
set -eu
stamp="$(date +%Y%m%d-%H%M%S)"
mkdir -p /data/backups
pg_dump "$DATABASE_URL" --format=custom --file="/data/backups/db-$stamp.dump"
tar -C /data -czf "/data/backups/uploads-$stamp.tar.gz" uploads
find /data/backups -type f -mtime +30 -delete
```

`restore.sh` must require an explicit dump path and uploads archive path, refuse to run when either is missing, restore into a named target database, and never overwrite production without `CONFIRM_RESTORE=YES`.

```bash
#!/usr/bin/env sh
set -eu
dump_path="${1:-}"
uploads_path="${2:-}"
target_database="${RESTORE_DATABASE_URL:-}"
if [ -z "$dump_path" ] || [ -z "$uploads_path" ] || [ -z "$target_database" ]; then
  echo "用法: RESTORE_DATABASE_URL=... restore.sh <dump> <uploads.tar.gz>" >&2
  exit 2
fi
if [ "${CONFIRM_RESTORE:-NO}" != "YES" ]; then
  echo "必须设置 CONFIRM_RESTORE=YES" >&2
  exit 3
fi
pg_restore --clean --if-exists --no-owner --dbname="$target_database" "$dump_path"
mkdir -p /data/uploads
tar -C /data -xzf "$uploads_path"
```

- [ ] **Step 6: Finalize production Compose and DSM guide**

Production `web` command: `gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 120`. Map DSM directories for PostgreSQL, uploads, exports and backups. Document DSM reverse proxy HTTPS, environment creation, initial superuser, daily Task Scheduler command, update, rollback and restore drill.

```yaml
services:
  web:
    command: gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 120
    restart: unless-stopped
    volumes:
      - /volume1/docker/shunda-finance/uploads:/data/uploads
      - /volume1/docker/shunda-finance/exports:/data/exports
      - /volume1/docker/shunda-finance/backups:/data/backups
  db:
    restart: unless-stopped
    volumes:
      - /volume1/docker/shunda-finance/postgres:/var/lib/postgresql/data
```

The deployment guide must use these verified operations:

```bash
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web /app/scripts/backup.sh
docker compose exec web /app/scripts/restore.sh /data/backups/db-test.dump /data/backups/uploads-test.tar.gz
```

- [ ] **Step 7: Run tests and container checks**

Run: `docker compose run --rm web pytest tests/reporting/test_exports.py tests/core/test_masking.py -q && docker compose config && docker compose build web`.

Expected: tests pass, Compose config is valid, and production image builds.

- [ ] **Step 8: Commit**

```bash
git add apps/reporting/exports.py apps/core/masking.py tests/reporting/test_exports.py tests/core/test_masking.py scripts Dockerfile compose.yml .env.example config/settings/prod.py docs/deployment-dsm.md
git commit -m "feat: 完成导出安全备份与群晖部署"
```

---

### Task 15: 测试铁路物流端到端验收、覆盖率与响应式 QA

**Files:**
- Create: `tests/fixtures/synthetic_railway/input_invoices.xlsx`
- Create: `tests/fixtures/synthetic_railway/bank_june.xls`
- Create: `tests/e2e/test_synthetic_railway_workflow.py`
- Create: `tests/e2e/import-and-reconcile.spec.ts`
- Create: `playwright.config.ts`
- Create: `docs/acceptance-results.md`
- Modify: `package.json`

**Interfaces:**
- Produces: executable acceptance evidence for imports, manual reconciliation, dashboard, permissions and responsive UI.
- Consumes: complete application from Tasks 1-14.

- [ ] **Step 1: Create sanitized regression fixtures**

The bank fixture contains the 13 June railway payments totaling `47,050.00`. The invoice fixture contains invoices `46,050.00` and `2,000.00`, both dated `2026-07-07`. Preserve dates and amounts but replace bank account identifiers not required by the assertion.

- [ ] **Step 2: Write the failing domain acceptance test**

```python
@pytest.mark.django_db(transaction=True)
def test_synthetic_railway_june_batch_never_uses_balance_to_hide_1000_difference(finance_user, synthetic_railway_files):
    import_and_confirm(synthetic_railway_files, actor=finance_user)
    invoice_2000 = Invoice.objects.get(total_amount=Decimal("2000.00"))
    payment_2000 = MoneyTransaction.objects.get(occurred_at__date=date(2026, 6, 16))
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(invoice_2000.id, payment_2000.id, Decimal("2000.00"))],
    )
    invoice_46050 = Invoice.objects.get(total_amount=Decimal("46050.00"))
    remaining_june = MoneyTransaction.objects.filter(occurred_at__month=6).exclude(pk=payment_2000.pk)
    assert sum(transaction_open_amount(item.id) for item in remaining_june) == Decimal("45050.00")
    assert invoice_open_amount(invoice_46050.id) == Decimal("46050.00")
    assert Decimal("46050.00") - Decimal("45050.00") == Decimal("1000.00")
```

- [ ] **Step 3: Verify the acceptance test fails before final fixture wiring**

Run: `docker compose run --rm web pytest tests/e2e/test_synthetic_railway_workflow.py -q`

Expected: FAIL until fixture import helpers and final batch allocation are wired.

- [ ] **Step 4: Complete the end-to-end batch allocation and assertions**

Allocate the remaining June payments only up to `45,050.00` against the `46,050.00` invoice. Assert the invoice remains partially reconciled with `1,000.00`, the June 16 payment cannot be reused, and `AccountBalanceSnapshot(50000.00)` does not change either result.

```python
remaining_allocations = [
    AllocationInput(invoice_46050.id, payment.id, transaction_open_amount(payment.id))
    for payment in remaining_june.order_by("occurred_at")
]
create_reconciliation(
    actor=finance_user,
    direction=ReconciliationDirection.PURCHASE_PAYMENT,
    allocations=remaining_allocations,
    note="测试铁路物流 2026 年 6 月结算，尚差 1,000.00 元",
)
assert invoice_open_amount(invoice_46050.id) == Decimal("1000.00")
with pytest.raises(ValidationError, match="资金可核销金额不足"):
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(invoice_46050.id, payment_2000.id, Decimal("0.01"))],
    )
AccountBalanceSnapshot.objects.create(
    account=payment_2000.account,
    as_of=datetime(2026, 6, 30, 23, 59, tzinfo=UTC),
    balance=Decimal("50000.00"),
    source_batch=payment_2000.import_batch,
)
assert invoice_open_amount(invoice_46050.id) == Decimal("1000.00")
```

- [ ] **Step 5: Add Playwright finance and owner workflows**

Extend `package.json` before writing the browser test:

```json
{
  "scripts": {
    "test:e2e": "playwright test"
  },
  "devDependencies": {
    "@playwright/test": "1.51.1"
  }
}
```

```typescript
test('finance imports and partially reconciles while owner stays read-only', async ({ page }) => {
  await loginAsFinance(page);
  await importFixture(page, 'input_invoices.xlsx', '进项发票');
  await importFixture(page, 'bank_june.xls', '银行流水');
  await page.goto('/reconciliation/workbench/');
  await expect(page.getByText('剩余未核')).toBeVisible();
  await expect(page.getByText('1,000.00')).toBeVisible();
  await logout(page);
  await loginAsOwner(page);
  await page.goto('/dashboard/');
  await expect(page.getByText('顺达财务驾驶舱')).toBeVisible();
  await expect(page.getByRole('link', { name: '导入中心' })).toHaveCount(0);
});
```

- [ ] **Step 6: Run responsive screenshots and overlap checks**

Run Playwright at `1440x900`, `1024x768`, `390x844`, and `360x800`. Capture dashboard, import center and reconciliation workbench screenshots. Assert no horizontal overflow on the page root, no clipped button text, no overlapping toolbar controls and no blank ECharts canvases.

```typescript
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 1024, height: 768 },
  { width: 390, height: 844 },
  { width: 360, height: 800 },
]) {
  test(`dashboard fits ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await loginAsOwner(page);
    await page.goto('/dashboard/');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBe(0);
    await expect(page.locator('#cashflow-chart canvas')).toBeVisible();
    const pixels = await page.locator('#cashflow-chart').screenshot();
    expect(pixels.byteLength).toBeGreaterThan(1000);
  });
}
```

- [ ] **Step 7: Run the full quality gate**

Run:

```bash
docker compose run --rm web ruff check .
docker compose run --rm web pytest --cov=apps --cov-report=term-missing --cov-fail-under=80
npm ci
npx playwright test
docker compose config
docker compose build web
```

Expected: Ruff clean, all tests pass, coverage at least 80%, all Playwright projects pass, Compose validates and the production image builds.

- [ ] **Step 8: Record acceptance evidence**

In `docs/acceptance-results.md`, record exact commands, test counts, coverage percentage, screenshot paths, Compose validation result, image tag and the 测试铁路物流 `1,000.00` remaining difference assertion.

- [ ] **Step 9: Commit**

```bash
git add tests/fixtures/synthetic_railway tests/e2e playwright.config.ts package.json package-lock.json docs/acceptance-results.md
git commit -m "test: 完成测试铁路物流端到端验收"
```

---

## Completion Gate

Implementation is complete only when all conditions below are true:

- All 15 task commits exist and contain only their declared files.
- `ruff check .` passes.
- `pytest --cov-fail-under=80` passes against PostgreSQL.
- Playwright passes at all four required viewports with nonblank charts and no overflow.
- Re-importing identical and overlapping files creates no duplicate formal records.
- 测试铁路物流 regression leaves exactly `1,000.00` unmatched after allocating the documented June payments and never uses account balance to close the difference.
- Finance and owner permissions are verified through both Django tests and browser tests.
- Production Compose builds and validates.
- A backup is created and restored successfully in a non-production test directory.
- `docs/deployment-dsm.md` and `docs/acceptance-results.md` contain the exact verified commands.
