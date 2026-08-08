import codecs
import csv
import io
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError

ALLOWED_UPLOAD_SIGNATURES = {
    ".pdf": (b"%PDF-",),
    ".xlsx": (b"PK\x03\x04",),
    ".xls": (bytes.fromhex("D0CF11E0A1B11AE1"),),
}
TEXT_EXTENSIONS = {".csv", ".txt"}


def validate_upload_signature(upload) -> None:
    """Validate allowed uploaded file types without consuming their content."""
    suffix = Path(getattr(upload, "name", "")).suffix.lower()
    try:
        upload.seek(0)
        content = _read_limited_content(upload)
        if suffix in TEXT_EXTENSIONS:
            _validate_text_upload(content)
            return
        signatures = ALLOWED_UPLOAD_SIGNATURES.get(suffix)
        if not signatures or not any(content.startswith(signature) for signature in signatures):
            raise ValidationError("文件扩展名与实际内容不一致")
    finally:
        upload.seek(0)


def _read_limited_content(upload) -> bytes:
    chunks = []
    size = 0
    for chunk in _upload_chunks(upload):
        if isinstance(chunk, str):
            chunk = chunk.encode()
        size += len(chunk)
        if size > settings.IMPORT_MAX_UPLOAD_BYTES:
            raise ValidationError("文件大小超过系统允许的上限")
        chunks.append(chunk)
    return b"".join(chunks)


def _upload_chunks(upload):
    chunks_method = getattr(upload, "chunks", None)
    if callable(chunks_method):
        yield from chunks_method()
        return
    while chunk := upload.read(64 * 1024):
        yield chunk


def _validate_text_upload(content: bytes) -> None:
    if any(byte < 32 and byte not in {9, 10, 13} for byte in content):
        raise ValidationError("文件扩展名与实际内容不一致")
    try:
        text = _decode_text(content, "utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = _decode_text(content, "gb18030")
        except UnicodeDecodeError as exc:
            raise ValidationError("文件扩展名与实际内容不一致") from exc
    try:
        next(csv.reader(io.StringIO(text)), None)
    except csv.Error as exc:
        raise ValidationError("文件扩展名与实际内容不一致") from exc


def _decode_text(content: bytes, encoding: str) -> str:
    decoder = codecs.getincrementaldecoder(encoding)()
    return decoder.decode(content, final=True)
