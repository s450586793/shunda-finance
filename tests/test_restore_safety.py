import os
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from scripts.restore_safety import (
    RestoreSafetyError,
    extract_uploads_archive,
    validate_restore_target,
)

PRODUCTION_URL = "postgresql://finance:secret@production-db:5432/shunda_finance"
PROJECT_ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    "restore_url",
    [
        "postgresql://restore:secret@restore-db:5432/safe_restore?dbname=shunda_finance",
        "postgres://restore:secret@production-alias:5432/shunda_finance?application_name=drill",
        "postgresql://restore:secret@production-db.:5432/shunda_finance?connect_timeout=2",
        "service=shunda_restore",
        "host=restore-db dbname=safe_restore servicefile=/tmp/service.conf",
        "hostaddr=127.0.0.1 port=5432 dbname=safe_restore",
    ],
)
def test_restore_target_rejects_production_overrides_and_indirect_configuration(
    restore_url,
):
    with pytest.raises(RestoreSafetyError):
        validate_restore_target(PRODUCTION_URL, restore_url)


def test_restore_target_requires_configured_production_database():
    with pytest.raises(RestoreSafetyError):
        validate_restore_target(None, "postgresql://restore:secret@restore-db:5432/safe_restore")


def test_restore_target_accepts_distinct_named_restore_database():
    validate_restore_target(
        PRODUCTION_URL,
        "postgresql://restore:secret@restore-db:5432/shunda_restore?application_name=drill",
        environment={},
    )


@pytest.mark.parametrize(
    "name",
    [
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGHOSTADDR",
        "PGHOST",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGPASSWORD",
    ],
)
def test_restore_helper_cli_rejects_any_libpq_environment_without_leaking_values(name):
    secret = "/private/service.conf?secret=do-not-disclose"
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": PRODUCTION_URL,
            "RESTORE_DATABASE_URL": "postgresql://restore:secret@restore-db:5432/shunda_restore",
            name: secret,
        }
    )

    result = subprocess.run(
        [sys.executable, "scripts/restore_safety.py", "validate-target"],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 1
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_restore_target_accepts_explicit_empty_environment(monkeypatch):
    monkeypatch.setenv("PGHOST", "production-shadow")

    validate_restore_target(
        PRODUCTION_URL,
        "postgresql://restore:secret@restore-db:5432/shunda_restore",
        environment={},
    )


def test_pg_restore_helper_starts_with_allowlisted_environment(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_pg_restore = fake_bin / "pg_restore"
    fake_pg_restore.write_text(
        "#!/bin/sh\n"
        "for last_argument do :; done\n"
        "env | sort > \"$last_argument.env\"\n"
        "printf '%s\\n' \"$@\" > \"$last_argument.args\"\n",
        encoding="utf-8",
    )
    fake_pg_restore.chmod(0o755)
    dump = tmp_path / "restore.dump"
    dump.write_bytes(b"fake dump")
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "PGHOST": "must-not-reach-pg-restore",
            "PGPASSWORD": "must-not-reach-pg-restore",
            "UNRELATED_SECRET": "must-not-reach-pg-restore",
        }
    )

    result = subprocess.run(
        [
            "sh",
            str(PROJECT_ROOT / "scripts" / "pg_restore_clean.sh"),
            "postgresql://restore:secret@restore-db:5432/shunda_restore",
            str(dump),
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0
    captured_environment = Path(f"{dump}.env").read_text(encoding="utf-8")
    assert "PGHOST=" not in captured_environment
    assert "PGPASSWORD=" not in captured_environment
    assert "UNRELATED_SECRET=" not in captured_environment
    assert "HOME=/tmp" in captured_environment
    assert "LANG=C.UTF-8" in captured_environment
    captured_arguments = Path(f"{dump}.args").read_text(encoding="utf-8")
    assert "--clean" in captured_arguments
    assert "--if-exists" in captured_arguments
    assert "--no-owner" in captured_arguments
    assert "--dbname=postgresql://restore:secret@restore-db:5432/shunda_restore" in (
        captured_arguments
    )


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_extract_uploads_archive_rejects_links(tmp_path, member_type):
    archive_path = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo("uploads/link")
        info.type = member_type
        info.linkname = "uploads/target" if member_type == tarfile.LNKTYPE else "/etc/passwd"
        archive.addfile(info)

    with pytest.raises(RestoreSafetyError):
        extract_uploads_archive(archive_path, tmp_path / "restore")


def test_extract_uploads_archive_extracts_only_regular_files_and_directories(tmp_path):
    archive_path = tmp_path / "uploads.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        directory = tarfile.TarInfo("uploads")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        content = b"safe upload"
        file_info = tarfile.TarInfo("uploads/document.txt")
        file_info.size = len(content)
        archive.addfile(file_info, BytesIO(content))

    destination = tmp_path / "restore"
    extract_uploads_archive(archive_path, destination)

    assert (destination / "uploads" / "document.txt").read_bytes() == b"safe upload"
