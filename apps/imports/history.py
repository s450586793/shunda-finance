from datetime import date

from .choices import SourceKind
from .models import CoverageStatus, DataCoveragePeriod


def coverage_matrix() -> dict[tuple[int, str], DataCoveragePeriod]:
    registered_years = DataCoveragePeriod.objects.values_list("year", flat=True)
    first_year = registered_years.order_by("year").first()
    if first_year is None:
        return {}
    last_year = registered_years.order_by("-year").first()
    periods = {
        (period.year, period.source_kind): period
        for period in DataCoveragePeriod.objects.filter(
            year__gte=first_year,
            year__lte=last_year,
        )
    }
    matrix = {}
    for year in range(first_year, last_year + 1):
        for source_kind in SourceKind.values:
            key = (year, source_kind)
            matrix[key] = periods.get(key) or _missing_period(year, source_kind)
    return matrix


def _missing_period(year: int, source_kind: str) -> DataCoveragePeriod:
    return DataCoveragePeriod(
        year=year,
        source_kind=source_kind,
        status=CoverageStatus.MISSING,
        expected_start=date(year, 1, 1),
        expected_end=date(year, 12, 31),
        missing_notes="未登记该年度来源资料完整性记录",
    )
