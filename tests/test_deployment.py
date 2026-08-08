import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

VALID_PRODUCTION_TAX_ID = "91320281" + "SAFE" + "000001"


def _production_settings_process(company_tax_id=...):
    environment = os.environ.copy()
    environment.update(
        {
            "DJANGO_ALLOWED_HOSTS": "localhost",
            "CSRF_TRUSTED_ORIGINS": "https://localhost",
            "DJANGO_SECRET_KEY": "p" * 50,
            "DJANGO_DEBUG": "false",
            "DATABASE_URL": (
                "postgresql://finance:long-production-password@db:5432/finance"
            ),
            "SHUNDA_RELEASE_VERSION": "v0.1.0",
            "SHUNDA_UPDATER_URL": "http://updater:8090",
            "SHUNDA_UPDATER_TOKEN": "u" * 32,
        }
    )
    if company_tax_id is ...:
        environment.pop("COMPANY_TAX_ID", None)
    else:
        environment["COMPANY_TAX_ID"] = company_tax_id
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from config.settings import prod; "
                "print(prod.COMPANY_TAX_ID)"
            ),
        ],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _compose_fixture_environment() -> dict[str, str]:
    return {
        "DJANGO_SETTINGS_MODULE": "config.settings.prod",
        "DJANGO_SECRET_KEY": "p" * 50,
        "DJANGO_DEBUG": "false",
        "DJANGO_ALLOWED_HOSTS": "localhost",
        "CSRF_TRUSTED_ORIGINS": "https://localhost",
        "DJANGO_COOKIE_SECURE": "true",
        "COMPANY_TAX_ID": VALID_PRODUCTION_TAX_ID,
        "POSTGRES_DB": "shunda_finance",
        "POSTGRES_USER": "finance",
        "POSTGRES_PASSWORD": "long-production-password",
        "SHUNDA_RELEASE_VERSION": "v9.9.9",
        "SHUNDA_WEB_IMAGE_TAG": "v0.2.0",
        "SHUNDA_UPDATER_IMAGE_TAG": "v0.2.1",
        "SHUNDA_UPDATER_TOKEN": "u" * 32,
        "SHUNDA_APP_DIR": "/volume4/docker/docker/shunda-finance/app",
        "SHUNDA_DATA_DIR": "/volume4/docker/docker/shunda-finance/data",
        "IMPORT_MAX_UPLOAD_BYTES": "20971520",
        "IMPORT_MAX_ROWS": "100000",
        "DATABASE_URL": (
            "postgresql://finance:long-production-password@db:5432/shunda_finance"
        ),
    }


def _render_compose():
    environment = _compose_fixture_environment()
    compose_template = Path("compose.yml").read_text()
    return re.sub(
        r"\$\{(?P<name>[A-Z0-9_]+)(?::\?[^}]*)?\}",
        lambda match: environment[match.group("name")],
        compose_template,
    )


def _fake_compose_json(
    data_dir: Path, *, release_version_override: str | None = None
) -> str:
    web_environment = {
        "DJANGO_SETTINGS_MODULE": "config.settings.prod",
        "DJANGO_SECRET_KEY": "p" * 50,
        "DJANGO_DEBUG": "false",
        "DJANGO_ALLOWED_HOSTS": "localhost",
        "CSRF_TRUSTED_ORIGINS": "https://localhost",
        "DJANGO_COOKIE_SECURE": "true",
        "COMPANY_TAX_ID": VALID_PRODUCTION_TAX_ID,
        "SHUNDA_UPDATER_URL": "http://updater:8090",
        "SHUNDA_UPDATER_TOKEN": "u" * 32,
        "IMPORT_MAX_UPLOAD_BYTES": "20971520",
        "IMPORT_MAX_ROWS": "100000",
        "DATABASE_URL": (
            "postgresql://finance:long-production-password@db:5432/shunda_finance"
        ),
    }
    if release_version_override is not None:
        web_environment["SHUNDA_RELEASE_VERSION"] = release_version_override
    return json.dumps(
        {
            "services": {
                "db": {
                    "volumes": [
                        {
                            "type": "bind",
                            "source": f"{data_dir}/postgres",
                            "target": "/var/lib/postgresql/data",
                        }
                    ]
                },
                "web": {
                    "pull_policy": "never",
                    "environment": web_environment,
                    "volumes": [
                        {
                            "type": "bind",
                            "source": f"{data_dir}/uploads",
                            "target": "/data/uploads",
                        }
                    ],
                },
                "updater": {
                    "pull_policy": "never",
                    "volumes": [
                        {
                            "type": "bind",
                            "source": "/var/run/docker.sock",
                            "target": "/var/run/docker.sock",
                        },
                        {
                            "type": "bind",
                            "source": f"{data_dir}/updater-state",
                            "target": "/state",
                        },
                    ],
                },
            }
        },
        separators=(",", ":"),
    )


