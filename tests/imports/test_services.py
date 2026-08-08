from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError

from apps.accounts.roles import Role
from apps.core.models import AuditLog
from apps.imports.choices import SourceKind
from apps.imports.models import ImportBatch, SourceFile, StagedRow
from apps.imports.services import (
    DuplicateSourceFileError,
    confirm_batch,
    stage_upload,
)
from apps.imports.types import RowValidationError
from apps.ledger.choices import InvoiceStatus, MoneyChannel, MoneyDirection
from apps.ledger.models import (
    AccountBalanceSnapshot,
    FundingAccount,
    Invoice,
    MoneyTransaction,
)
from apps.parties.models import AliasKind, Counterparty, CounterpartyAlias
from apps.reconciliation.choices import ReconciliationDirection
from apps.reconciliation.models import ReconciliationAllocation
from apps.reconciliation.services import AllocationInput, create_reconciliation
from apps.reporting.queries import ExceptionType, exception_items
from tests.builders import make_transaction
from tests.imports.fakes import FakeImporter, make_invoice_row, make_transaction_row


@pytest.fixture
def fake_import_file():
    return SimpleUploadedFile("input.csv", b"marker\nvalue\n")


@pytest.fixture
def fake_importer():
    return FakeImporter()


@pytest.fixture
def fake_registry(fake_importer):
    from apps.imports.registry import ImporterRegistry

    return ImporterRegistry([fake_importer])


@pytest.fixture
def invoice_party():
    return Counterparty.objects.create(
        name="测试供应商",
        normalized_name="测试供应商",
        tax_id="913200",
        is_supplier=True,
    )


@pytest.fixture
def transaction_party():
    return Counterparty.objects.create(
        name="测试收款方",
        normalized_name="测试收款方",
        is_supplier=True,
    )


@pytest.fixture
def funding_account():
    return FundingAccount.objects.create(
        channel=MoneyChannel.BANK,
        name="测试银行账户",
        identifier="bank-account",
        masked_identifier="********ount",
    )


@pytest.mark.django_db
def test_same_file_hash_cannot_create_second_source_file(
    finance_user, fake_import_file, fake_registry
):
    first = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    fake_import_file.seek(0)
    with pytest.raises(DuplicateSourceFileError):
        stage_upload(
            fake_import_file,
            source_kind=SourceKind.INPUT_INVOICE,
            actor=finance_user,
            registry=fake_registry,
        )

    assert first.source_files.count() == 1


@pytest.mark.django_db
def test_stage_upload_rewinds_file_before_detection_and_after_parsing(
    finance_user, fake_import_file, fake_importer, fake_registry
):
    fake_import_file.read(1)
    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    assert batch.pk is not None
    assert fake_importer.parse_positions == [0]
    assert fake_import_file.tell() == 0


@pytest.mark.django_db
def test_stage_upload_rejects_invalid_signature_before_parser(
    finance_user, fake_registry
):
    uploaded = SimpleUploadedFile("forged.xlsx", b"not-an-xlsx")

    with pytest.raises(ValidationError, match="文件扩展名与实际内容不一致"):
        stage_upload(
            uploaded,
            source_kind=SourceKind.INPUT_INVOICE,
            actor=finance_user,
            registry=fake_registry,
        )

    assert uploaded.tell() == 0
    assert not ImportBatch.objects.exists()


@pytest.mark.django_db
def test_stage_upload_enforces_actual_byte_limit_when_declared_size_is_wrong(
    finance_user, fake_registry, settings
):
    settings.IMPORT_MAX_UPLOAD_BYTES = 4
    uploaded = SimpleUploadedFile("large.csv", b"marker\n")
    uploaded.size = 1

    with pytest.raises(ValidationError, match="文件大小超过系统允许的上限"):
        stage_upload(
            uploaded,
            source_kind=SourceKind.INPUT_INVOICE,
            actor=finance_user,
            registry=fake_registry,
        )

    assert uploaded.tell() == 0
    assert not ImportBatch.objects.exists()


