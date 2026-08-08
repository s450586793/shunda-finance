import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
RUNNER = PROJECT_ROOT / "tests" / "e2e" / "run_server.py"
RESULTS_ROOT = PROJECT_ROOT / "test-results"
EXPECTED_DATABASE = RESULTS_ROOT / "e2e.sqlite3"
EXPECTED_MEDIA = RESULTS_ROOT / "e2e-media"
EXPECTED_STATIC = RESULTS_ROOT / "e2e-static"


def _validate_paths(project_root):
    from config.e2e_paths import validate_e2e_paths

    return validate_e2e_paths(project_root)


def _sentinel_digest(path):
    return hashlib.sha256(path.read_bytes()).digest()


def _polluted_environment(tmp_path):
    production_database = tmp_path / "db.sqlite3"
    with sqlite3.connect(production_database) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('do-not-change')")
    production_media = tmp_path / "media"
    production_media.mkdir()
    (production_media / "sentinel.txt").write_text("do-not-change", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.dev",
            "DATABASE_URL": f"sqlite:///{production_database}",
            "COMPANY_TAX_ID": "PRODUCTION-TAX-ID",
            "E2E_DATABASE_PATH": str(production_database),
            "E2E_MEDIA_ROOT": str(production_media),
            "E2E_STATIC_ROOT": str(tmp_path / "static"),
        }
    )
    return environment, production_database, production_media


def test_path_validator_accepts_real_repository_directory(tmp_path):
    project_root = tmp_path / "repository"
    results_root = project_root / "test-results"
    results_root.mkdir(parents=True)
    (results_root / "e2e.sqlite3").write_bytes(b"database")
    (results_root / "e2e-media").mkdir()
    (results_root / "e2e-static").mkdir()

    paths = _validate_paths(project_root)

    assert paths.results_root == results_root
    assert paths.database == results_root / "e2e.sqlite3"
    assert paths.media == results_root / "e2e-media"
    assert paths.static == results_root / "e2e-static"


def test_path_validator_rejects_results_root_symlink_without_touching_target(tmp_path):
    project_root = tmp_path / "repository"
    project_root.mkdir()
    external_root = tmp_path / "external-results"
    external_root.mkdir()
    sentinel = external_root / "sentinel.txt"
    sentinel.write_bytes(b"do-not-change")
    digest_before = _sentinel_digest(sentinel)
    (project_root / "test-results").symlink_to(
        external_root, target_is_directory=True
    )

    with pytest.raises(RuntimeError, match="E2E path validation failed") as error:
        _validate_paths(project_root)

    assert str(external_root) not in str(error.value)
    assert _sentinel_digest(sentinel) == digest_before


@pytest.mark.parametrize(
    ("child_name", "target_is_directory"),
    [
        ("e2e.sqlite3", False),
        ("e2e-media", True),
        ("e2e-static", True),
    ],
)
def test_path_validator_rejects_child_symlink_without_touching_target(
    tmp_path, child_name, target_is_directory
):
    project_root = tmp_path / "repository"
    results_root = project_root / "test-results"
    results_root.mkdir(parents=True)
    external_target = tmp_path / f"external-{child_name}"
    if target_is_directory:
        external_target.mkdir()
        sentinel = external_target / "sentinel.txt"
    else:
        sentinel = external_target
    sentinel.write_bytes(b"do-not-change")
    digest_before = _sentinel_digest(sentinel)
    (results_root / child_name).symlink_to(
        external_target, target_is_directory=target_is_directory
    )

    with pytest.raises(RuntimeError, match="E2E path validation failed") as error:
        _validate_paths(project_root)

    assert str(external_target) not in str(error.value)
    assert _sentinel_digest(sentinel) == digest_before


def test_runner_forces_e2e_environment_before_django_import(tmp_path):
    environment, _production_database, _production_media = _polluted_environment(
        tmp_path
    )
    probe = (
        "import os, runpy; "
        f"runpy.run_path({str(RUNNER)!r}, run_name='e2e_probe'); "
        "print(os.environ['DJANGO_SETTINGS_MODULE']); "
        "print(os.environ['COMPANY_TAX_ID']); "
        "print(os.environ['E2E_DATABASE_PATH']); "
        "print(os.environ['E2E_MEDIA_ROOT']); "
        "print(os.environ['E2E_STATIC_ROOT'])"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "config.settings.e2e",
        "91320281TEST000001",
        str(EXPECTED_DATABASE),
        str(EXPECTED_MEDIA),
        str(EXPECTED_STATIC),
    ]


def test_e2e_settings_ignore_external_storage_paths(tmp_path):
    environment, _production_database, _production_media = _polluted_environment(
        tmp_path
    )
    environment["DJANGO_SETTINGS_MODULE"] = "config.settings.e2e"
    probe = (
        "from django.conf import settings; "
        "print(settings.DATABASES['default']['ENGINE']); "
        "print(settings.DATABASES['default']['NAME']); "
        "print(settings.MEDIA_ROOT); "
        "print(settings.STATIC_ROOT)"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "django.db.backends.sqlite3",
        str(EXPECTED_DATABASE),
        str(EXPECTED_MEDIA),
        str(EXPECTED_STATIC),
    ]


def test_setup_only_never_mutates_prod_like_database_or_media(tmp_path):
    environment, production_database, production_media = _polluted_environment(tmp_path)
    database_before = hashlib.sha256(production_database.read_bytes()).digest()
    media_before = (production_media / "sentinel.txt").read_bytes()

    result = subprocess.run(
        [sys.executable, str(RUNNER), "--setup-only"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0
    assert hashlib.sha256(production_database.read_bytes()).digest() == database_before
    assert (production_media / "sentinel.txt").read_bytes() == media_before
    with sqlite3.connect(EXPECTED_DATABASE) as connection:
        usernames = {
            row[0]
            for row in connection.execute(
                "SELECT username FROM auth_user ORDER BY username"
            )
        }
    assert usernames == {"finance-e2e", "owner-e2e"}