def _materialize_temp_deploy_script(tmp_path: Path, app_dir: Path, data_dir: Path) -> Path:
    script_path = tmp_path / "deploy-dsm-test.sh"
    payload = Path("scripts/deploy-dsm.sh").read_text(encoding="utf-8")
    payload = payload.replace(
        'default_app_dir="/volume4/docker/docker/shunda-finance/app"',
        f'default_app_dir="{app_dir}"',
    )
    payload = payload.replace(
        'default_data_dir="/volume4/docker/docker/shunda-finance/data"',
        f'default_data_dir="{data_dir}"',
    )
    script_path.write_text(payload, encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


def _write_fake_docker(fake_bin: Path) -> None:
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env python3\n"
        "from __future__ import annotations\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "log_path = Path(os.environ['FAKE_DOCKER_LOG'])\n"
        "log_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "with log_path.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "state_path = Path(os.environ['FAKE_DOCKER_STATE'])\n"
        "state = json.loads(state_path.read_text(encoding='utf-8'))\n"
        "\n"
        "def save() -> None:\n"
        "    state_path.write_text(json.dumps(state, separators=(',', ':')), encoding='utf-8')\n"
        "\n"
        "if sys.argv[1] == 'compose':\n"
        "    args = sys.argv[2:]\n"
        "    project = None\n"
        "    index = 0\n"
        "    while index < len(args):\n"
        "        token = args[index]\n"
        "        if token == '--project-name':\n"
        "            project = args[index + 1]\n"
        "            index += 2\n"
        "            continue\n"
        "        if token in {'--env-file', '-f'}:\n"
        "            index += 2\n"
        "            continue\n"
        "        break\n"
        "    command = args[index]\n"
        "    rest = args[index + 1:]\n"
        "    services = state.setdefault('services', {})\n"
        "    services.setdefault('app', {})\n"
        "    services.setdefault('shunda-finance', {})\n"
        "    if command == 'config':\n"
        "        sys.stdout.write(Path(os.environ['FAKE_COMPOSE_CONFIG']).read_text(encoding='utf-8'))\n"
        "        raise SystemExit(0)\n"
        "    if command == 'ps' and rest[:1] in (['-q'], ['--all']):\n"
        "        if rest[:1] == ['--all']:\n"
        "            if rest[1:2] != ['-q']:\n"
        "                raise SystemExit(2)\n"
        "            service = rest[2]\n"
        "            include_stopped = True\n"
        "        else:\n"
        "            service = rest[1]\n"
        "            include_stopped = False\n"
        "        container = services.get(project, {}).get(service, '')\n"
        "        running = state.setdefault('running', {}).get(container, False)\n"
        "        if container and (include_stopped or running):\n"
        "            sys.stdout.write(container)\n"
        "        raise SystemExit(0)\n"
        "    if command == 'pull':\n"
        "        raise SystemExit(1 if os.environ.get('FAKE_FAIL_PULL') == '1' else 0)\n"
        "    if command == 'stop':\n"
        "        service_names = [service for service in rest if not service.startswith('-')]\n"
        "        if (\n"
        "            os.environ.get('FAKE_FAIL_LEGACY_STOP_AFTER_DB') == '1'\n"
        "            and project == 'app'\n"
        "            and service_names == ['db', 'web']\n"
        "        ):\n"
        "            container = services[project].get('db', '')\n"
        "            if container:\n"
        "                state.setdefault('running', {})[container] = False\n"
        "            save()\n"
        "            raise SystemExit(1)\n"
        "        if (\n"
        "            os.environ.get('FAKE_FAIL_TARGET_SERVICE_STOP_AFTER_WEB') == '1'\n"
        "            and project == 'shunda-finance'\n"
        "            and service_names == ['web', 'updater']\n"
        "        ):\n"
        "            container = services[project].get('web', '')\n"
        "            if container:\n"
        "                state.setdefault('running', {})[container] = False\n"
        "            save()\n"
        "            raise SystemExit(1)\n"
        "        for service in rest:\n"
        "            if service.startswith('-'):\n"
        "                continue\n"
        "            container = services[project].get(service, '')\n"
        "            if container:\n"
        "                state.setdefault('running', {})[container] = False\n"
        "        save()\n"
        "        raise SystemExit(0)\n"
        "    if command == 'up':\n"
        "        service_names = [service for service in rest if not service.startswith('-')]\n"
        "        if (\n"
        "            os.environ.get('FAKE_FAIL_TARGET_UP_AFTER_DB') == '1'\n"
        "            and project == 'shunda-finance'\n"
        "            and service_names == ['db', 'updater']\n"
        "        ):\n"
        "            services[project]['db'] = 'shunda-finance-db'\n"
        "            state.setdefault('running', {})['shunda-finance-db'] = True\n"
        "            save()\n"
        "            raise SystemExit(1)\n"
        "        for service in rest:\n"
        "            if service.startswith('-'):\n"
        "                continue\n"
        "            services[project][service] = (\n"
        "                f'legacy-{service}' if project == 'app' else f'{project}-{service}'\n"
        "            )\n"
        "            state.setdefault('running', {})[services[project][service]] = True\n"
        "        save()\n"
        "        raise SystemExit(0)\n"
        "    if command == 'run':\n"
        "        raise SystemExit(1 if os.environ.get('FAKE_FAIL_MIGRATE') == '1' else 0)\n"
        "if sys.argv[1] == 'start':\n"
        "    for container in sys.argv[2:]:\n"
        "        if container not in state.setdefault('running', {}):\n"
        "            raise SystemExit(1)\n"
        "        state['running'][container] = True\n"
        "    save()\n"
        "    raise SystemExit(0)\n"
        "if sys.argv[1] == 'stop':\n"
        "    if len(sys.argv) != 3:\n"
        "        raise SystemExit(2)\n"
        "    container = sys.argv[2]\n"
        "    if container == 'shunda-finance-db' and os.environ.get('FAKE_FAIL_TARGET_DB_STOP') == '1':\n"
        "        raise SystemExit(1)\n"
        "    if container not in state.setdefault('running', {}):\n"
        "        raise SystemExit(1)\n"
        "    if not (\n"
        "        container == 'shunda-finance-db'\n"
        "        and os.environ.get('FAKE_TARGET_DB_STOP_STAYS_RUNNING') == '1'\n"
        "    ):\n"
        "        state['running'][container] = False\n"
        "    if container == 'shunda-finance-db' and os.environ.get('FAKE_STALE_TARGET_DB_ID') == '1':\n"
        "        del state['running'][container]\n"
        "    save()\n"
        "    raise SystemExit(0)\n"
        "if sys.argv[1] == 'inspect':\n"
        "    container = sys.argv[-1]\n"
        "    if container not in state.setdefault('running', {}):\n"
        "        raise SystemExit(1)\n"
        "    if (\n"
        "        '{{.State.Running}}' in sys.argv\n"
        "        and container == 'shunda-finance-db'\n"
        "        and os.environ.get('FAKE_FAIL_TARGET_DB_INSPECT') == '1'\n"
        "    ):\n"
        "        raise SystemExit(1)\n"
        "    if '{{.State.Running}}' in sys.argv:\n"
        "        sys.stdout.write('true' if state['running'][container] else 'false')\n"
        "        raise SystemExit(0)\n"
        "    key = 'FAKE_HEALTH_' + container.upper().replace('-', '_')\n"
        "    sys.stdout.write(os.environ.get(key, 'healthy'))\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    (fake_bin / "docker").chmod(0o755)


def _write_python_commands(fake_bin: Path, *, python_fails: bool) -> None:
    (fake_bin / "python").write_text(
        "#!/bin/sh\n"
        + ("exit 127\n" if python_fails else f"exec {sys.executable} \"$@\"\n"),
        encoding="utf-8",
    )
    (fake_bin / "python").chmod(0o755)
    (fake_bin / "python3").write_text(
        f"#!/bin/sh\nexec {sys.executable} \"$@\"\n",
        encoding="utf-8",
    )
    (fake_bin / "python3").chmod(0o755)


def _prepare_deploy_fixture(
    tmp_path: Path,
    *,
    legacy_db: bool = False,
    legacy_web: bool = False,
    create_pg_version: bool = True,
    python_fails: bool = False,
) -> tuple[dict[str, str], Path, Path, Path]:
    app_dir = tmp_path / "app"
    data_dir = tmp_path / "data"
    fake_bin = tmp_path / "bin"
    state_path = tmp_path / "docker-state.json"
    command_log = tmp_path / "docker.log"
    compose_config = tmp_path / "compose-config.json"

    app_dir.mkdir()
    data_dir.mkdir()
    fake_bin.mkdir()
    postgres_dir = data_dir / "postgres"
    postgres_dir.mkdir()
    if create_pg_version:
        (postgres_dir / "PG_VERSION").write_text("16\n", encoding="utf-8")
    (app_dir / "compose.yml").write_text(Path("compose.yml").read_text(), encoding="utf-8")
    (app_dir / ".env").write_text("PLACEHOLDER=1\n", encoding="utf-8")
    script_path = _materialize_temp_deploy_script(tmp_path, app_dir, data_dir)
    compose_config.write_text(_fake_compose_json(data_dir), encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {
                "services": {
                    "app": {
                        "db": "legacy-db" if legacy_db else "",
                        "web": "legacy-web" if legacy_web else "",
                    },
                    "shunda-finance": {"db": "", "web": "", "updater": ""},
                },
                "running": {
                    **({"legacy-db": True} if legacy_db else {}),
                    **({"legacy-web": True} if legacy_web else {}),
                },
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    _write_fake_docker(fake_bin)
    _write_python_commands(fake_bin, python_fails=python_fails)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "FAKE_DOCKER_LOG": str(command_log),
            "FAKE_DOCKER_STATE": str(state_path),
            "FAKE_COMPOSE_CONFIG": str(compose_config),
            "SHUNDA_APP_DIR": str(app_dir),
            "SHUNDA_DATA_DIR": str(data_dir),
            "SHUNDA_WEB_IMAGE_TAG": "v0.2.0",
            "SHUNDA_UPDATER_IMAGE_TAG": "v0.2.1",
            "SHUNDA_UPDATER_TOKEN": "u" * 32,
            "SHUNDA_HEALTH_MAX_ATTEMPTS": "1",
            "SHUNDA_HEALTH_SLEEP_SECONDS": "0",
            "FAKE_DEPLOY_SCRIPT": str(script_path),
        }
    )
    return environment, app_dir, data_dir, command_log


def _run_deploy_script(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sh",
            environment.get(
                "FAKE_DEPLOY_SCRIPT", str(Path.cwd() / "scripts" / "deploy-dsm.sh")
            ),
        ],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_production_paths_match_compose_mounts_and_backup_scripts(monkeypatch):
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "localhost")
    monkeypatch.setenv("CSRF_TRUSTED_ORIGINS", "https://localhost")
    monkeypatch.setenv("DJANGO_SECRET_KEY", "p" * 50)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://finance:long-production-password@db:5432/finance"
    )
    monkeypatch.setenv("COMPANY_TAX_ID", VALID_PRODUCTION_TAX_ID)
    monkeypatch.setenv("SHUNDA_RELEASE_VERSION", "v0.1.0")
    monkeypatch.setenv("SHUNDA_UPDATER_URL", "http://updater:8090")
    monkeypatch.setenv("SHUNDA_UPDATER_TOKEN", "u" * 32)

    from config.settings import prod

    compose = Path("compose.yml").read_text()
    backup = Path("scripts/backup.sh").read_text()
    restore = Path("scripts/restore.sh").read_text()
    pg_restore_clean = Path("scripts/pg_restore_clean.sh").read_text()
    dockerfile = Path("Dockerfile").read_text()

    assert prod.MEDIA_ROOT == Path("/data/uploads")
    assert prod.EXPORT_ROOT == Path("/data/exports")
    assert prod.BACKUP_ROOT == Path("/data/backups")
    assert "${SHUNDA_DATA_DIR:?required}/uploads:/data/uploads" in compose
    assert "${SHUNDA_DATA_DIR:?required}/exports:/data/exports" in compose
    assert "${SHUNDA_DATA_DIR:?required}/backups:/data/backups" in compose
    assert "${SHUNDA_DATA_DIR:?required}/postgres:/var/lib/postgresql/data" in compose
    assert 'uploads_dir="${UPLOADS_DIR:-/data/uploads}"' in backup
    assert 'backup_dir="${BACKUP_DIR:-/data/backups}"' in backup
    assert "127.0.0.1:8000:8000" in compose
    assert "postgresql-client" in dockerfile
    assert "collectstatic --noinput" in dockerfile
    assert 'canonical_backup_dir="$(realpath -e "$backup_dir")"' in restore
    assert 'restore_dir="$(mktemp -d "$backup_dir/.uploads-restore.XXXXXX")"' in restore
    assert 'tar -C "$data_dir" -czf "$preserved_archive.tmp" -- uploads' in restore
    assert 'find "$uploads_dir" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +' in restore
    assert 'mv "$uploads_dir"' not in restore
    assert "restore_safety.py validate-target" in restore
    assert "restore_safety.py extract-uploads" in restore
    assert "pg_restore_clean.sh" in restore
    assert "env -i" in pg_restore_clean