@pytest.mark.django_db
def test_stage_upload_accepts_exact_configured_row_limit(
    finance_user,
    fake_import_file,
    fake_importer,
    fake_registry,
    invoice_party,
    settings,
):
    settings.IMPORT_MAX_ROWS = 2
    fake_importer.rows = (
        make_invoice_row(row_number=2, invoice_number="INV-LIMIT-1"),
        make_invoice_row(row_number=3, invoice_number="INV-LIMIT-2"),
    )

    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    assert batch.total_rows == 2
    assert batch.source_files.count() == 1


@pytest.mark.django_db
def test_stage_upload_rejects_one_row_over_limit_without_leaving_files(
    finance_user,
    fake_import_file,
    fake_importer,
    fake_registry,
    invoice_party,
    settings,
    tmp_path,
):
    settings.IMPORT_MAX_ROWS = 2
    settings.MEDIA_ROOT = tmp_path
    fake_importer.rows = (
        make_invoice_row(row_number=2, invoice_number="INV-LIMIT-1"),
        make_invoice_row(row_number=3, invoice_number="INV-LIMIT-2"),
        make_invoice_row(row_number=4, invoice_number="INV-LIMIT-3"),
    )

    with pytest.raises(RowValidationError, match="文件数据行数超过系统允许的上限"):
        stage_upload(
            fake_import_file,
            source_kind=SourceKind.INPUT_INVOICE,
            actor=finance_user,
            registry=fake_registry,
        )

    assert not ImportBatch.objects.exists()
    assert not SourceFile.objects.exists()
    assert not StagedRow.objects.exists()
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.django_db
def test_stage_upload_rolls_back_file_and_batch_when_parser_raises(
    finance_user, fake_import_file, settings, tmp_path
):
    from apps.imports.registry import ImporterRegistry

    settings.MEDIA_ROOT = tmp_path
    registry = ImporterRegistry([FakeImporter(error=RuntimeError("parser failed"))])

    with pytest.raises(RuntimeError, match="parser failed"):
        stage_upload(
            fake_import_file,
            source_kind=SourceKind.INPUT_INVOICE,
            actor=finance_user,
            registry=registry,
        )

    assert not ImportBatch.objects.exists()
    assert not SourceFile.objects.exists()
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.django_db
def test_stage_upload_compensates_saved_source_file_when_audit_fails(
    finance_user, fake_import_file, fake_registry, monkeypatch, settings, tmp_path
):
    from apps.imports import services

    settings.MEDIA_ROOT = tmp_path

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(services, "record_audit", fail_audit)

    with pytest.raises(RuntimeError, match="audit failed"):
        stage_upload(
            fake_import_file,
            source_kind=SourceKind.INPUT_INVOICE,
            actor=finance_user,
            registry=fake_registry,
        )

    assert not ImportBatch.objects.exists()
    assert not SourceFile.objects.exists()
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.django_db
def test_stage_upload_isolates_bad_rows_without_writing_formal_ledger(
    finance_user, fake_import_file, fake_importer, fake_registry, invoice_party
):
    fake_importer.rows = (
        make_invoice_row(row_number=2),
        make_invoice_row(
            row_number=3,
            normalized=None,
            issues=(("required", "发票号码不能为空", "invoice_number"),),
        ),
        make_invoice_row(row_number=4, direction="output"),
    )

    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    assert (batch.total_rows, batch.valid_rows, batch.duplicate_rows, batch.error_rows) == (
        3,
        1,
        0,
        2,
    )
    assert StagedRow.objects.filter(batch=batch, issues__isnull=False).count() == 3
    assert Invoice.objects.count() == 0
    assert MoneyTransaction.objects.count() == 0


@pytest.mark.django_db
def test_stage_upload_isolates_malformed_normalized_field(
    finance_user, fake_import_file, fake_importer, fake_registry
):
    fake_importer.rows = (make_invoice_row(row_number=2, invoice_number=None),)

    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    assert batch.error_rows == 1
    assert batch.rows.get().issues[0]["code"] == "required"
    assert Invoice.objects.count() == 0


