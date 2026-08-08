from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.db.models import QuerySet

from apps.imports.choices import BatchStatus, SourceKind
from apps.imports.models import ImportBatch, SourceFile, StagedRow


def test_batch_status_distinguishes_partial_and_completed_results():
    assert BatchStatus.PARTIAL.value == "partial"
    assert BatchStatus.COMPLETED.value == "completed"
    assert "confirmed" not in BatchStatus.values


def test_source_kind_distinguishes_input_and_output_invoices():
    assert SourceKind.INPUT_INVOICE.value == "input_invoice"
    assert SourceKind.OUTPUT_INVOICE.value == "output_invoice"
    assert "invoice" not in SourceKind.values


@pytest.mark.django_db
def test_invoice_source_kinds_are_persisted_separately(finance_user):
    input_batch = ImportBatch.objects.create(
        source_kind=SourceKind.INPUT_INVOICE, created_by=finance_user
    )
    output_batch = ImportBatch.objects.create(
        source_kind=SourceKind.OUTPUT_INVOICE, created_by=finance_user
    )

    assert list(
        ImportBatch.objects.filter(pk__in=[input_batch.pk, output_batch.pk])
        .order_by("source_kind")
        .values_list("source_kind", flat=True)
    ) == ["input_invoice", "output_invoice"]


@pytest.mark.django_db
def test_new_batch_starts_in_uploaded_state(finance_user):
    batch = ImportBatch.objects.create(source_kind=SourceKind.BANK, created_by=finance_user)

    assert batch.status == BatchStatus.UPLOADED
    assert batch.total_rows == 0
    assert batch.valid_rows == 0
    assert batch.duplicate_rows == 0
    assert batch.error_rows == 0


@pytest.mark.django_db
def test_source_file_records_immutable_original_upload(finance_user):
    batch = ImportBatch.objects.create(source_kind=SourceKind.BANK, created_by=finance_user)
    source_file = SourceFile.objects.create(
        batch=batch,
        file=SimpleUploadedFile("statement.csv", b"date,amount\n"),
        original_name="银行流水.csv",
        sha256="a" * 64,
        size=12,
    )

    assert source_file.file.name == f"imports/{batch.pk}/statement.csv"
    with pytest.raises(RuntimeError, match="正式财务记录不允许物理删除"):
        source_file.delete()
    with pytest.raises(RuntimeError, match="正式财务记录不允许物理删除"):
        SourceFile._base_manager.filter(pk=source_file.pk).delete()
    assert SourceFile.objects.filter(pk=source_file.pk).exists()


@pytest.mark.django_db
def test_source_file_field_cannot_physically_delete_uploaded_content(
    finance_user, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    batch = ImportBatch.objects.create(source_kind=SourceKind.BANK, created_by=finance_user)
    source_file = SourceFile.objects.create(
        batch=batch,
        file=SimpleUploadedFile("statement.csv", b"date,amount\n"),
        original_name="银行流水.csv",
        sha256="c" * 64,
        size=12,
    )

    assert source_file.file.storage.exists(source_file.file.name)
    with source_file.file.open("rb") as uploaded_file:
        assert uploaded_file.read() == b"date,amount\n"
    with pytest.raises(RuntimeError, match="原始上传文件不允许物理删除"):
        source_file.file.delete()
    assert source_file.file.storage.exists(source_file.file.name)


@pytest.mark.django_db
def test_source_file_sha256_must_be_unique(finance_user):
    batch = ImportBatch.objects.create(source_kind=SourceKind.BANK, created_by=finance_user)
    SourceFile.objects.create(
        batch=batch,
        file=SimpleUploadedFile("first.csv", b"first"),
        original_name="first.csv",
        sha256="b" * 64,
        size=5,
    )

    with pytest.raises(IntegrityError):
        SourceFile.objects.create(
            batch=batch,
            file=SimpleUploadedFile("second.csv", b"second"),
            original_name="second.csv",
            sha256="b" * 64,
            size=6,
        )


@pytest.mark.django_db
def test_staged_row_number_is_unique_within_batch(finance_user):
    batch = ImportBatch.objects.create(source_kind=SourceKind.WECHAT, created_by=finance_user)
    StagedRow.objects.create(batch=batch, row_number=1, raw_data={"amount": "12.34"})

    with pytest.raises(IntegrityError):
        StagedRow.objects.create(batch=batch, row_number=1, raw_data={"amount": "56.78"})


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["uploaded", "previewed"])
def test_staged_rows_can_be_deleted_from_non_final_batches(finance_user, status):
    batch = ImportBatch.objects.create(
        source_kind=SourceKind.INPUT_INVOICE,
        status=status,
        created_by=finance_user,
    )
    instance_row = StagedRow.objects.create(batch=batch, row_number=1, raw_data={})
    default_manager_row = StagedRow.objects.create(batch=batch, row_number=2, raw_data={})
    base_manager_row = StagedRow.objects.create(batch=batch, row_number=3, raw_data={})

    instance_result = instance_row.delete()
    default_manager_result = StagedRow.objects.filter(pk=default_manager_row.pk).delete()
    base_manager_result = StagedRow._base_manager.filter(pk=base_manager_row.pk).delete()

    assert [result[0] for result in (instance_result, default_manager_result, base_manager_result)] == [
        1,
        1,
        1,
    ]
    assert not StagedRow.objects.filter(batch=batch).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["partial", "completed"])