def test_docker_collectstatic_uses_a_valid_build_only_company_tax_id():
    dockerfile = Path("Dockerfile").read_text()
    collectstatic_instruction = next(
        line for line in dockerfile.splitlines() if "COMPANY_TAX_ID=" in line
    )
    match = re.search(
        r"COMPANY_TAX_ID=(?P<company_tax_id>[0-9A-Z]{15,20})",
        collectstatic_instruction,
    )

    assert match is not None
    result = _production_settings_process(match.group("company_tax_id"))
    assert result.returncode == 0


def test_docker_production_image_excludes_development_dependencies():
    dockerfile = Path("Dockerfile").read_text()

    assert 'pip install --no-cache-dir "."' in dockerfile
    assert 'pip install --no-cache-dir ".[dev]"' not in dockerfile


def test_postgres_client_major_matches_database_image():
    compose = Path("compose.yml").read_text()
    dockerfile = Path("Dockerfile").read_text()
    database_image = re.search(r"image: postgres:(?P<major>\d+)-", compose)

    assert database_image is not None
    assert f"postgresql-client-{database_image.group('major')}" in dockerfile


def test_production_static_assets_collect_without_missing_references(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "localhost")
    monkeypatch.setenv("CSRF_TRUSTED_ORIGINS", "https://localhost")
    monkeypatch.setenv("DJANGO_SECRET_KEY", "p" * 50)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://finance:long-production-password@db:5432/finance",
    )
    monkeypatch.setenv("COMPANY_TAX_ID", VALID_PRODUCTION_TAX_ID)
    monkeypatch.setenv("SHUNDA_RELEASE_VERSION", "v0.1.0")
    monkeypatch.setenv("SHUNDA_UPDATER_URL", "http://updater:8090")
    monkeypatch.setenv("SHUNDA_UPDATER_TOKEN", "u" * 32)

    from django.core.management import call_command
    from django.test import override_settings

    from config.settings import prod

    with override_settings(STATIC_ROOT=tmp_path, STORAGES=prod.STORAGES):
        call_command("collectstatic", interactive=False, verbosity=0)

    assert (tmp_path / "staticfiles.json").is_file()


