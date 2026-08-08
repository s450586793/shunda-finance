import re
from collections.abc import Iterable

import xlrd

from apps.imports.choices import SourceKind
from apps.imports.types import (
    NormalizedTransactionRow,
    ParsedRow,
    RowIssue,
    RowValidationError,
)
from apps.ledger.choices import MoneyChannel, MoneyDirection

from .csv_utils import parse_datetime, parse_decimal, text

BANK_HEADERS = [
    "交易时间",
    "收入金额",
    "支出金额",
    "账户余额",
    "对方账号",
    "对方户名",
    "对方开户行",
    "摘要",
]
class AgriculturalBankImporter:
    source_kinds = frozenset({SourceKind.BANK})

    def supports(self, filename: str, headers: list[str]) -> bool:
        return filename.lower().endswith(".xls") and headers == BANK_HEADERS

    def parse(self, file_obj) -> Iterable[ParsedRow]:
        file_obj.seek(0)
        workbook = xlrd.open_workbook(file_contents=file_obj.read())
        try:
            sheet, account_identifier = find_agricultural_bank_sheet(workbook)
            rows = [sheet.row_values(index) for index in range(sheet.nrows)]
        finally:
            file_obj.seek(0)

        for row_number, values in enumerate(rows[3:], start=4):
            if not any(text(value) for value in values):
                continue
            raw_data = dict(zip(BANK_HEADERS, values, strict=False))
            yield _parse_row(row_number, raw_data, account_identifier)


def parse_bank_amount(income, expense):
    income_amount = parse_decimal(income, blank_as_zero=True)
    expense_amount = parse_decimal(expense, blank_as_zero=True)
    if (income_amount > 0) == (expense_amount > 0):
        raise RowValidationError("收入金额和支出金额必须且只能有一个大于零")
    if income_amount > 0:
        return MoneyDirection.INFLOW, income_amount
    return MoneyDirection.OUTFLOW, expense_amount


def find_agricultural_bank_sheet(workbook):
    matches = []
    for sheet in workbook.sheets():
        account_identifier = _statement_account_identifier(sheet)
        if account_identifier is not None:
            matches.append((sheet, account_identifier))
    if not matches:
        raise RowValidationError("无法识别农业银行流水工作表")
    if len(matches) > 1:
        raise RowValidationError("农业银行流水工作表不唯一")
    return matches[0]


def _statement_account_identifier(sheet) -> str | None:
    if sheet.nrows < 3:
        return None
    title = [text(value) for value in sheet.row_values(0)]
    if not title or title[0] != "账户明细" or any(title[1:]):
        return None
    metadata = [text(value) for value in sheet.row_values(1)]
    account_identifier = _metadata_value(metadata, "账号")
    if not account_identifier:
        return None
    if not all(_metadata_value(metadata, label) for label in ("户名", "币种", "起止日期")):
        return None
    if [text(value) for value in sheet.row_values(2)] != BANK_HEADERS:
        return None
    return account_identifier


def _metadata_value(values, label: str) -> str | None:
    pattern = re.compile(rf"^{label}\s*[:：]\s*(\S.*)$")
    for value in values:
        matched = pattern.match(value)
        if matched:
            return matched.group(1).strip()
    return None


def _parse_row(row_number, raw_data, account_identifier) -> ParsedRow:
    try:
        direction, amount = parse_bank_amount(
            raw_data["收入金额"], raw_data["支出金额"]
        )
    except RowValidationError as exc:
        return _invalid_row(row_number, raw_data, "amount", str(exc), "amount")
    try:
        occurred_at = parse_datetime(raw_data["交易时间"])
    except RowValidationError as exc:
        return _invalid_row(row_number, raw_data, "datetime", str(exc), "occurred_at")
    try:
        balance = parse_decimal(raw_data["账户余额"])
    except RowValidationError as exc:
        return _invalid_row(row_number, raw_data, "balance", str(exc), "balance_after")

    return ParsedRow(
        row_number=row_number,
        raw_data=raw_data,
        normalized=NormalizedTransactionRow(
            channel=MoneyChannel.BANK,
            direction=direction,
            occurred_at=occurred_at,
            amount=amount,
            balance_after=balance,
            account_identifier=account_identifier,
            counterparty_name=text(raw_data["对方户名"]),
            counterparty_account=text(raw_data["对方账号"]),
            transaction_id="",
            summary=text(raw_data["摘要"]),
        ),
    )


def _invalid_row(row_number, raw_data, code, message, field) -> ParsedRow:
    return ParsedRow(
        row_number=row_number,
        raw_data=raw_data,
        normalized=None,
        issues=(RowIssue(code, message, field),),
    )
