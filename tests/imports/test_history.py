from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.imports.choices import SourceKind
from apps.imports.history import coverage_matrix
from apps.imports.models import CoverageStatus, DataCoveragePeriod


@pytest.mark.django_db
def test_coverage_matrix_is_empty_without_registered_years():
    assert coverage_matrix() == {}


@pytest.mark.django_db
def test_coverage_matrix_contains_every_source_for_continuous_registered_years():
    DataCoveragePeriod.objects.create(
        year=2024,
        source_kind=SourceKind.INPUT_INVOICE,
        status=CoverageStatus.FULL,
        expected_start=date(2024, 1, 1),
        expected_end=date(2024, 12, 31),
        actual_start=date(2024, 1, 1),
        actual_end=date(2024, 12, 31),
    )
    partial = DataCoveragePeriod.objects.create(
        year=2025,
        source_kind=SourceKind.BANK,
        status=CoverageStatus.PARTIAL,
        expected_start=date(2025, 1, 1),
        expected_end=date(2025, 12, 31),
        actual_start=date(2025, 2, 1),
        actual_end=date(2025, 11, 30),
        missing_notes="缺少一月和十二月银行流水",
    )

    matrix = coverage_matrix()

    assert set(matrix) == {
        (year, source_kind)
        for year in (2024, 2025)
        for source_kind in SourceKind.values
    }
    assert matrix[(2024, SourceKind.INPUT_INVOICE)].status == CoverageStatus.FULL
    assert matrix[(2024, SourceKind.BANK)].status == CoverageStatus.MISSING
    assert matrix[(2024, SourceKind.BANK)].missing_notes
    assert matrix[(2025, SourceKind.BANK)] == partial
    assert matrix[(2025, SourceKind.BANK)].missing_notes == "缺少一月和十二月银行流水"


@pytest.mark.django_db
def test_coverage_period_requires_one_record_per_year_and_source_kind():
    values = {
        "year": 2024,
        "source_kind": SourceKind.WECHAT,
        "status": CoverageStatus.FULL,
        "expected_start": date(2024, 1, 1),
        "expected_end": date(2024, 12, 31),
        "actual_start": date(2024, 1, 1),
        "actual_end": date(2024, 12, 31),
    }
    DataCoveragePeriod.objects.create(**values)

    with pytest.raises(IntegrityError), transaction.atomic():
        DataCoveragePeriod.objects.create(**values)


@pytest.mark.django_db
def test_coverage_period_rejects_reverse_expected_date_range():
    with pytest.raises(IntegrityError), transaction.atomic():
        DataCoveragePeriod.objects.create(
            year=2024,
            source_kind=SourceKind.WECHAT,
            status=CoverageStatus.PARTIAL,
            expected_start=date(2024, 12, 31),
            expected_end=date(2024, 1, 1),
            missing_notes="日期范围异常",
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "values",
    [
        {
            "status": "unexpected",
            "actual_start": date(2024, 1, 1),
            "actual_end": date(2024, 12, 31),
        },
        {
            "status": CoverageStatus.MISSING,
            "actual_start": date(2024, 1, 1),
            "actual_end": date(2024, 12, 31),
        },
        {
            "status": CoverageStatus.FULL,
        },
        {
            "status": CoverageStatus.PARTIAL,
            "actual_start": date(2023, 12, 31),
            "actual_end": date(2024, 12, 31),
        },
    ],
)
def test_coverage_period_constraints_reject_invalid_status_and_actual_ranges(values):
    with pytest.raises(IntegrityError), transaction.atomic():
        DataCoveragePeriod.objects.create(
            year=2024,
            source_kind=SourceKind.BANK,
            expected_start=date(2024, 1, 1),
            expected_end=date(2024, 12, 31),
            **values,
        )


@pytest.mark.django_db
def test_coverage_period_validation_requires_status_appropriate_actual_range():
    period = DataCoveragePeriod(
        year=2024,
        source_kind=SourceKind.BANK,
        status=CoverageStatus.FULL,
        expected_start=date(2024, 1, 1),
        expected_end=date(2024, 12, 31),
    )

    with pytest.raises(ValidationError, match="实际资料区间"):
        period.full_clean()