def test_production_settings_require_company_tax_id_without_leaking_a_value():
    result = _production_settings_process()

    assert result.returncode != 0
    assert "COMPANY_TAX_ID" in result.stderr


@pytest.mark.parametrize(
    "company_tax_id",
    [
        "REPLACE_COMPANY_TAX_ID",
        "91320281TEST000001",
        "123-INVALID-TAX-ID",
        "12345678901234",
        "111111111111111111",
    ],
)
def test_production_settings_reject_placeholder_or_invalid_company_tax_id(
    company_tax_id,
):
    result = _production_settings_process(company_tax_id)
    combined_output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "COMPANY_TAX_ID" in result.stderr
    assert company_tax_id not in combined_output


def test_production_settings_normalize_valid_company_tax_id():
    result = _production_settings_process(" 91320281ma1abcd123 ")

    assert result.returncode == 0
    assert result.stdout.strip() == "91320281MA1ABCD123"


@pytest.mark.parametrize(
    ("pg_dump_content", "fail_archive_move", "use_relative_directories"),
    [("dump", False, False), ("dump", False, True), ("", False, False), ("dump", True, False)],
)
def test_backup_script_emits_manifest_only_after_nonempty_final_backups(
    tmp_path, pg_dump_content, fail_archive_move, use_relative_directories
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    backup_dir = tmp_path / "backups"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    (uploads_dir / "invoice.pdf").write_bytes(b"uploaded document")

    (fake_bin / "pg_dump").write_text(
        "#!/bin/sh\n"
        "for argument do\n"
        "  case \"$argument\" in --file=*) output=${argument#--file=} ;; esac\n"
        "done\n"
        "printf '%s' \"${PG_DUMP_CONTENT-dump}\" > \"$output\"\n",
        encoding="utf-8",
    )
    (fake_bin / "tar").write_text(
        "#!/bin/sh\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = \"-czf\" ]; then output=$2; break; fi\n"
        "  shift\n"
        "done\n"
        "printf archive > \"$output\"\n",
        encoding="utf-8",
    )
    (fake_bin / "mv").write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *.tar.gz.tmp) [ \"${FAIL_ARCHIVE_MOVE:-0}\" = 1 ] && exit 1 ;;\n"
        "esac\n"
        "/bin/mv \"$@\"\n",
        encoding="utf-8",
    )
    (fake_bin / "find").write_text(
        "#!/bin/sh\n"
        "exit 0\n",
        encoding="utf-8",
    )
    for command in fake_bin.iterdir():
        command.chmod(0o755)

    environment = os.environ.copy()
    working_directory = tmp_path if use_relative_directories else Path.cwd()
    backup_directory_value = (
        str(backup_dir.relative_to(tmp_path)) if use_relative_directories else str(backup_dir)
    )
    uploads_directory_value = (
        str(uploads_dir.relative_to(tmp_path)) if use_relative_directories else str(uploads_dir)
    )
    environment.update(
        {
            "BACKUP_DIR": backup_directory_value,
            "UPLOADS_DIR": uploads_directory_value,
            "DATABASE_URL": "postgresql://backup:secret@db:5432/shunda_finance",
            "FAIL_ARCHIVE_MOVE": "1" if fail_archive_move else "0",
            "PG_DUMP_CONTENT": pg_dump_content,
            "PATH": f"{fake_bin}:{environment['PATH']}",
        }
    )
    result = subprocess.run(
        ["sh", str(Path.cwd() / "scripts" / "backup.sh")],
        cwd=working_directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    if fail_archive_move or not pg_dump_content:
        assert result.returncode != 0
        assert result.stdout == ""
        if fail_archive_move:
            assert list(backup_dir.glob("uploads-*.tar.gz")) == []
        return

    dump_path = next(backup_dir.glob("db-*.dump"))
    archive_path = next(backup_dir.glob("uploads-*.tar.gz"))
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        f"DB_BACKUP={dump_path}",
        f"UPLOADS_BACKUP={archive_path}",
    ]
    assert dump_path.stat().st_size > 0
    assert archive_path.stat().st_size > 0