@pytest.mark.django_db
def test_stage_upload_isolates_non_json_raw_data_and_confirms_other_rows(
    finance_user, fake_import_file, fake_importer, fake_registry, invoice_party
):
    good_row = make_invoice_row(row_number=2)
    bad_row = replace(good_row, row_number=3, raw_data={"bad": {"not-json"}})
    fake_importer.rows = (good_row, bad_row)

    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    bad_staged = batch.rows.get(row_number=3)
    assert batch.valid_rows == 1
    assert batch.error_rows == 1
    assert bad_staged.issues[0]["code"] == "raw_data"
    assert bad_staged.raw_data["_unserializable_type"] == "builtins.set"
    assert confirm_batch(batch.id, finance_user).posted_rows == 1
    assert Invoice.objects.count() == 1


@pytest.mark.django_db
def test_stage_upload_marks_same_batch_duplicate_invoice(
    finance_user, fake_import_file, fake_importer, fake_registry, invoice_party
):
    fake_importer.rows = (make_invoice_row(row_number=2), make_invoice_row(row_number=3))

    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    assert batch.valid_rows == 1
    assert batch.duplicate_rows == 1
    assert batch.error_rows == 0
    assert list(batch.rows.order_by("row_number").values_list("is_duplicate", flat=True)) == [
        False,
        True,
    ]


@pytest.mark.django_db
def test_stage_upload_marks_wrong_normalized_type_for_source_kind(
    finance_user, fake_import_file, fake_importer, fake_registry
):
    fake_importer.rows = (make_invoice_row(row_number=2),)

    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.BANK,
        actor=finance_user,
        registry=fake_registry,
    )

    row = batch.rows.get()
    assert batch.error_rows == 1
    assert row.issues[0]["code"] == "source_kind"


@pytest.mark.django_db
def test_stage_upload_rejects_unknown_counterparty(
    finance_user, fake_import_file, fake_importer, fake_registry
):
    fake_importer.rows = (make_invoice_row(row_number=2, seller_tax_id="unknown"),)

    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    assert batch.error_rows == 1
    assert batch.rows.get().issues[0]["code"] == "counterparty"


@pytest.mark.django_db
def test_stage_upload_rejects_ambiguous_invoice_tax_id(
    finance_user, fake_import_file, fake_importer, fake_registry, invoice_party
):
    Counterparty.objects.create(
        name="重复供应商",
        normalized_name="重复供应商",
        tax_id=invoice_party.tax_id,
        is_supplier=True,
    )
    fake_importer.rows = (make_invoice_row(row_number=2),)

    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    assert batch.error_rows == 1
    assert batch.rows.get().issues[0]["code"] == "counterparty_ambiguous"
    assert confirm_batch(batch.id, finance_user).posted_rows == 0
    assert Invoice.objects.count() == 0


@pytest.mark.django_db
def test_stage_upload_rejects_non_finance_actor(
    owner_user, fake_import_file, fake_registry
):
    with pytest.raises(PermissionDenied, match="财务"):
        stage_upload(
            fake_import_file,
            source_kind=SourceKind.INPUT_INVOICE,
            actor=owner_user,
            registry=fake_registry,
        )


@pytest.mark.django_db
def test_import_services_reject_existing_dual_role_actor(
    finance_user,
    owner_user,
    fake_import_file,
    fake_importer,
    fake_registry,
    invoice_party,
):
    owner_user.groups.add(Group.objects.get(name=Role.FINANCE.value))

    with pytest.raises(PermissionDenied, match="财务"):
        stage_upload(
            fake_import_file,
            source_kind=SourceKind.INPUT_INVOICE,
            actor=owner_user,
            registry=fake_registry,
        )

    fake_importer.rows = (make_invoice_row(row_number=2),)
    batch = stage_upload(
        SimpleUploadedFile("finance.csv", b"marker\nfinance\n"),
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )
    with pytest.raises(PermissionDenied, match="财务"):
        confirm_batch(batch.id, owner_user)

    assert Invoice.objects.count() == 0


