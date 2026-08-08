from pathlib import Path

import pytest
from django.contrib.auth.models import Group, User
from django.contrib.staticfiles import finders
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from openpyxl import Workbook

from apps.accounts.roles import Role
from apps.imports.forms import ImportUploadForm
from apps.imports.models import ImportBatch, SourceFile, StagedRow
from apps.ledger.models import FundingAccount, Invoice
from apps.parties.models import Counterparty

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"


def test_local_static_assets_are_discoverable():
    assert finders.find("css/app.css")
    assert finders.find("vendor/lucide.min.js")


def _invoice_file(name="tax_input_invoices.xlsx"):
    path = FIXTURES_DIR / "tax_input_invoices.xlsx"
    return SimpleUploadedFile(name, path.read_bytes())


def _invoice_file_with_issue():
    columns = [
        "发票号码",
        "销售方纳税人识别号",
        "销售方名称",
        "购买方纳税人识别号",
        "购买方名称",
        "开票日期",
        "价税合计",
        "发票状态",
    ]
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "发票基础信息"
    worksheet.append(columns)
    worksheet.append(
        [
            "",
            "913200000000000001",
            "测试供应商",
            "91320281TEST000001",
            "顺达",
            "2026-07-01",
            "2000.00",
            "正常",
        ]
    )
    from io import BytesIO

    content = BytesIO()
    workbook.save(content)
    workbook.close()
    return SimpleUploadedFile("存在问题.xlsx", content.getvalue())


def _wechat_file():
    path = FIXTURES_DIR / "wechat_transactions.csv"
    return SimpleUploadedFile(path.name, path.read_bytes())


def _issue_batch(finance_user, count):
    batch = ImportBatch.objects.create(
        source_kind="bank",
        status="previewed",
        total_rows=count,
        error_rows=count,
        created_by=finance_user,
    )
    StagedRow.objects.bulk_create(
        [
            StagedRow(
                batch=batch,
                row_number=row_number,
                raw_data={"sensitive": f"raw-{row_number}"},
                normalized_data={"sensitive": f"normalized-{row_number}"},
                issues=[
                    {
                        "field": "amount",
                        "code": "invalid",
                        "message": f"问题-{row_number}",
                    }
                ],
            )
            for row_number in range(1, count + 1)
        ]
    )
    return batch


@pytest.fixture
def import_context(settings, tmp_path):
    settings.COMPANY_TAX_ID = "91320281TEST000001"
    settings.MEDIA_ROOT = tmp_path
    Counterparty.objects.create(
        name="测试供应商",
        normalized_name="测试供应商",
        tax_id="913200000000000001",
        is_supplier=True,
    )


@pytest.mark.django_db
def test_anonymous_import_page_redirects_to_login(client):
    response = client.get("/imports/")

    assert response.status_code == 302
    assert response.headers["Location"] == "/accounts/login/?next=/imports/"


@pytest.mark.django_db
def test_login_page_loads_local_assets_and_redirects_valid_user(client, finance_user):
    page = client.get("/accounts/login/")

    assert page.status_code == 200
    assert 'href="/static/css/app.css"' in page.content.decode()
    assert 'src="/static/vendor/lucide.min.js"' in page.content.decode()
    response = client.post(
        "/accounts/login/", {"username": finance_user.username, "password": "secret"}
    )
    assert response.status_code == 302
    assert response.headers["Location"] == "/ledger/invoices/"


@pytest.mark.django_db
def test_owner_cannot_read_or_post_import(owner_client):
    assert owner_client.get("/imports/").status_code == 403
    response = owner_client.post(
        "/imports/",
        {"source_kind": "input_invoice", "file": _invoice_file()},
    )

    assert response.status_code == 403
    assert not ImportBatch.objects.exists()


@pytest.mark.django_db
def test_existing_dual_role_user_cannot_access_finance_http_endpoint(client):
    user = User.objects.create_user("dual-http")
    user.groups.add(
        Group.objects.get(name=Role.FINANCE.value),
        Group.objects.get(name=Role.OWNER.value),
    )
    client.force_login(user)

    assert client.get("/imports/").status_code == 403
    response = client.post(
        "/imports/",
        {"source_kind": "input_invoice", "file": _invoice_file()},
    )
    assert response.status_code == 403
    assert not ImportBatch.objects.exists()


