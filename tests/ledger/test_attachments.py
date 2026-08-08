from io import BytesIO
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from pypdf import PdfReader

from apps.core.models import AuditLog
from apps.ledger.attachments import (
    AttachmentStatus,
    attach_invoice_pdf,
    disable_attachment,
    extract_invoice_numbers,
)
from apps.ledger.models import Attachment
from tests.builders import make_invoice

INVOICE_NUMBER = "00000000000000000001"
FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / f"invoice_{INVOICE_NUMBER}.pdf"


@pytest.fixture
def invoice_record(finance_user):
    return make_invoice(finance_user, invoice_number=INVOICE_NUMBER)


@pytest.fixture
def invoice_pdf():
    return SimpleUploadedFile(
        f"dzfp_{INVOICE_NUMBER}_anonymized_202607.pdf",
        FIXTURE_PATH.read_bytes(),
        content_type="application/pdf",
    )


@pytest.fixture(autouse=True)
def attachment_media_root(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def test_fixture_is_synthetic_pdf_readable_by_pypdf():
    assert len(PdfReader(BytesIO(FIXTURE_PATH.read_bytes())).pages) == 1


@pytest.mark.django_db
def test_pdf_filename_links_unique_invoice(finance_user, invoice_record, invoice_pdf):
    attachment = attach_invoice_pdf(invoice_pdf, actor=finance_user)

    assert attachment.target == invoice_record
    assert attachment.status == AttachmentStatus.LINKED
    assert attachment.original_name == invoice_pdf.name
    assert attachment.uploaded_by == finance_user
    assert attachment.uploaded_at is not None
    assert AuditLog.objects.get(action="attachment.uploaded").target_id == str(attachment.pk)


@pytest.mark.django_db
def test_unknown_invoice_pdf_enters_claim_queue(finance_user, invoice_pdf):
    attachment = attach_invoice_pdf(invoice_pdf, actor=finance_user)

    assert attachment.status == AttachmentStatus.UNCLAIMED
    assert attachment.target is None


@pytest.mark.django_db
def test_pdf_text_links_when_filename_has_no_invoice_number(finance_user, invoice_record):
    uploaded = SimpleUploadedFile(
        "anonymized.pdf",
        _pdf_bytes(f"invoice number {INVOICE_NUMBER}"),
        content_type="application/pdf",
    )

    attachment = attach_invoice_pdf(uploaded, actor=finance_user)

    assert attachment.target == invoice_record
    assert attachment.status == AttachmentStatus.LINKED


@pytest.mark.django_db
def test_pdf_without_an_invoice_number_is_unclaimed(finance_user):
    uploaded = SimpleUploadedFile(
        "anonymized.pdf",
        _pdf_bytes("no invoice number in this attachment"),
        content_type="application/pdf",
    )

    attachment = attach_invoice_pdf(uploaded, actor=finance_user)

    assert attachment.status == AttachmentStatus.UNCLAIMED
    assert attachment.target is None


@pytest.mark.django_db
def test_pdf_with_multiple_numbers_is_unclaimed(finance_user, invoice_record):
    uploaded = SimpleUploadedFile(
        f"dzfp_{INVOICE_NUMBER}_00000000000000000002.pdf",
        FIXTURE_PATH.read_bytes(),
        content_type="application/pdf",
    )

    attachment = attach_invoice_pdf(uploaded, actor=finance_user)

    assert attachment.status == AttachmentStatus.UNCLAIMED
    assert attachment.target is None


@pytest.mark.django_db
def test_pdf_with_number_matching_multiple_invoices_is_unclaimed(finance_user, invoice_record):
    make_invoice(
        finance_user,
        invoice_number=invoice_record.invoice_number,
        seller_tax_id="913200000000000099",
    )
    uploaded = SimpleUploadedFile(
        f"dzfp_{INVOICE_NUMBER}_anonymized_202607.pdf",
        FIXTURE_PATH.read_bytes(),
        content_type="application/pdf",
    )

    attachment = attach_invoice_pdf(uploaded, actor=finance_user)

    assert attachment.status == AttachmentStatus.UNCLAIMED
    assert attachment.target is None


@pytest.mark.django_db
def test_duplicate_pdf_returns_existing_attachment_without_new_file_or_audit(
    finance_user, invoice_record, invoice_pdf, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    first = attach_invoice_pdf(invoice_pdf, actor=finance_user)
    duplicate = SimpleUploadedFile(invoice_pdf.name, FIXTURE_PATH.read_bytes())

    second = attach_invoice_pdf(duplicate, actor=finance_user)

    assert second == first
    assert Attachment.objects.count() == 1
    assert AuditLog.objects.filter(action="attachment.uploaded").count() == 1
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == [
        tmp_path / first.file.name
    ]


@pytest.mark.django_db
def test_finance_user_can_soft_disable_attachment_once_and_records_audit(
    finance_user, invoice_record, invoice_pdf, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    attachment = attach_invoice_pdf(invoice_pdf, actor=finance_user)

    first = disable_attachment(attachment.pk, actor=finance_user)
    disabled_at = first.disabled_at
    second = disable_attachment(attachment.pk, actor=finance_user)
    attachment.refresh_from_db()

    assert attachment.disabled_at is not None
    assert second.disabled_at == disabled_at
    assert attachment.status == AttachmentStatus.LINKED
    assert AuditLog.objects.filter(action="attachment.disabled").count() == 1
    assert not hasattr(attachment, "disable")
    assert attachment.file.storage.exists(attachment.file.name)
    with pytest.raises(RuntimeError, match="附件原始文件不允许物理删除"):
        attachment.file.delete()
    with pytest.raises(RuntimeError, match="正式财务记录不允许物理删除"):
        attachment.delete()


@pytest.mark.django_db
def test_attachment_disable_rejects_non_finance_actor(finance_user, owner_user, invoice_record, invoice_pdf):
    attachment = attach_invoice_pdf(invoice_pdf, actor=finance_user)

    with pytest.raises(PermissionDenied, match="财务"):
        disable_attachment(attachment.pk, actor=owner_user)

    attachment.refresh_from_db()
    assert attachment.disabled_at is None
    assert not AuditLog.objects.filter(action="attachment.disabled").exists()


@pytest.mark.django_db
def test_attachment_disable_rejects_unsaved_user(finance_user, invoice_record, invoice_pdf):
    attachment = attach_invoice_pdf(invoice_pdf, actor=finance_user)

    with pytest.raises(PermissionDenied, match="财务"):
        disable_attachment(attachment.pk, actor=User(username="unsaved-finance"))

    attachment.refresh_from_db()
    assert attachment.disabled_at is None


@pytest.mark.django_db
def test_attachment_rejects_non_finance_actor(owner_user, invoice_pdf):
    with pytest.raises(PermissionDenied, match="财务"):
        attach_invoice_pdf(invoice_pdf, actor=owner_user)

    assert not Attachment.objects.exists()


@pytest.mark.django_db
def test_attachment_upload_enforces_actual_byte_limit_when_declared_size_is_wrong(
    finance_user, settings
):
    settings.IMPORT_MAX_UPLOAD_BYTES = 4
    uploaded = SimpleUploadedFile("invoice.pdf", b"%PDF-oversized")
    uploaded.size = 1

    with pytest.raises(ValidationError, match="文件大小超过系统允许的上限"):
        attach_invoice_pdf(uploaded, actor=finance_user)

    assert uploaded.tell() == 0
    assert not Attachment.objects.exists()


@pytest.mark.django_db
def test_attachment_compensates_file_when_pdf_read_fails(finance_user, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    uploaded = SimpleUploadedFile("anonymized.pdf", b"not a pdf")

    with pytest.raises(ValidationError, match="文件扩展名与实际内容不一致"):
        attach_invoice_pdf(uploaded, actor=finance_user)

    assert not Attachment.objects.exists()
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.django_db
def test_filename_invoice_number_still_requires_valid_pdf(
    finance_user, invoice_record, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    uploaded = SimpleUploadedFile(
        f"dzfp_{INVOICE_NUMBER}_anonymized_202607.pdf",
        b"not a pdf",
        content_type="application/pdf",
    )

    with pytest.raises(ValidationError, match="文件扩展名与实际内容不一致"):
        attach_invoice_pdf(uploaded, actor=finance_user)

    assert not Attachment.objects.exists()
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_invoice_number_extraction_resets_file_pointer_after_structural_validation():
    uploaded = SimpleUploadedFile(
        f"dzfp_{INVOICE_NUMBER}_anonymized_202607.pdf",
        FIXTURE_PATH.read_bytes(),
        content_type="application/pdf",
    )
    uploaded.read(1)

    assert extract_invoice_numbers(uploaded.name, uploaded) == {INVOICE_NUMBER}
    assert uploaded.tell() == 0


@pytest.mark.django_db
def test_attachment_status_is_constrained_in_database(finance_user, invoice_record, invoice_pdf):
    attachment = attach_invoice_pdf(invoice_pdf, actor=finance_user)
    attachment.status = "unexpected"

    with pytest.raises(IntegrityError), transaction.atomic():
        attachment.save(update_fields=["status"])


@pytest.mark.django_db
def test_attachment_compensates_file_and_record_when_database_save_fails(
    finance_user, invoice_record, invoice_pdf, monkeypatch, settings, tmp_path
):
    from apps.ledger import attachments

    settings.MEDIA_ROOT = tmp_path
    original_save = Attachment.save

    def save_then_fail(instance, *args, **kwargs):
        original_save(instance, *args, **kwargs)
        raise RuntimeError("database save failed")

    monkeypatch.setattr(attachments.Attachment, "save", save_then_fail)

    with pytest.raises(RuntimeError, match="database save failed"):
        attach_invoice_pdf(invoice_pdf, actor=finance_user)

    assert not Attachment.objects.exists()
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.django_db
def test_attachment_compensates_file_and_record_when_audit_fails(
    finance_user, invoice_record, invoice_pdf, monkeypatch, settings, tmp_path
):
    from apps.ledger import attachments

    settings.MEDIA_ROOT = tmp_path
    monkeypatch.setattr(
        attachments,
        "record_audit",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit failed")),
    )

    with pytest.raises(RuntimeError, match="audit failed"):
        attach_invoice_pdf(invoice_pdf, actor=finance_user)

    assert not Attachment.objects.exists()
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def _pdf_bytes(text):
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode())
        result.extend(obj)
        result.extend(b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    result.extend(b"0000000000 65535 f \n")
    result.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    result.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(result)