@pytest.mark.django_db
def test_confirm_batch_posts_valid_rows_sets_partial_status_and_records_audit(
    finance_user, fake_import_file, fake_importer, fake_registry, invoice_party
):
    fake_importer.rows = (
        make_invoice_row(row_number=2),
        make_invoice_row(row_number=3, normalized=None, issues=(("required", "缺少金额", "amount"),)),
    )
    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    result = confirm_batch(batch.id, finance_user)
    batch.refresh_from_db()

    assert result.posted_rows == 1
    assert result.error_rows == 1
    assert batch.status == "partial"
    assert batch.confirmed_at is not None
    assert Invoice.objects.get().counterparty == invoice_party
    assert batch.rows.filter(posted_at__isnull=False).count() == 1
    assert batch.rows.filter(posted_at__isnull=True).count() == 1
    assert batch.rows.filter(issues__isnull=False).count() == 2


@pytest.mark.django_db
def test_confirm_batch_all_errors_is_partial_and_posts_nothing(
    finance_user, fake_import_file, fake_importer, fake_registry
):
    fake_importer.rows = (
        make_invoice_row(row_number=2, normalized=None, issues=(("required", "缺少金额", "amount"),)),
    )
    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    result = confirm_batch(batch.id, finance_user)
    batch.refresh_from_db()

    assert result.posted_rows == 0
    assert batch.status == "partial"
    assert Invoice.objects.count() == 0


@pytest.mark.django_db
def test_confirm_batch_is_idempotent(
    finance_user, fake_import_file, fake_importer, fake_registry, invoice_party
):
    fake_importer.rows = (make_invoice_row(row_number=2),)
    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    first = confirm_batch(batch.id, finance_user)
    second = confirm_batch(batch.id, finance_user)

    assert first == second
    assert Invoice.objects.count() == 1
    assert batch.rows.filter(posted_at__isnull=False).count() == 1


@pytest.mark.django_db
def test_confirm_batch_rolls_back_all_ledger_and_staging_changes_on_failure(
    finance_user, fake_import_file, fake_importer, fake_registry, invoice_party, monkeypatch
):
    from apps.imports import services

    fake_importer.rows = (make_invoice_row(row_number=2), make_invoice_row(row_number=3, invoice_number="INV-002"))
    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )
    original_post = services._post_normalized_row
    calls = 0

    def fail_after_second_post(locked_batch, row):
        nonlocal calls
        calls += 1
        original_post(locked_batch, row)
        if calls == 2:
            raise RuntimeError("posting failed")

    monkeypatch.setattr(services, "_post_normalized_row", fail_after_second_post)

    with pytest.raises(RuntimeError, match="posting failed"):
        confirm_batch(batch.id, finance_user)

    batch.refresh_from_db()
    assert Invoice.objects.count() == 0
    assert batch.confirmed_at is None
    assert batch.status == "previewed"
    assert batch.rows.filter(posted_at__isnull=False).count() == 0


@pytest.mark.django_db
def test_confirm_batch_reraises_non_unique_integrity_error_and_rolls_back(
    finance_user, fake_import_file, fake_importer, fake_registry, invoice_party, monkeypatch
):
    from apps.imports import services

    fake_importer.rows = (
        make_invoice_row(row_number=2),
        make_invoice_row(row_number=3, invoice_number="INV-002"),
    )
    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )
    original_post = services._post_normalized_row
    calls = 0

    def fail_second_post(locked_batch, row):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise IntegrityError("check constraint failed")
        original_post(locked_batch, row)

    monkeypatch.setattr(services, "_post_normalized_row", fail_second_post)

    with pytest.raises(IntegrityError, match="check constraint failed"):
        confirm_batch(batch.id, finance_user)

    batch.refresh_from_db()
    assert batch.status == "previewed"
    assert batch.confirmed_at is None
    assert batch.duplicate_rows == 0
    assert batch.rows.filter(posted_at__isnull=False).count() == 0
    assert Invoice.objects.count() == 0
    assert AuditLog.objects.filter(action="import.confirmed").count() == 0