@pytest.mark.django_db
def test_finance_sees_preview_before_confirm(finance_client, import_context):
    response = finance_client.post(
        "/imports/",
        {"source_kind": "input_invoice", "file": _invoice_file()},
    )

    assert response.status_code == 302
    assert Invoice.objects.count() == 0
    preview = finance_client.get(response.headers["Location"])
    body = preview.content.decode()
    assert preview.status_code == 200
    assert "有效数据" in body
    assert "重复数据" in body
    assert "异常数据" in body
    assert "2026-07-01" in body
    assert "确认导入" in body
    assert "/media/" not in body
    assert "下载原文件" in body


@pytest.mark.django_db
def test_finance_upload_auto_detects_input_invoice(finance_client, import_context):
    response = finance_client.post("/imports/", {"file": _invoice_file()})

    assert response.status_code == 302
    batch = ImportBatch.objects.get()
    assert batch.source_kind == "input_invoice"


@pytest.mark.django_db
def test_finance_upload_auto_detects_wechat_statement(finance_client, import_context):
    FundingAccount.objects.create(
        channel="wechat",
        name="微信账户",
        identifier="零钱",
        masked_identifier="零钱",
    )
    Counterparty.objects.create(
        name="测试客户",
        normalized_name="测试客户",
        is_customer=True,
    )

    response = finance_client.post("/imports/", {"file": _wechat_file()})

    assert response.status_code == 302
    assert ImportBatch.objects.get().source_kind == "wechat"


@pytest.mark.django_db
def test_confirm_is_separate_csrf_protected_post(finance_user, import_context):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(finance_user)
    upload_page = csrf_client.get("/imports/")
    token = upload_page.cookies["csrftoken"].value
    staged = csrf_client.post(
        "/imports/",
        {
            "source_kind": "input_invoice",
            "file": _invoice_file(),
            "csrfmiddlewaretoken": token,
        },
    )
    confirm_url = f"{staged.headers['Location']}confirm/"

    assert csrf_client.get(confirm_url).status_code == 405
    assert csrf_client.post(confirm_url).status_code == 403
    confirmed = csrf_client.post(confirm_url, {"csrfmiddlewaretoken": token})

    assert confirmed.status_code == 302
    assert Invoice.objects.count() == 1
    assert (
        csrf_client.post(confirm_url, {"csrfmiddlewaretoken": token}).status_code == 302
    )
    assert Invoice.objects.count() == 1


@pytest.mark.django_db
def test_owner_cannot_confirm_staged_batch(finance_client, owner_user, import_context):
    staged = finance_client.post(
        "/imports/",
        {"source_kind": "input_invoice", "file": _invoice_file()},
    )
    finance_client.force_login(owner_user)

    assert (
        finance_client.post(f"{staged.headers['Location']}confirm/").status_code == 403
    )
    assert Invoice.objects.count() == 0


@pytest.mark.django_db
def test_upload_error_is_clear_and_does_not_expose_exception(
    finance_client, import_context
):
    response = finance_client.post(
        "/imports/",
        {
            "source_kind": "input_invoice",
            "file": SimpleUploadedFile("broken.xlsx", b"not-a-workbook"),
        },
    )

    body = response.content.decode()
    assert response.status_code == 400
    assert "文件扩展名与实际内容不一致" in body
    assert "BadZipFile" not in body
    assert "/home/" not in body


@pytest.mark.django_db
def test_duplicate_source_file_returns_safe_form_error(finance_client, import_context):
    payload = {"source_kind": "input_invoice", "file": _invoice_file()}
    assert finance_client.post("/imports/", payload).status_code == 302

    response = finance_client.post(
        "/imports/",
        {"source_kind": "input_invoice", "file": _invoice_file()},
    )

    assert response.status_code == 400
    assert "相同 SHA-256 的原始文件已导入" in response.content.decode()
    assert ImportBatch.objects.count() == 1


@pytest.mark.django_db
def test_upload_form_rejects_unsupported_extension(finance_client):
    response = finance_client.post(
        "/imports/",
        {
            "source_kind": "input_invoice",
            "file": SimpleUploadedFile("invoice.pdf", b"not-an-import"),
        },
    )

    assert response.status_code == 400
    assert "仅支持 CSV、TXT、XLS 和 XLSX 文件" in response.content.decode()
    assert not ImportBatch.objects.exists()


