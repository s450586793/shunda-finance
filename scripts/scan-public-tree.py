from __future__ import annotations

import re
import stat
import sys
import zipfile
from pathlib import Path

FORBIDDEN_NAMES = {
    ".env",
    ".git",
    ".superpowers",
    ".venv",
    ".workflow",
    "__pycache__",
    "db.sqlite3",
    "node_modules",
}
FORBIDDEN_SUFFIXES = {".key", ".p12", ".pem", ".pfx", ".sqlite", ".sqlite3"}
PRIVATE_KEY_PATTERN = re.compile(br"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


def _load_anchor_variants(anchor_path: Path) -> list[tuple[bytes, bytes, bytes]]:
    if anchor_path.is_symlink() or not anchor_path.is_file():
        raise ValueError
    if stat.S_IMODE(anchor_path.stat().st_mode) != 0o600:
        raise ValueError
    anchors = [
        line.strip()
        for line in anchor_path.read_bytes().splitlines()
        if line.strip() and not line.lstrip().startswith(b"#")
    ]
    if not anchors:
        raise ValueError
    variants = []
    for anchor in anchors:
        text = anchor.decode("utf-8", errors="strict")
        variants.append((anchor, text.encode("utf-16-le"), text.encode("utf-16-be")))
    return variants


def _scan_bytes(data: bytes, anchors: list[tuple[bytes, bytes, bytes]]) -> None:
    if PRIVATE_KEY_PATTERN.search(data):
        raise ValueError
    if any(candidate in data for variants in anchors for candidate in variants):
        raise ValueError


def _scan_tree(root: Path, anchor_path: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError
    if anchor_path == root or root in anchor_path.parents:
        raise ValueError
    anchors = _load_anchor_variants(anchor_path)
    archive_total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in FORBIDDEN_NAMES for part in relative.parts):
            raise ValueError
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise ValueError
        if path.is_dir():
            continue
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError
        _scan_bytes(str(relative).encode("utf-8"), anchors)
        data = path.read_bytes()
        _scan_bytes(data, anchors)
        if not zipfile.is_zipfile(path):
            continue
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if member.file_size > 64 * 1024 * 1024:
                    raise ValueError
                archive_total += member.file_size
                if archive_total > 256 * 1024 * 1024:
                    raise ValueError
                _scan_bytes(member.filename.encode("utf-8"), anchors)
                _scan_bytes(archive.read(member), anchors)


def _main() -> int:
    if len(sys.argv) != 3:
        return 1
    try:
        root = Path(sys.argv[1]).resolve(strict=True)
        anchor_path = Path(sys.argv[2]).resolve(strict=True)
        _scan_tree(root, anchor_path)
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile):
        return 1
    print("public tree scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