def test_production_compose_uses_release_images_and_internal_service_boundaries():
    compose = _render_compose()
    config = yaml.safe_load(compose)
    web = config["services"]["web"]

    assert "services:\n  db:\n" in compose
    assert "\n  web:\n" in compose
    assert "\n  updater:\n" in compose
    assert "image: ghcr.io/s450586793/shunda-finance-web:v0.2.0" in compose
    assert "image: ghcr.io/s450586793/shunda-finance-updater:v0.2.1" in compose
    assert "build:" not in compose
    assert "127.0.0.1:8000:8000" in compose
    assert "SHUNDA_UPDATER_URL: http://updater:8090" in compose
    assert "env_file" not in web
    assert web["environment"] == {
        "DJANGO_SETTINGS_MODULE": "config.settings.prod",
        "DJANGO_SECRET_KEY": "p" * 50,
        "DJANGO_DEBUG": "false",
        "DJANGO_ALLOWED_HOSTS": "localhost",
        "CSRF_TRUSTED_ORIGINS": "https://localhost",
        "DJANGO_COOKIE_SECURE": "true",
        "COMPANY_TAX_ID": VALID_PRODUCTION_TAX_ID,
        "SHUNDA_UPDATER_URL": "http://updater:8090",
        "SHUNDA_UPDATER_TOKEN": "u" * 32,
        "IMPORT_MAX_UPLOAD_BYTES": "20971520",
        "IMPORT_MAX_ROWS": "100000",
        "DATABASE_URL": (
            "postgresql://finance:long-production-password@db:5432/shunda_finance"
        ),
    }
    assert config["services"]["updater"]["environment"]["SHUNDA_UPDATER_TOKEN"] == (
        "u" * 32
    )
    assert compose.count("/var/run/docker.sock:/var/run/docker.sock") == 1
    assert "/volume4/docker/docker/shunda-finance/app:/config" in compose
    assert "/volume4/docker/docker/shunda-finance/data/updater-state:/state" in compose
    assert "condition: service_healthy" in compose
    assert "healthcheck:" in compose
    assert "networks:\n      - internal" in compose
    assert "internal: true" in compose