def test_upload_form_accepts_exact_byte_limit_and_rejects_one_byte_over(settings):
    settings.IMPORT_MAX_UPLOAD_BYTES = 4
    exact = ImportUploadForm(
        data={"source_kind": "input_invoice"},
        files={"file": SimpleUploadedFile("exact.csv", b"1234")},
    )
    over = ImportUploadForm(
        data={"source_kind": "input_invoice"},
        files={"file": SimpleUploadedFile("over.csv", b"12345")},
    )

    assert exact.is_valid()
    assert not over.is_valid()
    assert over.errors["file"] == ["文件大小超过系统允许的上限"]


@pytest.mark.django_db
def test_issue_csv_download_contains_row_details_and_requires_finance(
    finance_client, owner_user, import_context
):
    staged = finance_client.post(
        "/imports/",
        {"source_kind": "input_invoice", "file": _invoice_file_with_issue()},
    )
    issue_url = f"{staged.headers['Location']}issues.csv"

    response = finance_client.get(issue_url)
    content = b"".join(response.streaming_content).decode("utf-8-sig")
    assert response.status_code == 200
    assert "行号,字段,问题代码,问题说明" in content
    assert "2,invoice_number,required,发票号码不能为空" in content
    finance_client.force_login(owner_user)
    assert finance_client.get(issue_url).status_code == 403
    finance_client.logout()
    assert finance_client.get(issue_url).status_code == 302


@pytest.mark.django_db
def test_preview_paginates_issue_rows_by_100_without_loading_source_json(
    finance_client, finance_user
):
    batch = _issue_batch(finance_user, 205)

    with CaptureQueriesContext(connection) as queries:
        first = finance_client.get(f"/imports/{batch.pk}/")

    issue_page = first.context["issue_page"]
    assert issue_page.paginator.per_page == 100
    assert [row.row_number for row in issue_page.object_list] == list(range(1, 101))
    issue_selects = [
        query["sql"]
        for query in queries
        if "imports_stagedrow" in query["sql"].lower()
        and "issues" in query["sql"].lower()
    ]
    assert issue_selects
    assert all(
        "raw_data" not in sql and "normalized_data" not in sql for sql in issue_selects
    )

    second = finance_client.get(f"/imports/{batch.pk}/", {"issue_page": 2})
    third = finance_client.get(f"/imports/{batch.pk}/", {"issue_page": 3})
    assert [row.row_number for row in second.context["issue_page"].object_list] == list(
        range(101, 201)
    )
    assert [row.row_number for row in third.context["issue_page"].object_list] == list(
        range(201, 206)
    )


@pytest.mark.django_db
def test_issue_csv_defers_and_limits_staged_row_query(finance_client, finance_user):
    batch = _issue_batch(finance_user, 205)
    issue_url = f"/imports/{batch.pk}/issues.csv"

    with CaptureQueriesContext(connection) as response_queries:
        response = finance_client.get(issue_url)

    assert not any(
        "imports_stagedrow" in query["sql"].lower() for query in response_queries
    )

    with CaptureQueriesContext(connection) as stream_queries:
        content = b"".join(response.streaming_content).decode("utf-8-sig")

    row_selects = [
        query["sql"]
        for query in stream_queries
        if "imports_stagedrow" in query["sql"].lower()
    ]
    assert len(row_selects) == 1
    assert "row_number" in row_selects[0]
    assert "issues" in row_selects[0]
    assert "raw_data" not in row_selects[0]
    assert "normalized_data" not in row_selects[0]
    assert "问题-1" in content
    assert "问题-205" in content


@pytest.mark.django_db
def test_original_download_streams_stored_bytes_with_safe_disposition(
    finance_client, owner_user, import_context
):
    uploaded = _invoice_file('财务"明细.xlsx')
    original = uploaded.read()
    uploaded.seek(0)
    staged = finance_client.post(
        "/imports/",
        {"source_kind": "input_invoice", "file": uploaded},
    )
    source_url = f"{staged.headers['Location']}source/"

    response = finance_client.get(source_url)
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == original
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert "\n" not in response.headers["Content-Disposition"]
    finance_client.force_login(owner_user)
    assert finance_client.get(source_url).status_code == 403
    finance_client.logout()
    assert finance_client.get(source_url).status_code == 302


