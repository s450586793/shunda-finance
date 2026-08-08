from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import xlwt
from openpyxl import Workbook
from pypdf import PdfWriter

FIXTURES = Path(__file__).resolve().parent
RAILWAY_FIXTURES = FIXTURES / "synthetic_railway"

COMPANY_TAX_ID = "91320281TEST000001"
SUPPLIER_TAX_ID = "91310000TEST000001"
COUNTERPARTY_DISPLAY_NAME = "测试铁路物流收款专户"
COUNTERPARTY_LEGAL_NAME = "测试铁路物流有限公司"
BANK_ACCOUNT_ID = "TEST-BANK-RAIL-0001"
INVOICE_NUMBER = "00000000000000000001"

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
INVOICE_HEADERS = [
    "发票号码",
    "销售方纳税人识别号",
    "销售方名称",
    "购买方纳税人识别号",
    "购买方名称",
    "开票日期",
    "价税合计",
    "发票状态",
]
RAILWAY_PAYMENTS = [
    ("2026-06-01 10:00:00", Decimal("800.00")),
    ("2026-06-02 10:00:00", Decimal("4400.00")),
    ("2026-06-03 10:00:00", Decimal("2700.00")),
    ("2026-06-04 10:00:00", Decimal("2900.00")),
    ("2026-06-09 10:00:00", Decimal("850.00")),
    ("2026-06-14 10:00:00", Decimal("3750.00")),
    ("2026-06-16 10:00:00", Decimal("2000.00")),
    ("2026-06-17 10:00:00", Decimal("8000.00")),
    ("2026-06-18 10:00:00", Decimal("8100.00")),
    ("2026-06-19 10:00:00", Decimal("2750.00")),
    ("2026-06-23 10:00:00", Decimal("4150.00")),
    ("2026-06-24 10:00:00", Decimal("5800.00")),
    ("2026-06-27 10:00:00", Decimal("850.00")),
]


def _write_xls(path: Path, sheets: list[tuple[str, list[list[object]]]]) -> None:
    workbook = xlwt.Workbook(encoding="utf-8")
    for title, rows in sheets:
        worksheet = workbook.add_sheet(title)
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                worksheet.write(row_index, column_index, value)
    workbook.save(str(path))


def _write_xlsx(path: Path, rows: list[list[object]]) -> None:
    workbook = Workbook()
    workbook.properties.creator = "Shunda synthetic fixture generator"
    workbook.properties.created = datetime(2026, 1, 1, tzinfo=UTC)
    workbook.properties.modified = datetime(2026, 1, 1, tzinfo=UTC)
    worksheet = workbook.active
    worksheet.title = "发票基础信息"
    for row in rows:
        worksheet.append(row)
    workbook.save(path)


def _general_bank_rows() -> list[list[object]]:
    return [
        ["账户明细", "", "", "", "", "", "", ""],
        [
            "账号:test-bank-account",
            "户名:测试公司",
            "币种:人民币",
            "",
            "",
            "起止日期: 2026年07月27日 - 2026年07月27日",
            "",
            "",
        ],
        BANK_HEADERS,
        [
            "2026-07-27 13:14:14",
            "",
            "2000.00",
            "100000.00",
            "TEST-COUNTERPARTY-0001",
            COUNTERPARTY_DISPLAY_NAME,
            "测试银行",
            "运输费",
        ],
        [
            "2026-07-27 13:14:14",
            "3200.00",
            "",
            "102000.00",
            "TEST-COUNTERPARTY-0002",
            "测试收款方",
            "测试银行",
            "运费收入",
        ],
    ]