def test_deployment_contract_files_exclude_sensitive_local_state_and_pin_images():
    env_example = Path(".env.example").read_text()
    dockerignore = Path(".dockerignore").read_text()
    gitignore = Path(".gitignore").read_text()
    dockerfile = Path("Dockerfile").read_text()
    deploy_script = Path("scripts/deploy-dsm.sh").read_text()

    for name in (
        "SHUNDA_APP_DIR",
        "SHUNDA_DATA_DIR",
        "SHUNDA_WEB_IMAGE_TAG",
        "SHUNDA_UPDATER_IMAGE_TAG",
        "SHUNDA_UPDATER_TOKEN",
    ):
        assert f"{name}=" in env_example
    assert "SHUNDA_RELEASE_VERSION=" not in env_example
    assert {line for line in gitignore.splitlines() if line} >= {
        ".venv/",
        ".workflow/",
        "db.sqlite3",
        ".superpowers/",
    }
    assert {line for line in dockerignore.splitlines() if line} == {
        "**",
        "!Dockerfile",
        "!pyproject.toml",
        "!manage.py",
        "!apps/",
        "!apps/**",
        "!config/",
        "!config/**",
        "!scripts/",
        "!scripts/backup.sh",
        "!scripts/pg_restore_clean.sh",
        "!scripts/restore.sh",
        "!scripts/restore_safety.py",
        "!scripts/wait_for_db.py",
        "!templates/",
        "!templates/**",
        "!static/",
        "!static/**",
        "static/js/*.test.js",
        "!updater/",
        "!updater/**",
    }
    assert "FROM python:3.12.11-slim-bookworm AS web" in dockerfile
    assert "FROM docker:27.5.1-cli AS updater" in dockerfile
    assert "org.opencontainers.image.version=$SHUNDA_RELEASE_VERSION" in dockerfile
    assert 'ENTRYPOINT ["python3", "-m", "updater.main"]' in dockerfile
    assert 'compose "$target_project" pull --policy always web updater' in deploy_script
    assert 'compose "$target_project" up -d db updater' in deploy_script
    assert 'compose "$target_project" run --rm --no-deps web python manage.py migrate' in deploy_script
    assert 'compose "$target_project" up -d --no-deps web' in deploy_script
    assert 'compose "$target_project" config --format json' in deploy_script
    assert "python3" in deploy_script
    assert "PG_VERSION" in deploy_script
    assert "SHUNDA_DEPLOY_MODE" in deploy_script
    assert "SHUNDA_EXPECTED_APP_DIR" not in deploy_script
    assert "SHUNDA_EXPECTED_DATA_DIR" not in deploy_script
    assert "docker image prune" not in deploy_script
    assert "--force" not in deploy_script


def test_deploy_script_is_owner_executable():
    assert Path("scripts/deploy-dsm.sh").stat().st_mode & 0o100


def test_web_upgrade_override_targets_web_service_only():
    platform_code = Path("updater/platform.py").read_text()

    assert '"services:\\n"' in platform_code
    assert '"  web:\\n"' in platform_code
    assert '"    pull_policy: never\\n"' in platform_code
    assert "db:" not in platform_code.split("def _write_override", maxsplit=1)[1].split(
        "def _rollback_alias", maxsplit=1
    )[0]
    assert "updater:" not in platform_code.split(
        "def _write_override", maxsplit=1
    )[1].split("def _rollback_alias", maxsplit=1)[0]


def test_deploy_script_rejects_short_private_token_without_leaking_it(tmp_path):
    environment, _app_dir, _data_dir, _command_log = _prepare_deploy_fixture(tmp_path)
    environment["SHUNDA_UPDATER_TOKEN"] = "secret-short-token"
    result = _run_deploy_script(environment)

    assert result.returncode != 0
    assert "SHUNDA_UPDATER_TOKEN" in result.stderr
    assert "secret-short-token" not in result.stdout + result.stderr


def test_deploy_script_initial_migration_pulls_first_and_stops_legacy_before_starting_target(
    tmp_path,
):
    environment, app_dir, _data_dir, command_log = _prepare_deploy_fixture(
        tmp_path, legacy_db=True, legacy_web=True
    )
    environment["SHUNDA_DEPLOY_MODE"] = "initial-migration"
    result = _run_deploy_script(environment)

    assert result.returncode == 0
    assert "u" * 32 not in result.stdout + result.stderr
    assert "prune" not in command_log.read_text()
    assert "--force" not in command_log.read_text()
    assert command_log.read_text().splitlines() == [
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} config --format json",
        f"compose --project-name app --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} ps -q db",
        f"compose --project-name app --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} ps -q web",
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} ps -q db",
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} ps -q web",
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} ps -q updater",
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} pull --policy always web updater",
        f"compose --project-name app --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} stop db web",
        f"compose --project-name app --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} ps -q db",
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} up -d db updater",
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} ps --all -q db",
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} ps -q db",
        "inspect -f {{.State.Health.Status}} shunda-finance-db",
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} run --rm --no-deps web python manage.py migrate",
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} up -d --no-deps web",
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} ps -q updater",
        "inspect -f {{.State.Health.Status}} shunda-finance-updater",
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} ps -q web",
        "inspect -f {{.State.Health.Status}} shunda-finance-web",
    ]


def test_deploy_script_rejects_data_dir_without_pg_version(tmp_path):
    environment, _app_dir, data_dir, _command_log = _prepare_deploy_fixture(
        tmp_path, create_pg_version=False
    )
    result = _run_deploy_script(environment)

    assert result.returncode != 0
    assert "PG_VERSION" in result.stderr
    assert str(data_dir) not in result.stderr


