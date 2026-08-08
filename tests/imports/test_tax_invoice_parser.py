import runpy
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook

from apps.imports.choices import SourceKind
from apps.imports.services import confirm_batch, stage_upload
from apps.imports.types import RowValidationError
from apps.ledger.choices import InvoiceDirection, InvoiceStatus
from apps.ledger.models import Invoice
from apps.parties.models import Counterparty

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"
BASE_SETTINGS_PATH = Path(__file__).parents[2] / "config" / "settings" / "base.py"
COLUMNS = [
    "发票号码",
    "销售方纳税人识别号",
    "销售方名称",
    "购买方纳税人识别号",
    "购买方名称",
    "开票日期",
    "价税合计",
    "发票状态",
]


@pytest.fixture
def tax_input_fixture():
    fixture_path = FIXTURES_DIR / "tax_input_invoices.xlsx"
    return SimpleUploadedFile(fixture_path.name, fixture_path.read_bytes())


@pytest.fixture
def tax_output_fixture():
    fixture_path = FIXTURES_DIR / "tax_output_invoices.xlsx"
    return SimpleUploadedFile(fixture_path.name, fixture_path.read_bytes())


def test_company_as_buyer_creates_input_invoice(tax_input_fixture, settings):
    from apps.imports.parsers.tax_invoice import TaxInvoiceImporter

    settings.COMPANY_TAX_ID = "91320281TEST000001"

    row = next(TaxInvoiceImporter().parse(tax_input_fixture)).normalized

    assert row.direction == InvoiceDirection.INPUT
    assert row.total_amount == Decimal("2000.00")


def test_company_as_seller_creates_output_invoice(tax_output_fixture, settings):
    from apps.imports.parsers.tax_invoice import TaxInvoiceImporter

    settings.COMPANY_TAX_ID = "91320281TEST000001"

    row = next(TaxInvoiceImporter().parse(tax_output_fixture)).normalized

    assert row.direction == InvoiceDirection.OUTPUT


@pytest.mark.parametrize(
    ("fixture_name", "expected_source_kind"),
    [
        ("tax_input_invoices.xlsx", SourceKind.INPUT_INVOICE),
        ("tax_output_invoices.xlsx", SourceKind.OUTPUT_INVOICE),
    ],
)
def test_tax_invoice_importer_infers_source_kind_from_company_tax_id(
    fixture_name, expected_source_kind, settings
):
    from apps.imports.parsers.tax_invoice import TaxInvoiceImporter

    settings.COMPANY_TAX_ID = "91320281TEST000001"
    fixture_path = FIXTURES_DIR / fixture_name
    file_obj = SimpleUploadedFile(fixture_path.name, fixture_path.read_bytes())

    assert TaxInvoiceImporter().infer_source_kind(file_obj) == expected_source_kind


def test_tax_invoice_importer_rejects_mixed_input_and_output_rows(settings):
    from apps.imports.parsers.tax_invoice import TaxInvoiceImporter

    settings.COMPANY_TAX_ID = "91320281TEST000001"
    file_obj = _workbook_file(
        _valid_row(),
        _valid_row(
            销售方纳税人识别号="91320281TEST000001",
            购买方纳税人识别号="913200000000000002",
        ),
    )

    with pytest.raises(RowValidationError, match="同时包含进项和销项"):
        TaxInvoiceImporter().infer_source_kind(file_obj)


def test_tax_invoice_importer_infers_direction_when_other_fields_are_invalid(settings):
    from apps.imports.parsers.tax_invoice import TaxInvoiceImporter

    settings.COMPANY_TAX_ID = "91320281TEST000001"
    file_obj = _workbook_file(_valid_row(发票号码=""))

    assert TaxInvoiceImporter().infer_source_kind(file_obj) == SourceKind.INPUT_INVOICE


def test_company_tax_id_is_read_from_environment(monkeypatch):
    monkeypatch.setenv("COMPANY_TAX_ID", "91320281TEST000001")

    base_settings = runpy.run_path(BASE_SETTINGS_PATH)

    assert base_settings["COMPANY_TAX_ID"] == "91320281TEST000001"


@pytest.mark.parametrize(
    ("filename", "headers"),
    [
        ("tax-invoices.csv", COLUMNS),
        ("tax-invoices.xlsx", COLUMNS[:-1]),
    ],
)
def test_tax_invoice_importer_rejects_non_matching_template(filename, headers):
    from apps.imports.parsers.tax_invoice import TaxInvoiceImporter

    assert not TaxInvoiceImporter().supports(filename, headers)


