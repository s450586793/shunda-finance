import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.core.uploads import validate_upload_signature


class ChunkedUpload:
    def __init__(self, name, chunks, *, declared_size=0):
        self.name = name
        self._content = b"".join(chunks)
        self.size = declared_size
        self._position = 0

    def chunks(self):
        while self._position < len(self._content):
            chunk = self._content[self._position : self._position + 4096]
            self._position += len(chunk)
            yield chunk

    def seek(self, position):
        self._position = position


def test_upload_signature_accepts_text_csv_and_resets_pointer():
    upload = SimpleUploadedFile("transactions.csv", "标题,金额\n测试,1\n".encode("gb18030"))
    upload.read(1)

    validate_upload_signature(upload)

    assert upload.tell() == 0


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("invoice.pdf", b"not-a-pdf"),
        ("bank.xlsx", b"not-a-zip"),
        ("bank.xls", b"not-an-ole-file"),
        ("bank.csv", b"name\x00amount"),
    ],
)
def test_upload_signature_rejects_extension_and_content_mismatch(name, content):
    upload = SimpleUploadedFile(name, content)

    with pytest.raises(ValidationError, match="文件扩展名与实际内容不一致"):
        validate_upload_signature(upload)

    assert upload.tell() == 0


def test_upload_signature_checks_controls_beyond_initial_chunk_and_resets_pointer():
    upload = ChunkedUpload(
        "transactions.csv",
        [b"column\nvalue\n", b"safe" * 3000, b"\x01invalid"],
        declared_size=1,
    )

    with pytest.raises(ValidationError, match="文件扩展名与实际内容不一致"):
        validate_upload_signature(upload)

    assert upload._position == 0


def test_upload_signature_enforces_actual_chunked_size_not_declared_size(settings):
    settings.IMPORT_MAX_UPLOAD_BYTES = 4
    upload = ChunkedUpload("transactions.csv", [b"abc", b"de"], declared_size=1)

    with pytest.raises(ValidationError, match="文件大小超过系统允许的上限"):
        validate_upload_signature(upload)

    assert upload._position == 0
