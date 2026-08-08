import hashlib
import os


def sha256_file(file_obj):
    try:
        file_obj.seek(0)
    except (AttributeError, OSError) as exc:
        raise ValueError("文件流必须支持定位") from exc

    digest = hashlib.sha256()
    while chunk := file_obj.read(64 * 1024):
        digest.update(chunk)
    file_obj.seek(0)
    return digest.hexdigest()


def source_upload_path(instance, filename):
    return f"imports/{instance.batch_id}/{os.path.basename(filename)}"
