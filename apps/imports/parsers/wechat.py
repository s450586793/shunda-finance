from collections.abc import Iterable

from apps.imports.choices import SourceKind
from apps.imports.types import (
    NormalizedTransactionRow,
    ParsedRow,
    RowIssue,
    RowValidationError,
)
from apps.ledger.choices import MoneyChannel, MoneyDirection

from .csv_utils import parse_datetime, parse_decimal, read_csv_rows, text

WECHAT_HEADERS = [
    "交易时间",
    "交易类型",
    "交易对方",
    "商品",
    "收/支",
    "金额(元)",
    "支付方式",
    "当前状态",
    "交易单号",
    "商户单号",
    "备注",
]
WECHAT_DIRECTION = {"收入": MoneyDirection.INFLOW, "支出": MoneyDirection.OUTFLOW}
SUCCESS_STATES = {"支付成功", "已收钱", "已转账", "对方已收钱"}


class WechatImporter:
    source_kinds = frozenset({SourceKind.WECHAT})

    def supports(self, filename: str, headers: list[str]) -> bool:
        return filename.lower().endswith(".csv") and set(WECHAT_HEADERS).issubset(headers)

    def parse(self, file_obj) -> Iterable[ParsedRow]:
        rows = read_csv_rows(file_obj)
        header_index = _find_header_row(rows)
        headers = [text(value) for value in rows[header_index]]
        for row_number, values in enumerate(rows[header_index + 1 :], start=header_index + 2):
            if not any(text(value) for value in values):
                continue
            raw_data = dict(zip(headers, values, strict=False))
            yield _parse_row(row_number, raw_data)


def _find_header_row(rows) -> int:
    for index, values in enumerate(rows):
        if set(WECHAT_HEADERS).issubset({text(value) for value in values}):
            return index
    raise RowValidationError("微信流水缺少标准表头")


def _parse_row(row_number, raw_data) -> ParsedRow:
    direction = WECHAT_DIRECTION.get(text(raw_data.get("收/支")))
    if direction is None:
        return _invalid_row(row_number, raw_data, "direction", "微信记录不是收入或支出", "direction")
    state = text(raw_data.get("当前状态"))
    if state not in SUCCESS_STATES:
        return _invalid_row(
            row_number,
            raw_data,
            "status",
            f"微信交易尚未成功：{state}",
            "status",
        )
    try:
        occurred_at = parse_datetime(raw_data.get("交易时间"))
        amount = parse_decimal(text(raw_data.get("金额(元)")).replace("¥", ""))
        if amount <= 0:
            raise RowValidationError("微信交易金额必须大于零")
    except RowValidationError as exc:
        return _invalid_row(row_number, raw_data, "amount", str(exc), "amount")

    return ParsedRow(
        row_number=row_number,
        raw_data=raw_data,
        normalized=NormalizedTransactionRow(
            channel=MoneyChannel.WECHAT,
            direction=direction,
            occurred_at=occurred_at,
            amount=amount,
            balance_after=None,
            account_identifier=text(raw_data.get("支付方式")),
            counterparty_name=text(raw_data.get("交易对方")),
            counterparty_account="",
            transaction_id=text(raw_data.get("交易单号")),
            summary=text(raw_data.get("备注")),
        ),
    )


def _invalid_row(row_number, raw_data, code, message, field) -> ParsedRow:
    return ParsedRow(
        row_number=row_number,
        raw_data=raw_data,
        normalized=None,
        issues=(RowIssue(code, message, field),),
    )
