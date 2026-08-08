#!/usr/bin/env sh
set -eu

project_root="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)"
cd "$project_root"

export POSTGRES_DB="shunda_finance"
export POSTGRES_USER="finance"
export POSTGRES_PASSWORD="long-production-password"
export DJANGO_SETTINGS_MODULE="config.settings.prod"
export DJANGO_SECRET_KEY="pppppppppppppppppppppppppppppppppppppppppppppppppp"
export DJANGO_DEBUG="false"
export DJANGO_ALLOWED_HOSTS="localhost"
export CSRF_TRUSTED_ORIGINS="https://localhost"
export DJANGO_COOKIE_SECURE="true"
export COMPANY_TAX_ID="91320281SAFE000001"
export SHUNDA_RELEASE_VERSION="v9.9.9"
export SHUNDA_WEB_IMAGE_TAG="v0.2.0"
export SHUNDA_UPDATER_IMAGE_TAG="v0.2.1"
export SHUNDA_UPDATER_TOKEN="uuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuu"
export SHUNDA_APP_DIR="/volume4/docker/docker/shunda-finance/app"
export SHUNDA_DATA_DIR="/volume4/docker/docker/shunda-finance/data"
export IMPORT_MAX_UPLOAD_BYTES="20971520"
export IMPORT_MAX_ROWS="100000"
export DATABASE_URL="postgresql://finance:long-production-password@db:5432/shunda_finance"

if [ "${POSTGRES_DB}" != "shunda_finance" ] || [ "${SHUNDA_WEB_IMAGE_TAG}" != "v0.2.0" ] || [ "${DATABASE_URL}" != "postgresql://finance:long-production-password@db:5432/shunda_finance" ]; then
  echo "compose fixture must not consume ambient environment values" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  if [ "${SHUNDA_REQUIRE_DOCKER_COMPOSE:-0}" = "1" ]; then
    echo "docker CLI with Compose v2 is required for this contract" >&2
    exit 1
  fi
  echo "SKIP: docker CLI with Compose v2 is unavailable"
  exit 0
fi

python3 <<'PY'
import os
import json
import stat
import subprocess
import tempfile
from pathlib import Path

deploy_script = Path("scripts/deploy-dsm.sh").read_text(encoding="utf-8")
deploy_mode = Path("scripts/deploy-dsm.sh").stat().st_mode

fixture_values = {
    "POSTGRES_DB": "shunda_finance",
    "POSTGRES_USER": "finance",
    "POSTGRES_PASSWORD": "long-production-password",
    "DJANGO_SETTINGS_MODULE": "config.settings.prod",
    "DJANGO_SECRET_KEY": "pppppppppppppppppppppppppppppppppppppppppppppppppp",
    "DJANGO_DEBUG": "false",
    "DJANGO_ALLOWED_HOSTS": "localhost",
    "CSRF_TRUSTED_ORIGINS": "https://localhost",
    "DJANGO_COOKIE_SECURE": "true",
    "COMPANY_TAX_ID": "91320281SAFE000001",
    "SHUNDA_UPDATER_TOKEN": "uuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuu",
    "SHUNDA_APP_DIR": "/volume4/docker/docker/shunda-finance/app",
    "SHUNDA_DATA_DIR": "/volume4/docker/docker/shunda-finance/data",
    "IMPORT_MAX_UPLOAD_BYTES": "20971520",
    "IMPORT_MAX_ROWS": "100000",
    "DATABASE_URL": "postgresql://finance:long-production-password@db:5432/shunda_finance",
    "SHUNDA_WEB_IMAGE_TAG": "v0.2.0",
    "SHUNDA_UPDATER_IMAGE_TAG": "v0.2.1",
}
compose_environment = os.environ.copy()
compose_environment.update({name: f"ambient-poison-{name}" for name in fixture_values})
compose_environment.update(fixture_values)
with tempfile.TemporaryDirectory(prefix="compose-contract-") as tempdir:
    env_file = Path(tempdir) / "compose.env"
    env_file.write_text("".join(f"{name}={value}\n" for name, value in fixture_values.items()), encoding="utf-8")
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--project-name",
                "shunda-finance",
                "--env-file",
                str(env_file),
                "-f",
                "compose.yml",
                "config",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=compose_environment,
        )
    except FileNotFoundError as error:
        raise SystemExit("docker CLI with Compose v2 is required for this contract") from error
