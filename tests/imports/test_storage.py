from io import BytesIO

import pytest

from apps.imports.storage import sha256_file, source_upload_path


def test_sha256_file_is_stable_and_rewinds_stream():
    stream = BytesIO(b"same-content")

    first = sha256_file(stream)
    second = sha256_file(stream)

    assert first == "cae1b3faaa5e4ac7c3306bd164b36dcfdff98294b8024c9c949639b4c480bf6b"
    assert second == first
    assert stream.tell() == 0


def test_sha256_file_rewinds_stream_after_hashing_from_middle():
    stream = BytesIO(b"same-content")
    stream.seek(4)

    assert sha256_file(stream) == "cae1b3faaa5e4ac7c3306bd164b36dcfdff98294b8024c9c949639b4c480bf6b"
    assert stream.tell() == 0


def test_sha256_file_rejects_non_seekable_stream():
    class NonSeekableStream:
        def read(self, size=-1):
            return b""

    with pytest.raises(ValueError, match="文件流必须支持定位"):
        sha256_file(NonSeekableStream())


def test_source_upload_path_keeps_batch_files_together():
    instance = type("SourceFileStub", (), {"batch_id": "batch-uuid"})()

    assert source_upload_path(instance, "statement.csv") == "imports/batch-uuid/statement.csv"


def test_source_upload_path_uses_filename_basename():
    instance = type("SourceFileStub", (), {"batch_id": "batch-uuid"})()

    assert source_upload_path(instance, "../unsafe/statement.csv") == "imports/batch-uuid/statement.csv"
