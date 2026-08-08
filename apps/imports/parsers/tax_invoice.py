from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings

from apps.imports.choices import SourceKind
from apps.imports.types import (
    NormalizedInvoiceRow,
    ParsedRow,
    RowIssue,
    RowValidationError,
)
from apps.ledger.choices import InvoiceDirection, InvoiceStatus

from .excel import iter_sheet_rows

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
SHEET_NAME = "发票基础信息"
MONEY_DECIMAL_PLACES = 2
MAX_TOTAL_AMOUNT = Decimal("9999999999999999.99")


class TaxInvoiceImporter:
    source_kinds = frozenset({SourceKind.INPUT_INVOICE, SourceKind.OUTPUT_INVOICE})

    def supports(self, filename: str, headers: list[str]) -> bool:
        return filename.lower().endswith(".xlsx") and set(COLUMNS.values()).issubset(
            headers
        )

    def parse(self, file_obj) -> Iterable[ParsedRow]:
        company_tax_id = _text(getattr(settings, "COMPANY_TAX_ID", ""))
        for row_number, raw_data in iter_sheet_rows(file_obj, SHEET_NAME):
            yield _parse_row(row_number, raw_data, company_tax_id)

    def infer_source_kind(self, file_obj) -> str:
        company_tax_id = _text(getattr(settings, "COMPANY_TAX_ID", ""))
        directions = set()
        for _row_number, raw_data in iter_sheet_rows(file_obj, SHEET_NAME):
            try:
                directions.add(
                    direction_for(
                        _text(raw_data.get(COLUMNS["seller_tax_id"])),
                        _text(raw_data.get(COLUMNS["buyer_tax_id"])),
                        company_tax_id,
                    )
                )
            except RowValidationError:
                continue
        if directions == {InvoiceDirection.INPUT}:
            return SourceKind.INPUT_INVOICE
        if directions == {InvoiceDirection.OUTPUT}:
            return SourceKind.OUTPUT_INVOICE
        if not directions:
            raise RowValidationError("无法根据购销方税号判断进项或销项")
        raise RowValidationError("同一文件同时包含进项和销项，请拆分后导入")


def direction_for(seller_tax_id, buyer_tax_id, company_tax_id):
    if seller_tax_id == company_tax_id:
        return InvoiceDirection.OUTPUT
    if buyer_tax_id == company_tax_id:
        return InvoiceDirection.INPUT
    raise RowValidationError("发票购销双方均不是当前公司")


def _parse_row(row_number, raw_data, company_tax_id) -> ParsedRow:
    values = {name: _text(raw_data.get(column)) for name, column in COLUMNS.items()}
    issue = _validate_values(values, raw_data, company_tax_id)
    if issue is not None:
        return ParsedRow(
            row_number=row_number,
            raw_data=raw_data,
            normalized=None,
            issues=(issue,),
        )

    return ParsedRow(
        row_number=row_number,
        raw_data=raw_data,
        normalized=NormalizedInvoiceRow(
            direction=direction_for(
                values["seller_tax_id"], values["buyer_tax_id"], company_tax_id
            ),
            invoice_number=values["invoice_number"],
            seller_tax_id=values["seller_tax_id"],
            seller_name=values["seller_name"],
            buyer_tax_id=values["buyer_tax_id"],
            buyer_name=values["buyer_name"],
            issue_date=_parse_date(raw_data[COLUMNS["issue_date"]]),
            total_amount=_parse_amount(raw_data[COLUMNS["total_amount"]]),
            status=_normalize_status(values["status"]),
        ),
    )


def _validate_values(values, raw_data, company_tax_id) -> RowIssue | None:
    if not values["invoice_number"]:
        return RowIssue("required", "发票号码不能为空", "invoice_number")
    try:
        _parse_amount(raw_data[COLUMNS["total_amount"]])
    except (InvalidOperation, TypeError, ValueError):
        return RowIssue("amount", "发票金额不合法", "total_amount")
    try:
        _parse_date(raw_data[COLUMNS["issue_date"]])
    except (TypeError, ValueError):
        return RowIssue("date", "开票日期不合法", "issue_date")
    try:
        direction_for(values["seller_tax_id"], values["buyer_tax_id"], company_tax_id)
    except RowValidationError:
        return RowIssue("direction", "发票购销双方均不是当前公司", "direction")
    if _normalize_status(values["status"]) is None:
        return RowIssue("status", "发票状态不合法", "status")
    return None


def _parse_amount(value) -> Decimal:
    amount = Decimal(str(value).replace(",", "").strip())
    if (
        not amount.is_finite()
        or amount <= 0
        or amount.as_tuple().exponent < -MONEY_DECIMAL_PLACES
        or amount > MAX_TOTAL_AMOUNT
    ):
        raise ValueError("发票金额必须大于零")
    return amount


def _parse_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    return date.fromisoformat(text.replace("/", "-"))


def _normalize_status(value: str) -> str | None:
    if value == "正常":
        return InvoiceStatus.NORMAL
    if value == "作废":
        return InvoiceStatus.VOID
    if "红" in value:
        return InvoiceStatus.RED
    return None


def _text(value) -> str:
    return str(value).strip() if value is not None else ""