@pytest.mark.django_db
def test_stage_upload_and_confirm_skip_cross_batch_invoice_duplicate(
    finance_user, fake_importer, fake_registry, invoice_party
):
    first_file = SimpleUploadedFile("first.csv", b"marker\nfirst\n")
    fake_importer.rows = (make_invoice_row(row_number=2),)
    first_batch = stage_upload(
        first_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )
    confirm_batch(first_batch.id, finance_user)

    second_file = SimpleUploadedFile("second.csv", b"marker\nsecond\n")
    fake_importer.rows = (make_invoice_row(row_number=2),)
    second_batch = stage_upload(
        second_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )
    result = confirm_batch(second_batch.id, finance_user)

    second_batch.refresh_from_db()
    assert result.posted_rows == 0
    assert second_batch.duplicate_rows == 1
    assert second_batch.rows.get().is_duplicate
    assert Invoice.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("new_status", [InvoiceStatus.RED, InvoiceStatus.VOID])
def test_invoice_status_change_updates_existing_invoice_with_audit_trace(
    finance_user, fake_importer, fake_registry, invoice_party, new_status
):
    fake_importer.rows = (make_invoice_row(row_number=2),)
    original_batch = stage_upload(
        SimpleUploadedFile("normal.csv", b"marker\nnormal\n"),
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )
    confirm_batch(original_batch.id, finance_user)
    invoice = Invoice.objects.get()

    changed = replace(make_invoice_row(row_number=2).normalized, status=new_status)
    fake_importer.rows = (make_invoice_row(row_number=2, normalized=changed),)
    status_batch = stage_upload(
        SimpleUploadedFile(f"{new_status}.csv", f"marker\n{new_status}\n".encode()),
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    assert status_batch.valid_rows == 1
    assert status_batch.duplicate_rows == 0
    assert status_batch.error_rows == 0

    result = confirm_batch(status_batch.id, finance_user)
    invoice.refresh_from_db()
    status_batch.rows.get().refresh_from_db()
    audit = AuditLog.objects.get(action="invoice.status_changed")

    assert result.posted_rows == 1
    assert Invoice.objects.count() == 1
    assert invoice.status == new_status
    assert invoice.import_batch_id == original_batch.id
    assert audit.target_id == str(invoice.id)
    assert audit.changes == {
        "from_status": InvoiceStatus.NORMAL,
        "to_status": new_status,
        "source_batch_id": str(status_batch.id),
        "source_row": 2,
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    "changes",
    [
        {"total_amount": Decimal("101.00")},
        {"buyer_tax_id": "913201-CONFLICT"},
        {"seller_name": "冲突的销售方名称"},
    ],
)
def test_same_invoice_identity_with_incompatible_fields_is_staged_as_conflict(
    finance_user, fake_importer, fake_registry, invoice_party, changes
):
    fake_importer.rows = (make_invoice_row(row_number=2),)
    first_batch = stage_upload(
        SimpleUploadedFile("original.csv", b"marker\noriginal\n"),
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )
    confirm_batch(first_batch.id, finance_user)
    original = Invoice.objects.get()

    conflicting = replace(make_invoice_row(row_number=2).normalized, **changes)
    fake_importer.rows = (make_invoice_row(row_number=2, normalized=conflicting),)
    conflict_batch = stage_upload(
        SimpleUploadedFile("conflict.csv", b"marker\nconflict\n"),
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )
    row = conflict_batch.rows.get()

    assert conflict_batch.error_rows == 1
    assert conflict_batch.duplicate_rows == 0
    assert row.issues == [
        {
            "code": "invoice_conflict",
            "message": "相同发票号码和销售方税号的业务字段冲突",
            "field": "",
        }
    ]

    result = confirm_batch(conflict_batch.id, finance_user)
    original.refresh_from_db()
    assert result.posted_rows == 0
    assert Invoice.objects.count() == 1
    assert original.status == InvoiceStatus.NORMAL
    assert original.total_amount == Decimal("100.00")


@pytest.mark.django_db
def test_void_or_red_invoice_cannot_be_silently_restored_to_normal(
    finance_user, fake_importer, fake_registry, invoice_party
):
    fake_importer.rows = (make_invoice_row(row_number=2),)
    normal_batch = stage_upload(
        SimpleUploadedFile("normal.csv", b"marker\nnormal\n"),
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )
    confirm_batch(normal_batch.id, finance_user)
    invoice = Invoice.objects.get()
    invoice.status = InvoiceStatus.RED
    invoice.save(update_fields=["status"])

    fake_importer.rows = (make_invoice_row(row_number=2),)
    restore_batch = stage_upload(
        SimpleUploadedFile("restore.csv", b"marker\nrestore\n"),
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    row = restore_batch.rows.get()
    assert restore_batch.error_rows == 1
    assert row.issues[0]["code"] == "invoice_conflict"
    assert "恢复" in row.issues[0]["message"]
    assert confirm_batch(restore_batch.id, finance_user).posted_rows == 0
    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.RED


@pytest.mark.django_db
@pytest.mark.parametrize("new_status", [InvoiceStatus.RED, InvoiceStatus.VOID])
def test_invoice_status_change_preserves_allocations_and_enters_exception_report(
    finance_user, fake_importer, fake_registry, invoice_party, new_status
):
    fake_importer.rows = (make_invoice_row(row_number=2),)
    first_batch = stage_upload(
        SimpleUploadedFile("normal.csv", b"marker\nnormal\n"),
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )
    confirm_batch(first_batch.id, finance_user)
    invoice = Invoice.objects.get()
    payment = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=invoice.total_amount,
        counterparty=invoice.counterparty,
    )
    reconciliation = create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(invoice.id, payment.id, invoice.total_amount)],
    )
    type(reconciliation).objects.filter(pk=reconciliation.pk).update(
        created_at=datetime(2026, 7, 31, 12, tzinfo=UTC)
    )

    changed = replace(make_invoice_row(row_number=2).normalized, status=new_status)
    fake_importer.rows = (make_invoice_row(row_number=2, normalized=changed),)
    status_batch = stage_upload(
        SimpleUploadedFile(f"allocated-{new_status}.csv", f"marker\n{new_status}\n".encode()),
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )
    confirm_batch(status_batch.id, finance_user)

    risks = [
        item
        for item in exception_items(date(2026, 7, 31))
        if item.type == ExceptionType.RED_WITH_ACTIVE_ALLOCATION
    ]
    assert ReconciliationAllocation.objects.count() == 1
    assert len(risks) == 1
    assert risks[0].reference_id == invoice.id
    assert risks[0].detail == {
        InvoiceStatus.RED: "红冲发票仍存在有效核销",
        InvoiceStatus.VOID: "作废发票仍存在有效核销",
    }[new_status]


