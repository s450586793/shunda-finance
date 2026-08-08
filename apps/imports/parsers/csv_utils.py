import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from apps.imports.types import RowValidationError

MONEY_DECIMAL_PLACES = 2
MAX_MONEY_AMOUNT = Decimal("9999999999999999.99")


def parse_decimal(value, *, blank_as_zero=False) -> Decimal:
    text = "" if value is None else str(value).replace(",", "").strip()
    if not text and blank_as_zero:
        return Decimal(0)
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise RowValidationError("金额不合法") from exc
    if (
        not amount.is_finite()
        or amount.as_tuple().exponent < -MONEY_DECIMAL_PLACES
        or abs(amount) > MAX_MONEY_AMOUNT
    ):
        raise RowValidationError("金额不合法")
    return amount


def parse_datetime(value) -> datetime:
    if isinstance(value, datetime):
        occurred_at = value
    else:
        try:
            occurred_at = datetime.fromisoformat(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise RowValidationError("交易时间不合法") from exc
    if timezone.is_naive(occurred_at):
        return timezone.make_aware(occurred_at)
    return occurred_at


def read_csv_rows(file_obj) -> list[list[str]]:
    file_obj.seek(0)
    content = file_obj.read()
    if isinstance(content, str):
        text = content
    else:
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise RowValidationError("CSV 文件编码不受支持")
    file_obj.seek(0)
    return list(csv.reader(io.StringIO(text)))


def text(value) -> str:
    return str(value).strip() if value is not None else ""