def test_staged_rows_cannot_be_deleted_from_final_batches(finance_user, status):
    batch = ImportBatch.objects.create(
        source_kind=SourceKind.OUTPUT_INVOICE,
        status=status,
        created_by=finance_user,
    )
    instance_row = StagedRow.objects.create(batch=batch, row_number=1, raw_data={})
    default_manager_row = StagedRow.objects.create(batch=batch, row_number=2, raw_data={})
    base_manager_row = StagedRow.objects.create(batch=batch, row_number=3, raw_data={})

    with pytest.raises(RuntimeError, match="最终状态批次的暂存记录不允许删除"):
        instance_row.delete()
    with pytest.raises(RuntimeError, match="最终状态批次的暂存记录不允许删除"):
        StagedRow.objects.filter(pk=default_manager_row.pk).delete()
    with pytest.raises(RuntimeError, match="最终状态批次的暂存记录不允许删除"):
        StagedRow._base_manager.filter(pk=base_manager_row.pk).delete()
    assert StagedRow.objects.filter(batch=batch).count() == 3


@pytest.mark.django_db
def test_staged_row_delete_evaluates_locked_batch_status_before_removing_rows(finance_user):
    class StatusValues:
        def __init__(self, queryset, events):
            self.queryset = queryset
            self.events = events

        def __iter__(self):
            self.events.append("locked_statuses_evaluated")
            return iter(self.queryset)

    class LockedBatchQuerySet:
        def __init__(self, queryset, events):
            self.queryset = queryset
            self.events = events

        def filter(self, *args, **kwargs):
            self.events.append("candidate_batches_filtered")
            return type(self)(self.queryset.filter(*args, **kwargs), self.events)

        def values_list(self, *args, **kwargs):
            self.events.append("locked_statuses_requested")
            return StatusValues(self.queryset.values_list(*args, **kwargs), self.events)

        def __getattr__(self, name):
            return getattr(self.queryset, name)

    batch = ImportBatch.objects.create(
        source_kind=SourceKind.OUTPUT_INVOICE, created_by=finance_user
    )
    row = StagedRow.objects.create(batch=batch, row_number=1, raw_data={})
    events = []
    select_for_update = ImportBatch.objects.select_for_update
    queryset_delete = QuerySet.delete

    def locked_batches():
        events.append("lock_requested")
        return LockedBatchQuerySet(select_for_update(), events)

    def delete_rows(queryset):
        events.append("rows_deleted")
        return queryset_delete(queryset)

    with (
        patch.object(ImportBatch.objects, "select_for_update", side_effect=locked_batches),
        patch.object(QuerySet, "delete", new=delete_rows),
    ):
        deleted_count, deleted_by_model = StagedRow.objects.filter(pk=row.pk).delete()

    assert events == [
        "lock_requested",
        "candidate_batches_filtered",
        "locked_statuses_requested",
        "locked_statuses_evaluated",
        "rows_deleted",
    ]
    assert deleted_count == 1
    assert deleted_by_model == {StagedRow._meta.label: 1}
    assert not StagedRow.objects.filter(pk=row.pk).exists()


@pytest.mark.django_db
def test_import_models_expose_query_indexes(finance_user):
    batch = ImportBatch.objects.create(source_kind=SourceKind.BANK, created_by=finance_user)
    StagedRow.objects.create(batch=batch, row_number=1, raw_data={})

    batch_indexes = {tuple(index.fields) for index in ImportBatch._meta.indexes}
    row_indexes = {tuple(index.fields) for index in StagedRow._meta.indexes}

    assert {("status",), ("source_kind",), ("period_start",), ("period_end",)} <= batch_indexes
    assert {("batch", "posted_at"), ("is_duplicate",)} <= row_indexes