@pytest.mark.django_db
def test_stage_upload_and_confirm_skip_cross_batch_transaction_duplicate(
    finance_user, fake_importer, fake_registry, funding_account, transaction_party
):
    first_file = SimpleUploadedFile("first.csv", b"marker\nfirst\n")
    fake_importer.rows = (make_transaction_row(row_number=2),)
    first_batch = stage_upload(
        first_file,
        source_kind=SourceKind.BANK,
        actor=finance_user,
        registry=fake_registry,
    )
    confirm_batch(first_batch.id, finance_user)

    second_file = SimpleUploadedFile("second.csv", b"marker\nsecond\n")
    fake_importer.rows = (make_transaction_row(row_number=2),)
    second_batch = stage_upload(
        second_file,
        source_kind=SourceKind.BANK,
        actor=finance_user,
        registry=fake_registry,
    )
    confirm_batch(second_batch.id, finance_user)

    second_batch.refresh_from_db()
    assert second_batch.duplicate_rows == 1
    assert MoneyTransaction.objects.count() == 1


@pytest.mark.django_db
def test_overlapping_transaction_id_ignores_changed_display_fields(
    finance_user, fake_importer, fake_registry, funding_account, transaction_party
):
    CounterpartyAlias.objects.create(
        counterparty=transaction_party,
        kind=AliasKind.BANK_ACCOUNT,
        value="62220000",
        normalized_value="62220000",
        confirmed_by=finance_user,
    )
    first = make_transaction_row(row_number=2).normalized
    changed_display = replace(
        first,
        occurred_at=first.occurred_at.replace(hour=11),
        amount=first.amount + 1,
        counterparty_name="银行更新后的展示名称",
        summary="银行更新后的摘要",
    )

    for filename, content, normalized in (
        ("first.csv", b"marker\nfirst\n", first),
        ("overlap.csv", b"marker\noverlap\n", changed_display),
    ):
        fake_importer.rows = (
            make_transaction_row(row_number=2, normalized=normalized),
        )
        batch = stage_upload(
            SimpleUploadedFile(filename, content),
            source_kind=SourceKind.BANK,
            actor=finance_user,
            registry=fake_registry,
        )
        confirm_batch(batch.id, finance_user)

    batch.refresh_from_db()
    assert MoneyTransaction.objects.count() == 1
    assert batch.duplicate_rows == 1
    assert batch.rows.get().is_duplicate