if result.returncode != 0:
    raise SystemExit(f"docker compose config failed: {result.stderr.strip()}")
try:
    config = json.loads(result.stdout)
except json.JSONDecodeError as error:
    raise SystemExit("docker compose config must emit JSON") from error

services = config.get("services")
if not isinstance(services, dict) or set(services) != {"db", "web", "updater"}:
    raise SystemExit("compose must define exactly db, web, and updater services")
web = services["web"]
updater = services["updater"]

if web.get("image") != "ghcr.io/s450586793/shunda-finance-web:v0.2.0":
    raise SystemExit("web must use the requested immutable image tag")
if updater.get("image") != "ghcr.io/s450586793/shunda-finance-updater:v0.2.1":
    raise SystemExit("updater must use the requested immutable image tag")
if web.get("pull_policy") != "never" or updater.get("pull_policy") != "never":
    raise SystemExit("web and updater must use pull_policy never")
if web.get("ports") != [{"mode": "ingress", "target": 8000, "published": "8000", "protocol": "tcp", "host_ip": "127.0.0.1"}]:
    raise SystemExit("web must expose only loopback 127.0.0.1:8000:8000")
if "ports" in updater:
    raise SystemExit("updater must not publish ports")

def bind_targets(service: dict) -> set[tuple[str, str]]:
    volumes = service.get("volumes", [])
    if not isinstance(volumes, list):
        raise SystemExit("service volumes must be a list")
    return {
        (volume.get("source"), volume.get("target"))
        for volume in volumes
        if isinstance(volume, dict) and volume.get("type") == "bind"
    }


if bind_targets(web) != {
    ("/volume4/docker/docker/shunda-finance/data/uploads", "/data/uploads"),
    ("/volume4/docker/docker/shunda-finance/data/exports", "/data/exports"),
    ("/volume4/docker/docker/shunda-finance/data/backups", "/data/backups"),
}:
    raise SystemExit("web bind mounts must match the app data boundaries")
if bind_targets(updater) != {
    ("/var/run/docker.sock", "/var/run/docker.sock"),
    ("/volume4/docker/docker/shunda-finance/app", "/config"),
    ("/volume4/docker/docker/shunda-finance/data/updater-state", "/state"),
}:
    raise SystemExit("updater bind mounts must match the updater boundary")
all_binds = [source for service in services.values() for source, _ in bind_targets(service)]
if all_binds.count("/var/run/docker.sock") != 1:
    raise SystemExit("Docker socket must appear exactly once and only on updater")

for name, service in services.items():
    if set(service.get("networks", {})) != {"internal"}:
        raise SystemExit(f"{name} must connect only to internal network")
networks = config.get("networks")
if not isinstance(networks, dict) or set(networks) != {"internal"} or networks["internal"].get("internal") is not True:
    raise SystemExit("top-level internal network must be internal: true")

if "env_file" in web or "SHUNDA_RELEASE_VERSION" in web.get("environment", {}):
    raise SystemExit("web must not receive deployment-only release environment")
if web["environment"].get("DJANGO_SETTINGS_MODULE") != "config.settings.prod":
    raise SystemExit("web must use production Django settings")
if web["environment"].get("DATABASE_URL", "").endswith("/shunda_finance") is not True:
    raise SystemExit("web database must target shunda_finance")
if web["environment"].get("SHUNDA_UPDATER_TOKEN") != "u" * 32 or updater["environment"].get("SHUNDA_UPDATER_TOKEN") != "u" * 32:
    raise SystemExit("web and updater must receive the fixture updater token")
assert 'compose "$target_project" pull --policy always web updater' in deploy_script
assert 'compose "$target_project" config --format json' in deploy_script
assert "SHUNDA_DEPLOY_MODE" in deploy_script
assert "PG_VERSION" in deploy_script
assert "python3" in deploy_script
assert "SHUNDA_EXPECTED_APP_DIR" not in deploy_script
assert "SHUNDA_EXPECTED_DATA_DIR" not in deploy_script
assert "SHUNDA_RELEASE_VERSION=" not in Path(".env.example").read_text(encoding="utf-8")
assert deploy_mode & stat.S_IXUSR
PY
