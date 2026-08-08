# ruff: noqa: RUF012

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models.fields.files import FieldFile, FileField

from apps.core.models import ImmutableLedgerModel, UUIDModel

from .choices import FINAL_BATCH_STATUSES, BatchStatus, SourceKind
from .storage import source_upload_path


class ProtectedSourceFieldFile(FieldFile):
    def delete(self, save=True):
        raise RuntimeError("原始上传文件不允许物理删除")


class ProtectedSourceFileField(FileField):
    attr_class = ProtectedSourceFieldFile

    def deconstruct(self):
        name, _path, args, kwargs = super().deconstruct()
        return name, "django.db.models.FileField", args, kwargs


class ImportBatch(ImmutableLedgerModel):
    source_kind = models.CharField(max_length=20, choices=SourceKind.choices)
    status = models.CharField(
        max_length=20, choices=BatchStatus.choices, default=BatchStatus.UPLOADED
    )
    total_rows = models.PositiveIntegerField(default=0)
    valid_rows = models.PositiveIntegerField(default=0)
    duplicate_rows = models.PositiveIntegerField(default=0)
    error_rows = models.PositiveIntegerField(default=0)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta(ImmutableLedgerModel.Meta):
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["source_kind"]),
            models.Index(fields=["period_start"]),
            models.Index(fields=["period_end"]),
        ]


class SourceFile(ImmutableLedgerModel):
    batch = models.ForeignKey(
        ImportBatch, on_delete=models.PROTECT, related_name="source_files"
    )
    file = ProtectedSourceFileField(upload_to=source_upload_path)
    original_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64, unique=True)
    size = models.PositiveBigIntegerField()


class CoverageStatus(models.TextChoices):
    FULL = "full", "完整"
    PARTIAL = "partial", "部分缺失"
    MISSING = "missing", "缺失"


class DataCoveragePeriod(UUIDModel):
    year = models.PositiveSmallIntegerField()
    source_kind = models.CharField(max_length=20, choices=SourceKind.choices)
    status = models.CharField(max_length=12, choices=CoverageStatus.choices)
    expected_start = models.DateField()
    expected_end = models.DateField()
    actual_start = models.DateField(null=True, blank=True)
    actual_end = models.DateField(null=True, blank=True)
    missing_notes = models.TextField(blank=True)

    def clean(self):
        super().clean()
        errors = {}
        if self.expected_start and self.expected_end and self.expected_start > self.expected_end:
            errors["expected_end"] = "预期资料区间结束日期不能早于开始日期"
        if self.status == CoverageStatus.MISSING:
            if self.actual_start is not None or self.actual_end is not None:
                errors["actual_start"] = "缺失资料不能记录实际资料区间"
        elif self.status in {CoverageStatus.FULL, CoverageStatus.PARTIAL}:
            if self.actual_start is None or self.actual_end is None:
                errors["actual_start"] = "实际资料区间必须同时提供开始和结束日期"
            elif self.actual_start > self.actual_end:
                errors["actual_end"] = "实际资料区间结束日期不能早于开始日期"
            elif (
                self.expected_start
                and self.expected_end
                and (
                    self.actual_start < self.expected_start
                    or self.actual_end > self.expected_end
                )
            ):
                errors["actual_start"] = "实际资料区间必须位于预期资料区间内"
        if errors:
            raise ValidationError(errors)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["year", "source_kind"],
                name="uniq_data_coverage_period",
            ),
            models.CheckConstraint(
                condition=models.Q(expected_start__lte=models.F("expected_end")),
                name="data_coverage_expected_dates_ordered",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status__in=CoverageStatus.values)
                ),
                name="data_coverage_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status=CoverageStatus.MISSING,
                        actual_start__isnull=True,
                        actual_end__isnull=True,
                    )
                    | models.Q(
                        status__in=[CoverageStatus.FULL, CoverageStatus.PARTIAL],
                        actual_start__isnull=False,
                        actual_end__isnull=False,
                        actual_start__lte=models.F("actual_end"),
                        actual_start__gte=models.F("expected_start"),
                        actual_end__lte=models.F("expected_end"),
                    )
                ),
                name="data_coverage_actual_range_valid",
            ),
        ]


class StagedRowQuerySet(models.QuerySet):
    def delete(self):
        with transaction.atomic():
            locked_statuses = tuple(
                ImportBatch.objects.select_for_update()
                .filter(pk__in=self.values("batch_id"))
                .values_list("status", flat=True)
            )
            if FINAL_BATCH_STATUSES.intersection(locked_statuses):
                raise RuntimeError("最终状态批次的暂存记录不允许删除")
            return super().delete()


class StagedRow(UUIDModel):
    objects = StagedRowQuerySet.as_manager()

    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="rows")
    row_number = models.PositiveIntegerField()
    raw_data = models.JSONField()
    normalized_data = models.JSONField(default=dict)
    issues = models.JSONField(default=list)
    is_duplicate = models.BooleanField(default=False)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        base_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "row_number"], name="uniq_staged_row_number"
            ),
        ]
        indexes = [
            models.Index(fields=["batch", "posted_at"]),
            models.Index(fields=["is_duplicate"]),
        ]

    def delete(self, *args, **kwargs):
        if self._state.adding:
            return super().delete(*args, **kwargs)
        return type(self).objects.filter(pk=self.pk).delete()