@pytest.mark.django_db
def test_duplicate_transaction_does_not_create_balance_snapshot_from_duplicate_row(
    finance_user, fake_importer, fake_registry, funding_account, transaction_party
):
    original = make_transaction_row(row_number=2).normalized
    duplicate = replace(
        original,
        occurred_at=original.occurred_at.replace(hour=11),
        amount=Decimal("101.00"),
        balance_after=Decimal("999.00"),
        summary="银行更新后的重复记录",
    )

    for filename, content, normalized in (
        ("first.csv", b"marker\nfirst\n", original),
        ("duplicate.csv", b"marker\nduplicate\n", duplicate),
    ):
        fake_importer.rows = (
            make_transaction_row(row_number=2, normalized=normalized),
        )
        batch = stage_upload(
            SimpleUploadedFile(filename, content),
            source_kind=SourceKind.BANK,
            actor=finance_user,
            registry=fake_registry,
        )
        confirm_batch(batch.id, finance_user)

    snapshots = list(AccountBalanceSnapshot.objects.all())
    assert MoneyTransaction.objects.count() == 1
    assert len(snapshots) == 1
    assert snapshots[0].as_of == original.occurred_at
    assert snapshots[0].balance == original.balance_after


@pytest.mark.django_db
def test_same_transaction_id_across_channel_or_funding_account_is_not_merged(
    finance_user, fake_importer, fake_registry, funding_account, transaction_party
):
    FundingAccount.objects.create(
        channel=MoneyChannel.BANK,
        name="另一银行账户",
        identifier="other-bank-account",
        masked_identifier="********ount",
    )
    FundingAccount.objects.create(
        channel=MoneyChannel.WECHAT,
        name="微信资金账户",
        identifier="bank-account",
        masked_identifier="********ount",
    )
    original = make_transaction_row(row_number=2).normalized
    variants = (
        (SourceKind.BANK, original),
        (SourceKind.BANK, replace(original, account_identifier="other-bank-account")),
        (SourceKind.WECHAT, replace(original, channel=MoneyChannel.WECHAT)),
    )

    for index, (source_kind, normalized) in enumerate(variants):
        fake_importer.rows = (
            make_transaction_row(row_number=2, normalized=normalized),
        )
        batch = stage_upload(
            SimpleUploadedFile(f"overlap-{index}.csv", f"marker\n{index}\n".encode()),
            source_kind=source_kind,
            actor=finance_user,
            registry=fake_registry,
        )
        confirm_batch(batch.id, finance_user)

    assert MoneyTransaction.objects.count() == 3


@pytest.mark.django_db
def test_overlapping_transaction_without_id_uses_stable_fallback_fields(
    finance_user, fake_importer, fake_registry, funding_account, transaction_party
):
    original = replace(
        make_transaction_row(row_number=2).normalized,
        transaction_id="",
    )
    variants = (
        original,
        replace(original, summary="更新后的摘要"),
    )

    for index, normalized in enumerate(variants):
        fake_importer.rows = (
            make_transaction_row(row_number=2, normalized=normalized),
        )
        batch = stage_upload(
            SimpleUploadedFile(f"fallback-{index}.csv", f"marker\n{index}\n".encode()),
            source_kind=SourceKind.BANK,
            actor=finance_user,
            registry=fake_registry,
        )
        confirm_batch(batch.id, finance_user)

    batch.refresh_from_db()
    assert MoneyTransaction.objects.count() == 1
    assert batch.duplicate_rows == 1