def _write_general_bank_fixtures() -> None:
    valid = _general_bank_rows()
    _write_xls(FIXTURES / "agricultural_bank.xls", [("交易明细", valid)])
    _write_xls(
        FIXTURES / "agricultural_bank_ambiguous_sheets.xls",
        [("第一份", valid), ("第二份", valid)],
    )

    invalid_header = [row.copy() for row in valid]
    invalid_header.insert(2, [""] * len(BANK_HEADERS))
    _write_xls(
        FIXTURES / "agricultural_bank_invalid_header_position.xls",
        [("交易明细", invalid_header)],
    )

    invalid_metadata = [row.copy() for row in valid]
    invalid_metadata[1] = [""] * len(BANK_HEADERS)
    _write_xls(
        FIXTURES / "agricultural_bank_invalid_metadata.xls",
        [("交易明细", invalid_metadata)],
    )

    invalid_title = [row.copy() for row in valid]
    invalid_title[0][0] = "错误标题"
    _write_xls(
        FIXTURES / "agricultural_bank_invalid_title.xls",
        [("交易明细", invalid_title)],
    )
    _write_xls(
        FIXTURES / "agricultural_bank_renamed_second_sheet.xls",
        [
            ("说明", [["说明"], ["该表不包含农业银行流水结构"]]),
            ("重命名交易明细", valid),
        ],
    )


def _write_invoice_fixtures() -> None:
    input_row = [
        "INPUT-001",
        "913200000000000001",
        "测试供应商",
        COMPANY_TAX_ID,
        "顺达",
        "2026-07-01",
        "2000.00",
        "正常",
    ]
    _write_xlsx(
        FIXTURES / "tax_input_invoices.xlsx",
        [INVOICE_HEADERS, input_row, input_row.copy()],
    )
    _write_xlsx(
        FIXTURES / "tax_output_invoices.xlsx",
        [
            INVOICE_HEADERS,
            [
                "OUTPUT-001",
                COMPANY_TAX_ID,
                "顺达",
                "913200000000000002",
                "测试客户",
                "2026-07-02",
                "3060.00",
                "红冲",
            ],
        ],
    )
    _write_xlsx(
        RAILWAY_FIXTURES / "input_invoices.xlsx",
        [
            INVOICE_HEADERS,
            [
                "SYN-RAIL-20260707-01",
                SUPPLIER_TAX_ID,
                COUNTERPARTY_LEGAL_NAME,
                COMPANY_TAX_ID,
                "顺达测试公司",
                "2026-07-07",
                "2000.00",
                "正常",
            ],
            [
                "SYN-RAIL-20260707-02",
                SUPPLIER_TAX_ID,
                COUNTERPARTY_LEGAL_NAME,
                COMPANY_TAX_ID,
                "顺达测试公司",
                "2026-07-07",
                "46050.00",
                "正常",
            ],
        ],
    )


def _write_railway_bank_fixture() -> None:
    balance = Decimal("97050.00")
    rows: list[list[object]] = [
        ["账户明细", "", "", "", "", "", "", ""],
        [
            f"账号：{BANK_ACCOUNT_ID}",
            "户名：顺达测试账户",
            "币种：人民币",
            "起止日期：2026-06-01 至 2026-06-30",
            "",
            "",
            "",
            "",
        ],
        BANK_HEADERS,
    ]
    for occurred_at, amount in RAILWAY_PAYMENTS:
        balance -= amount
        rows.append(
            [
                occurred_at,
                "",
                f"{amount:.2f}",
                f"{balance:.2f}",
                "TEST-COUNTERPARTY-0001",
                COUNTERPARTY_DISPLAY_NAME,
                "测试银行",
                "测试铁路运输结算",
            ]
        )
    if balance != Decimal("50000.00"):
        raise RuntimeError("synthetic railway balance invariant failed")
    _write_xls(RAILWAY_FIXTURES / "bank_june.xls", [("交易明细", rows)])


def _write_invoice_pdf() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": "Synthetic invoice attachment"})
    with (FIXTURES / f"invoice_{INVOICE_NUMBER}.pdf").open("wb") as handle:
        writer.write(handle)


def _generate_fixtures() -> None:
    RAILWAY_FIXTURES.mkdir(parents=True, exist_ok=True)
    _write_general_bank_fixtures()
    _write_invoice_fixtures()
    _write_railway_bank_fixture()
    _write_invoice_pdf()


if __name__ == "__main__":
    _generate_fixtures()