def test_deploy_script_creates_private_real_updater_state_directory(tmp_path):
    environment, _app_dir, data_dir, _command_log = _prepare_deploy_fixture(tmp_path)

    result = _run_deploy_script(environment)

    state_directory = data_dir / "updater-state"
    directory_stat = state_directory.lstat()
    assert result.returncode == 0
    assert stat.S_ISDIR(directory_stat.st_mode)
    assert not state_directory.is_symlink()
    assert stat.S_IMODE(directory_stat.st_mode) == 0o700


def test_deploy_script_tightens_existing_updater_state_directory_permissions(tmp_path):
    environment, _app_dir, data_dir, _command_log = _prepare_deploy_fixture(tmp_path)
    state_directory = data_dir / "updater-state"
    state_directory.mkdir(mode=0o755)
    state_directory.chmod(0o755)

    result = _run_deploy_script(environment)

    assert result.returncode == 0
    assert stat.S_IMODE(state_directory.lstat().st_mode) == 0o700


@pytest.mark.parametrize(
    "unsafe_kind",
    ["directory_symlink", "file_symlink", "regular_file"],
)
def test_deploy_script_rejects_unsafe_updater_state_path_without_leaking_it(
    tmp_path, unsafe_kind
):
    environment, _app_dir, data_dir, command_log = _prepare_deploy_fixture(tmp_path)
    state_path = data_dir / "updater-state"
    outside_path = tmp_path / "outside-private-state"
    if unsafe_kind == "directory_symlink":
        outside_path.mkdir(mode=0o755)
        outside_path.chmod(0o755)
        state_path.symlink_to(outside_path, target_is_directory=True)
    elif unsafe_kind == "file_symlink":
        outside_path.write_text("private state", encoding="utf-8")
        state_path.symlink_to(outside_path)
    else:
        state_path.write_text("private state", encoding="utf-8")

    result = _run_deploy_script(environment)

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "updater state directory is unsafe\n"
    assert str(state_path) not in result.stderr
    assert str(outside_path) not in result.stderr
    assert not command_log.exists()
    if unsafe_kind == "directory_symlink":
        assert stat.S_IMODE(outside_path.stat().st_mode) == 0o755


def test_deploy_script_rejects_non_expected_fixed_paths(tmp_path):
    environment, _app_dir, _data_dir, _command_log = _prepare_deploy_fixture(tmp_path)
    environment["SHUNDA_EXPECTED_DATA_DIR"] = str(tmp_path / "other-data")
    result = _run_deploy_script(environment)

    assert result.returncode == 0


def test_deploy_script_requires_explicit_initial_migration_when_legacy_project_is_active(
    tmp_path,
):
    environment, _app_dir, _data_dir, command_log = _prepare_deploy_fixture(
        tmp_path, legacy_db=True, legacy_web=True
    )
    result = _run_deploy_script(environment)

    assert result.returncode != 0
    assert "SHUNDA_DEPLOY_MODE" in result.stderr
    assert all(" stop " not in line for line in command_log.read_text().splitlines())


def test_deploy_script_rejects_partial_legacy_state_during_initial_migration(tmp_path):
    environment, _app_dir, _data_dir, command_log = _prepare_deploy_fixture(
        tmp_path, legacy_db=True, legacy_web=False
    )
    environment["SHUNDA_DEPLOY_MODE"] = "initial-migration"
    result = _run_deploy_script(environment)

    assert result.returncode != 0
    assert "legacy" in result.stderr
    assert all(" stop " not in line for line in command_log.read_text().splitlines())


def test_deploy_script_pull_failure_keeps_legacy_project_running(tmp_path):
    environment, _app_dir, _data_dir, command_log = _prepare_deploy_fixture(
        tmp_path, legacy_db=True, legacy_web=True
    )
    environment["SHUNDA_DEPLOY_MODE"] = "initial-migration"
    environment["FAKE_FAIL_PULL"] = "1"
    result = _run_deploy_script(environment)

    assert result.returncode != 0
    assert command_log.read_text().splitlines() == [
        f"compose --project-name shunda-finance --env-file {Path(environment['SHUNDA_APP_DIR']) / '.env'} -f {Path(environment['SHUNDA_APP_DIR']) / 'compose.yml'} config --format json",
        f"compose --project-name app --env-file {Path(environment['SHUNDA_APP_DIR']) / '.env'} -f {Path(environment['SHUNDA_APP_DIR']) / 'compose.yml'} ps -q db",
        f"compose --project-name app --env-file {Path(environment['SHUNDA_APP_DIR']) / '.env'} -f {Path(environment['SHUNDA_APP_DIR']) / 'compose.yml'} ps -q web",
        f"compose --project-name shunda-finance --env-file {Path(environment['SHUNDA_APP_DIR']) / '.env'} -f {Path(environment['SHUNDA_APP_DIR']) / 'compose.yml'} ps -q db",
        f"compose --project-name shunda-finance --env-file {Path(environment['SHUNDA_APP_DIR']) / '.env'} -f {Path(environment['SHUNDA_APP_DIR']) / 'compose.yml'} ps -q web",
        f"compose --project-name shunda-finance --env-file {Path(environment['SHUNDA_APP_DIR']) / '.env'} -f {Path(environment['SHUNDA_APP_DIR']) / 'compose.yml'} ps -q updater",
        f"compose --project-name shunda-finance --env-file {Path(environment['SHUNDA_APP_DIR']) / '.env'} -f {Path(environment['SHUNDA_APP_DIR']) / 'compose.yml'} pull --policy always web updater",
    ]