@pytest.mark.django_db
def test_stage_upload_rejects_unknown_transaction_counterparty(
    finance_user, fake_import_file, fake_importer, fake_registry, funding_account
):
    normalized = replace(
        make_transaction_row(row_number=2).normalized,
        counterparty_name="未知单位",
    )
    fake_importer.rows = (make_transaction_row(row_number=2, normalized=normalized),)

    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.BANK,
        actor=finance_user,
        registry=fake_registry,
    )

    assert batch.error_rows == 1
    assert batch.rows.get().issues[0]["code"] == "counterparty"


@pytest.mark.django_db
def test_stage_upload_rejects_ambiguous_transaction_counterparty_name(
    finance_user,
    fake_import_file,
    fake_importer,
    fake_registry,
    funding_account,
    transaction_party,
):
    Counterparty.objects.create(
        name="重复收款方",
        normalized_name=transaction_party.normalized_name,
        is_supplier=True,
    )
    fake_importer.rows = (make_transaction_row(row_number=2),)

    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.BANK,
        actor=finance_user,
        registry=fake_registry,
    )

    assert batch.error_rows == 1
    assert batch.rows.get().issues[0]["code"] == "counterparty_ambiguous"
    assert confirm_batch(batch.id, finance_user).posted_rows == 0
    assert MoneyTransaction.objects.count() == 0


@pytest.mark.django_db
def test_stage_upload_rejects_unknown_funding_account(
    finance_user, fake_import_file, fake_importer, fake_registry, transaction_party
):
    normalized = replace(
        make_transaction_row(row_number=2).normalized,
        account_identifier="unknown-account",
    )
    fake_importer.rows = (make_transaction_row(row_number=2, normalized=normalized),)

    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.BANK,
        actor=finance_user,
        registry=fake_registry,
    )

    assert batch.error_rows == 1
    assert batch.rows.get().issues[0]["code"] == "account"


@pytest.mark.django_db
def test_stage_upload_rejects_ambiguous_funding_account(
    finance_user,
    fake_import_file,
    fake_importer,
    fake_registry,
    funding_account,
    transaction_party,
):
    FundingAccount.objects.create(
        channel=funding_account.channel,
        name="重复账户",
        identifier=funding_account.identifier,
        masked_identifier="********ount",
    )
    fake_importer.rows = (make_transaction_row(row_number=2),)

    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.BANK,
        actor=finance_user,
        registry=fake_registry,
    )

    assert batch.error_rows == 1
    assert batch.rows.get().issues[0]["code"] == "account_ambiguous"
    assert confirm_batch(batch.id, finance_user).posted_rows == 0
    assert MoneyTransaction.objects.count() == 0


@pytest.mark.django_db
def test_confirm_batch_rejects_non_finance_actor(
    finance_user, owner_user, fake_import_file, fake_importer, fake_registry, invoice_party
):
    fake_importer.rows = (make_invoice_row(row_number=2),)
    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    with pytest.raises(PermissionDenied, match="财务"):
        confirm_batch(batch.id, owner_user)

    assert Invoice.objects.count() == 0


@pytest.mark.django_db
def test_confirm_batch_records_finance_actor_and_result_in_audit_log(
    finance_user, fake_import_file, fake_importer, fake_registry, invoice_party
):
    fake_importer.rows = (make_invoice_row(row_number=2),)
    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )

    result = confirm_batch(batch.id, finance_user)

    audit = AuditLog.objects.get(action="import.confirmed")
    assert audit.actor == finance_user
    assert audit.target_id == str(batch.id)
    assert audit.changes == result.as_dict()


@pytest.mark.django_db
def test_confirm_batch_locks_batch_before_posting_rows(
    finance_user, fake_import_file, fake_importer, fake_registry, invoice_party, monkeypatch
):
    from apps.imports import services

    fake_importer.rows = (make_invoice_row(row_number=2),)
    batch = stage_upload(
        fake_import_file,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
        registry=fake_registry,
    )
    lock_calls = []
    original_select_for_update = ImportBatch.objects.select_for_update

    def select_for_update(*args, **kwargs):
        lock_calls.append((args, kwargs))
        return original_select_for_update(*args, **kwargs)

    monkeypatch.setattr(ImportBatch.objects, "select_for_update", select_for_update)

    services.confirm_batch(batch.id, finance_user)

    assert lock_calls