@pytest.mark.django_db
def test_missing_original_file_returns_404(finance_client, finance_user):
    batch = ImportBatch.objects.create(source_kind="bank", created_by=finance_user)

    assert finance_client.get(f"/imports/{batch.pk}/source/").status_code == 404


@pytest.mark.django_db
def test_source_record_with_missing_storage_file_returns_safe_404(
    finance_client, import_context
):
    finance_client.post(
        "/imports/",
        {"source_kind": "input_invoice", "file": _invoice_file("private-ledger.xlsx")},
    )
    batch = ImportBatch.objects.get()
    source = batch.source_files.get()
    source.file.storage.delete(source.file.name)
    finance_client.raise_request_exception = False

    response = finance_client.get(f"/imports/{batch.pk}/source/")

    assert response.status_code == 404
    body = response.content.decode()
    assert "原文件已丢失" in body
    assert "private-ledger.xlsx" not in body
    assert "/tmp/" not in body


@pytest.mark.django_db
def test_unreadable_source_storage_returns_safe_503(
    finance_client, import_context, monkeypatch
):
    finance_client.post(
        "/imports/",
        {"source_kind": "input_invoice", "file": _invoice_file("private-ledger.xlsx")},
    )
    batch = ImportBatch.objects.get()
    storage = SourceFile._meta.get_field("file").storage

    def fail_open(*args, **kwargs):
        raise OSError("/private/storage/backend unavailable")

    monkeypatch.setattr(storage, "open", fail_open)
    finance_client.raise_request_exception = False

    response = finance_client.get(f"/imports/{batch.pk}/source/")

    assert response.status_code == 503
    body = response.content.decode()
    assert "原文件暂时无法读取，请稍后重试" in body
    assert "private-ledger.xlsx" not in body
    assert "/private/storage" not in body


@pytest.mark.django_db
def test_import_navigation_context_has_exact_entries(finance_client):
    response = finance_client.get("/imports/")
    body = response.content.decode()

    labels = [
        "总览",
        "导入中心",
        "人工核销",
        "结算批次",
        "应收应付",
        "往来单位",
        "操作记录",
    ]
    assert [item["label"] for item in response.context["navigation_items"]] == labels
    assert 'href="/imports/"' in body
    assert "https://" not in body


@pytest.mark.django_db
def test_rendered_ui_has_no_decorative_english_labels(
    client, finance_client, finance_user
):
    batch = _issue_batch(finance_user, 1)
    responses = [
        client.get("/accounts/login/"),
        finance_client.get("/imports/"),
        finance_client.get(f"/imports/{batch.pk}/"),
        finance_client.get("/ledger/invoices/"),
        finance_client.get("/ledger/transactions/"),
        finance_client.get("/parties/"),
    ]
    rendered = "\n".join(response.content.decode() for response in responses)

    for label in (
        "DATA INTAKE",
        "IMPORT PREVIEW",
        "LEDGER",
        "COUNTERPARTIES",
        "FINANCE LEDGER",
        ">SD<",
    ):
        assert label not in rendered


def test_mobile_table_icon_buttons_keep_44_pixel_hit_area():
    css = (Path(__file__).parents[2] / "static" / "css" / "app.css").read_text()
    table_button_rule = css.split(".data-table td .icon-button {", 1)[1].split("}", 1)[
        0
    ]

    assert "width: 44px;" in table_button_rule
    assert "min-height: 44px;" in table_button_rule


def test_desktop_buttons_keep_44_pixel_hit_area():
    css = (Path(__file__).parents[2] / "static" / "css" / "app.css").read_text()
    desktop_rules = css.split("@media (min-width: 900px) {", 1)[1].split(
        "@media (min-width: 1280px) {", 1
    )[0]

    assert ".toolbar select,\n  .button {" not in desktop_rules
    icon_button_rule = desktop_rules.rsplit(".icon-button {", 1)[1].split("}", 1)[0]
    assert "width: 44px;" in icon_button_rule
    assert "min-height: 44px;" in icon_button_rule