def test_deploy_script_migrate_failure_proves_exact_target_db_stopped_before_legacy_restore(
    tmp_path,
):
    environment, app_dir, _data_dir, command_log = _prepare_deploy_fixture(
        tmp_path, legacy_db=True, legacy_web=True
    )
    environment["SHUNDA_DEPLOY_MODE"] = "initial-migration"
    environment["FAKE_FAIL_MIGRATE"] = "1"
    result = _run_deploy_script(environment)

    assert result.returncode != 0
    assert command_log.read_text().splitlines()[-4:] == [
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} stop web updater",
        "stop shunda-finance-db",
        "inspect -f {{.State.Running}} shunda-finance-db",
        "start legacy-db legacy-web",
    ]


def test_deploy_script_web_health_failure_stops_target_and_restores_legacy_project(
    tmp_path,
):
    environment, app_dir, _data_dir, command_log = _prepare_deploy_fixture(
        tmp_path, legacy_db=True, legacy_web=True
    )
    environment["SHUNDA_DEPLOY_MODE"] = "initial-migration"
    environment["FAKE_HEALTH_SHUNDA_FINANCE_WEB"] = "unhealthy"
    result = _run_deploy_script(environment)

    assert result.returncode != 0
    assert command_log.read_text().splitlines()[-4:] == [
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} stop web updater",
        "stop shunda-finance-db",
        "inspect -f {{.State.Running}} shunda-finance-db",
        "start legacy-db legacy-web",
    ]


def test_deploy_script_uses_python3_when_python_command_is_unavailable(tmp_path):
    environment, _app_dir, _data_dir, _command_log = _prepare_deploy_fixture(
        tmp_path, python_fails=True
    )
    result = _run_deploy_script(environment)

    assert result.returncode == 0


def test_deploy_script_legacy_stop_partial_failure_restores_by_exact_container_ids(
    tmp_path,
):
    environment, app_dir, _data_dir, command_log = _prepare_deploy_fixture(
        tmp_path, legacy_db=True, legacy_web=True
    )
    environment["SHUNDA_DEPLOY_MODE"] = "initial-migration"
    environment["FAKE_FAIL_LEGACY_STOP_AFTER_DB"] = "1"

    result = _run_deploy_script(environment)

    assert result.returncode != 0
    assert command_log.read_text().splitlines()[-1] == "start legacy-db legacy-web"
    assert (
        f"compose --project-name app --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} up -d db web"
        not in command_log.read_text()
    )


def test_deploy_script_target_up_partial_failure_captures_and_stops_exact_db_before_restore(
    tmp_path,
):
    environment, app_dir, _data_dir, command_log = _prepare_deploy_fixture(
        tmp_path, legacy_db=True, legacy_web=True
    )
    environment["SHUNDA_DEPLOY_MODE"] = "initial-migration"
    environment["FAKE_FAIL_TARGET_UP_AFTER_DB"] = "1"

    result = _run_deploy_script(environment)

    assert result.returncode != 0
    assert command_log.read_text().splitlines()[-5:] == [
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} ps --all -q db",
        f"compose --project-name shunda-finance --env-file {app_dir / '.env'} -f {app_dir / 'compose.yml'} stop web updater",
        "stop shunda-finance-db",
        "inspect -f {{.State.Running}} shunda-finance-db",
        "start legacy-db legacy-web",
    ]


@pytest.mark.parametrize(
    "failure_flag",
    [
        "FAKE_FAIL_TARGET_DB_STOP",
        "FAKE_FAIL_TARGET_DB_INSPECT",
        "FAKE_STALE_TARGET_DB_ID",
        "FAKE_TARGET_DB_STOP_STAYS_RUNNING",
        "FAKE_FAIL_TARGET_SERVICE_STOP_AFTER_WEB",
    ],
)
def test_deploy_script_target_stop_ambiguity_keeps_legacy_db_stopped(
    tmp_path, failure_flag
):
    environment, _app_dir, _data_dir, command_log = _prepare_deploy_fixture(
        tmp_path, legacy_db=True, legacy_web=True
    )
    environment.update(
        {
            "SHUNDA_DEPLOY_MODE": "initial-migration",
            "FAKE_FAIL_MIGRATE": "1",
            failure_flag: "1",
        }
    )

    result = _run_deploy_script(environment)

    state = json.loads(Path(environment["FAKE_DOCKER_STATE"]).read_text(encoding="utf-8"))
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "initial migration requires manual recovery\n"
    assert "start legacy-db legacy-web" not in command_log.read_text().splitlines()
    assert state["running"]["legacy-db"] is False


def test_deploy_script_rejects_rendered_runtime_release_version_override(tmp_path):
    environment, _app_dir, data_dir, command_log = _prepare_deploy_fixture(tmp_path)
    Path(environment["FAKE_COMPOSE_CONFIG"]).write_text(
        _fake_compose_json(data_dir, release_version_override="v9.9.9"),
        encoding="utf-8",
    )

    result = _run_deploy_script(environment)

    assert result.returncode != 0
    assert result.stderr == "rendered compose contract is invalid\n"
    assert command_log.read_text().splitlines()[-1].endswith("config --format json")
