import re
from pathlib import Path

from django.db import IntegrityError, transaction
from django.utils import timezone
from pypdf import PdfReader

from apps.accounts.roles import require_finance_actor
from apps.core.audit import record_audit
from apps.core.uploads import validate_upload_signature
from apps.imports.storage import sha256_file

from .models import Attachment, AttachmentStatus, Invoice

INVOICE_NUMBER_PATTERN = re.compile(r"(?<!\d)\d{20}(?!\d)")


def extract_invoice_numbers(filename: str, file_obj) -> set[str]:
    try:
        reader = PdfReader(file_obj)
        numbers = set(INVOICE_NUMBER_PATTERN.findall(filename))
        if not numbers:
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            numbers.update(INVOICE_NUMBER_PATTERN.findall(text))
        else:
            len(reader.pages)
        return numbers
    finally:
        file_obj.seek(0)


def attach_invoice_pdf(uploaded_file, actor) -> Attachment:
    require_finance_actor(actor, message="只有财务人员可以上传发票附件")
    validate_upload_signature(uploaded_file)
    sha256 = sha256_file(uploaded_file)
    existing = Attachment.objects.filter(sha256=sha256).first()
    if existing is not None:
        return existing

    original_name = Path(getattr(uploaded_file, "name", "upload.pdf")).name
    invoice_numbers = extract_invoice_numbers(original_name, uploaded_file)
    target = _unique_invoice_target(invoice_numbers)
    attachment = None
    try:
        with transaction.atomic():
            attachment = Attachment(
                file=uploaded_file,
                original_name=original_name,
                sha256=sha256,
                status=(
                    AttachmentStatus.LINKED
                    if target is not None
                    else AttachmentStatus.UNCLAIMED
                ),
                target=target,
                uploaded_by=actor,
            )
            attachment.save()
            record_audit(
                actor,
                "attachment.uploaded",
                attachment,
                {
                    "status": attachment.status,
                    "sha256": attachment.sha256,
                    "target_id": str(target.pk) if target is not None else None,
                },
            )
            return attachment
    except IntegrityError:
        _delete_uncommitted_attachment_file(attachment)
        existing = Attachment.objects.filter(sha256=sha256).first()
        if existing is not None:
            return existing
        raise
    except Exception:
        _delete_uncommitted_attachment_file(attachment)
        raise


@transaction.atomic
def disable_attachment(attachment_id, actor) -> Attachment:
    require_finance_actor(actor, message="只有财务人员可以上传发票附件")
    attachment = Attachment.objects.select_for_update().get(pk=attachment_id)
    if attachment.disabled_at is not None:
        return attachment
    attachment.disabled_at = timezone.now()
    attachment.save(update_fields=["disabled_at"])
    record_audit(
        actor,
        "attachment.disabled",
        attachment,
        {"disabled_at": attachment.disabled_at.isoformat()},
    )
    return attachment


def _unique_invoice_target(invoice_numbers: set[str]):
    if len(invoice_numbers) != 1:
        return None
    matches = list(Invoice.objects.filter(invoice_number=next(iter(invoice_numbers))))
    return matches[0] if len(matches) == 1 else None


def _delete_uncommitted_attachment_file(attachment) -> None:
    if attachment is None:
        return
    field_file = attachment.file
    if field_file._committed and field_file.name:
        field_file.storage.delete(field_file.name)