def test_tax_invoice_importer_rejects_workbook_without_invoice_sheet(settings):
    from apps.imports.parsers.tax_invoice import TaxInvoiceImporter

    settings.COMPANY_TAX_ID = "91320281TEST000001"
    file_obj = _workbook_file(_valid_row(), sheet_name="错误工作表")

    with pytest.raises(RowValidationError, match="发票基础信息"):
        list(TaxInvoiceImporter().parse(file_obj))


@pytest.mark.parametrize(
    ("raw_status", "expected_status"),
    [
        ("正常", InvoiceStatus.NORMAL),
        ("作废", InvoiceStatus.VOID),
        ("红冲", InvoiceStatus.RED),
        ("红字发票", InvoiceStatus.RED),
    ],
)
def test_tax_invoice_status_is_normalized(raw_status, expected_status, settings):
    from apps.imports.parsers.tax_invoice import TaxInvoiceImporter

    settings.COMPANY_TAX_ID = "91320281TEST000001"
    file_obj = _workbook_file(_valid_row(发票状态=raw_status))

    row = next(TaxInvoiceImporter().parse(file_obj)).normalized

    assert row.status == expected_status


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("发票号码", "", "required"),
        ("价税合计", "not-a-number", "amount"),
        ("价税合计", "Infinity", "amount"),
        ("价税合计", "1.234", "amount"),
        ("价税合计", "10000000000000000.00", "amount"),
        ("开票日期", "2026/99/99", "date"),
        ("购买方纳税人识别号", "not-the-company", "direction"),
    ],
)
def test_invalid_tax_invoice_row_is_isolated_with_its_source_row(
    field, value, expected_code, settings
):
    from apps.imports.parsers.tax_invoice import TaxInvoiceImporter

    settings.COMPANY_TAX_ID = "91320281TEST000001"
    bad_row = _valid_row()
    bad_row[field] = value
    file_obj = _workbook_file(_valid_row(), bad_row)

    rows = list(TaxInvoiceImporter().parse(file_obj))

    assert rows[0].normalized is not None
    assert rows[1].row_number == 3
    assert rows[1].normalized is None
    assert rows[1].issues[0].code == expected_code


def test_tax_invoice_accepts_largest_decimal_field_amount(settings):
    from apps.imports.parsers.tax_invoice import TaxInvoiceImporter

    settings.COMPANY_TAX_ID = "91320281TEST000001"
    file_obj = _workbook_file(_valid_row(价税合计="9999999999999999.99"))

    row = next(TaxInvoiceImporter().parse(file_obj)).normalized

    assert row.total_amount == Decimal("9999999999999999.99")


@pytest.mark.django_db
def test_tax_invoice_staging_skips_duplicates_and_preserves_source_data(
    finance_user, settings, tax_input_fixture, tmp_path
):
    settings.COMPANY_TAX_ID = "91320281TEST000001"
    settings.MEDIA_ROOT = tmp_path
    Counterparty.objects.create(
        name="测试供应商",
        normalized_name="测试供应商",
        tax_id="913200000000000001",
        is_supplier=True,
    )

    batch = stage_upload(
        tax_input_fixture,
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
    )

    assert (
        batch.total_rows,
        batch.valid_rows,
        batch.duplicate_rows,
        batch.error_rows,
    ) == (
        2,
        1,
        1,
        0,
    )
    assert confirm_batch(batch.id, finance_user).posted_rows == 1
    invoice = Invoice.objects.get()
    assert invoice.source_row == 2
    assert invoice.source_payload["发票号码"] == "INPUT-001"


def _valid_row(**overrides):
    row = {
        "发票号码": "INPUT-001",
        "销售方纳税人识别号": "913200000000000001",
        "销售方名称": "测试供应商",
        "购买方纳税人识别号": "91320281TEST000001",
        "购买方名称": "顺达",
        "开票日期": "2026-07-01",
        "价税合计": "2000.00",
        "发票状态": "正常",
    }
    row.update(overrides)
    return row


def _workbook_file(*rows, sheet_name="发票基础信息"):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet_name
    worksheet.append(COLUMNS)
    for row in rows:
        worksheet.append([row[column] for column in COLUMNS])
    file_obj = BytesIO()
    workbook.save(file_obj)
    file_obj.name = "tax-invoices.xlsx"
    file_obj.seek(0)
    return file_obj
