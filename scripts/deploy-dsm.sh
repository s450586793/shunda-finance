#!/usr/bin/env sh
set -eu

target_project="shunda-finance"
legacy_project="app"
default_app_dir="/volume4/docker/docker/shunda-finance/app"
default_data_dir="/volume4/docker/docker/shunda-finance/data"
deploy_mode="${SHUNDA_DEPLOY_MODE:-upgrade}"
health_max_attempts="${SHUNDA_HEALTH_MAX_ATTEMPTS:-30}"
health_sleep_seconds="${SHUNDA_HEALTH_SLEEP_SECONDS:-2}"
manual_recovery_message="initial migration requires manual recovery"
legacy_restore_needed=0
target_cleanup_needed=0
target_db_start_attempted=0
target_db_identity_ambiguous=0
legacy_db_id=""
legacy_web_id=""
target_db_id=""

fail() {
  echo "$1" >&2
  exit "${2:-2}"
}

require_value() {
  name="$1"
  eval "value=\${$name-}"
  if [ -z "${value}" ]; then
    fail "$name is required"
  fi
}

require_version() {
  name="$1"
  eval "value=\${$name-}"
  if ! printf '%s' "$value" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$'; then
    fail "$name must use canonical vX.Y.Z format"
  fi
}

require_python3() {
  if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 is required"
  fi
}

require_private_token() {
  name="$1"
  eval "value=\${$name-}"
  if [ -z "$value" ]; then
    fail "$name is required"
  fi
  if ! TOKEN_VALUE="$value" python3 <<'PY'
import os
import sys

value = os.environ["TOKEN_VALUE"]
try:
    valid = bool(value.strip()) and len(value.encode("utf-8")) >= 32
except UnicodeError:
    valid = False
sys.exit(0 if valid else 1)
PY
  then
    fail "$name must be a non-empty token of at least 32 bytes"
  fi
}

compose() {
  project_name="$1"
  shift
  docker compose --project-name "$project_name" --env-file "$env_file" -f "$compose_file" "$@"
}

service_container_id() {
  compose "$1" ps -q "$2" | tr -d '\r'
}

all_service_container_ids() {
  compose "$1" ps --all -q "$2" | tr -d '\r'
}

capture_target_db_id() {
  candidate="$(all_service_container_ids "$target_project" db)"
  case "$candidate" in
    "" | *'
'* | *[!a-zA-Z0-9_.:-]*)
      return 1
      ;;
  esac
  target_db_id="$candidate"
}

wait_healthy() {
  project_name="$1"
  service_name="$2"
  attempt=0
  while [ "$attempt" -lt "$health_max_attempts" ]; do
    container_id="$(service_container_id "$project_name" "$service_name")"
    if [ -n "$container_id" ]; then
      status="$(docker inspect -f '{{.State.Health.Status}}' "$container_id" 2>/dev/null || true)"
      if [ "$status" = "healthy" ]; then
        return 0
      fi
    fi
    attempt=$((attempt + 1))
    sleep "$health_sleep_seconds"
  done
  fail "$service_name failed to become healthy" 1
}

canonical_dir() {
  (
    cd "$1" >/dev/null 2>&1 || exit 1
    pwd -P
  )
}

validate_expected_paths() {
  resolved_app_dir="$(canonical_dir "$app_dir")" || fail "SHUNDA_APP_DIR must be a directory"
  resolved_data_dir="$(canonical_dir "$data_dir")" || fail "SHUNDA_DATA_DIR must be a directory"
  if [ "$resolved_app_dir" != "$default_app_dir" ]; then
    fail "SHUNDA_APP_DIR must match the fixed deployment path"
  fi
  if [ "$resolved_data_dir" != "$default_data_dir" ]; then
    fail "SHUNDA_DATA_DIR must match the fixed deployment path"
  fi
}

validate_postgres_data() {
  postgres_dir="$data_dir/postgres"
  if [ ! -d "$postgres_dir" ]; then
    fail "SHUNDA_DATA_DIR/postgres must exist"
  fi
  if [ ! -f "$postgres_dir/PG_VERSION" ]; then
    fail "SHUNDA_DATA_DIR/postgres/PG_VERSION must exist"
  fi
}

prepare_private_state_directory() {
  state_directory="$data_dir/updater-state"
  if ! STATE_DIRECTORY="$state_directory" python3 2>/dev/null <<'PY'
import os
import stat
import sys
from pathlib import Path

path = Path(os.environ["STATE_DIRECTORY"])
try:
    path.mkdir(mode=0o700)
except FileExistsError:
    pass
except OSError:
    sys.exit(1)

flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
try:
    descriptor = os.open(path, flags)
except OSError:
    sys.exit(1)
try:
    opened_stat = os.fstat(descriptor)
    if not stat.S_ISDIR(opened_stat.st_mode):
        sys.exit(1)
    os.fchmod(descriptor, 0o700)
    secured_stat = os.fstat(descriptor)
    path_stat = path.lstat()
    if (
        not stat.S_ISDIR(secured_stat.st_mode)
        or stat.S_IMODE(secured_stat.st_mode) != 0o700
        or not stat.S_ISDIR(path_stat.st_mode)
        or stat.S_IMODE(path_stat.st_mode) != 0o700
        or path_stat.st_dev != secured_stat.st_dev
        or path_stat.st_ino != secured_stat.st_ino
    ):
        sys.exit(1)
except OSError:
    sys.exit(1)
finally:
    os.close(descriptor)
PY
  then
    fail "updater state directory is unsafe"
  fi
}

validate_rendered_contract() {
  rendered_json="$(mktemp)"
  trap 'rm -f "$rendered_json"' EXIT HUP INT TERM
  compose "$target_project" config --format json >"$rendered_json"
  if ! RENDERED_JSON="$rendered_json" EXPECTED_POSTGRES_DIR="$data_dir/postgres" python3 <<'PY'
import json
import os
import sys
from pathlib import Path


def fail():
    sys.exit(1)


try:
    payload = json.loads(Path(os.environ["RENDERED_JSON"]).read_text(encoding="utf-8"))
    services = payload["services"]
    db = services["db"]
    web = services["web"]
    updater = services["updater"]
except Exception:  # noqa: BLE001
    fail()

db_volumes = db.get("volumes")
if not isinstance(db_volumes, list):
    fail()
matches = [
    volume
    for volume in db_volumes
    if isinstance(volume, dict)
    and volume.get("type") == "bind"
    and volume.get("target") == "/var/lib/postgresql/data"
]
if len(matches) != 1 or matches[0].get("source") != os.environ["EXPECTED_POSTGRES_DIR"]:
    fail()
if web.get("pull_policy") != "never" or updater.get("pull_policy") != "never":
    fail()
web_environment = web.get("environment")
required_web_environment = {
    "DJANGO_SETTINGS_MODULE",
    "DJANGO_SECRET_KEY",
    "DJANGO_DEBUG",
    "DJANGO_ALLOWED_HOSTS",
    "CSRF_TRUSTED_ORIGINS",
    "DJANGO_COOKIE_SECURE",
    "COMPANY_TAX_ID",
    "SHUNDA_UPDATER_URL",
    "SHUNDA_UPDATER_TOKEN",
    "IMPORT_MAX_UPLOAD_BYTES",
    "IMPORT_MAX_ROWS",
    "DATABASE_URL",
}
if not isinstance(web_environment, dict) or set(web_environment) != required_web_environment:
    fail()
if (
    web_environment.get("DJANGO_SETTINGS_MODULE") != "config.settings.prod"
    or web_environment.get("DJANGO_DEBUG") != "false"
    or web_environment.get("SHUNDA_UPDATER_URL") != "http://updater:8090"
):
    fail()
web_socket = 0
updater_socket = 0
for volume in web.get("volumes", []):
    if isinstance(volume, dict) and volume.get("source") == "/var/run/docker.sock":
        web_socket += 1
for volume in updater.get("volumes", []):
    if isinstance(volume, dict) and volume.get("source") == "/var/run/docker.sock":
        updater_socket += 1
if web_socket != 0 or updater_socket != 1:
    fail()
PY
  then
    fail "rendered compose contract is invalid"
  fi
  rm -f "$rendered_json"
  trap - EXIT HUP INT TERM
}

validate_mode() {
  legacy_db_id="$(service_container_id "$legacy_project" db)"
  legacy_web_id="$(service_container_id "$legacy_project" web)"
  existing_target_db_id="$(service_container_id "$target_project" db)"
  existing_target_web_id="$(service_container_id "$target_project" web)"
  existing_target_updater_id="$(service_container_id "$target_project" updater)"
  case "$deploy_mode" in
    initial-migration)
      if [ -z "$legacy_db_id" ] && [ -z "$legacy_web_id" ]; then
        fail "legacy app project must be running for SHUNDA_DEPLOY_MODE=initial-migration"
      fi
      if [ -z "$legacy_db_id" ] || [ -z "$legacy_web_id" ]; then
        fail "legacy app project must have db and web running together for initial-migration"
      fi
      if [ -n "$existing_target_db_id" ] || [ -n "$existing_target_web_id" ] || [ -n "$existing_target_updater_id" ]; then
        fail "shunda-finance project must be stopped before initial-migration"
      fi
      ;;
    upgrade)
      if [ -n "$legacy_db_id" ] || [ -n "$legacy_web_id" ]; then
        fail "SHUNDA_DEPLOY_MODE must be initial-migration while legacy app is still active"
      fi
      ;;
    *)
      fail "SHUNDA_DEPLOY_MODE must be upgrade or initial-migration"
      ;;
  esac
}

restore_legacy_containers() {
  if [ -n "$legacy_db_id" ] && [ -n "$legacy_web_id" ]; then
    docker start "$legacy_db_id" "$legacy_web_id" >/dev/null
  fi
}

stop_target_and_prove_db_stopped() {
  cleanup_safe=1
  if ! compose "$target_project" stop web updater >/dev/null 2>&1; then
    cleanup_safe=0
  fi

  if [ "$target_db_start_attempted" -eq 1 ]; then
    if [ "$target_db_identity_ambiguous" -eq 1 ] || [ -z "$target_db_id" ]; then
      cleanup_safe=0
    elif ! docker stop "$target_db_id" >/dev/null 2>&1; then
      cleanup_safe=0
    else
      target_db_running="$(
        docker inspect -f '{{.State.Running}}' "$target_db_id" 2>/dev/null
      )" || cleanup_safe=0
      if [ "$target_db_running" != "false" ]; then
        cleanup_safe=0
      fi
    fi
  fi

  [ "$cleanup_safe" -eq 1 ]
}

start_target_services() {
  target_db_start_attempted=1
  target_up_status=0
  compose "$target_project" up -d db updater || target_up_status=$?
  if ! capture_target_db_id; then
    target_db_identity_ambiguous=1
  fi
  if [ "$target_up_status" -ne 0 ] || [ "$target_db_identity_ambiguous" -eq 1 ]; then
    return 1
  fi
}

stop_legacy_project() {
  legacy_restore_needed=1
  compose "$legacy_project" stop db web
  if [ -n "$(service_container_id "$legacy_project" db)" ]; then
    fail "legacy app database must be stopped before starting shunda-finance"
  fi
}

on_exit() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$status" -ne 0 ]; then
    set +e
    cleanup_safe=1
    if [ "$target_cleanup_needed" -eq 1 ]; then
      stop_target_and_prove_db_stopped || cleanup_safe=0
    fi
    if [ "$legacy_restore_needed" -eq 1 ]; then
      if [ "$cleanup_safe" -eq 1 ]; then
        if ! restore_legacy_containers >/dev/null 2>&1; then
          printf '%s\n' "$manual_recovery_message" >&2
        fi
      else
        printf '%s\n' "$manual_recovery_message" >&2
      fi
    fi
  fi
  exit "$status"
}

require_python3
require_value SHUNDA_APP_DIR
require_value SHUNDA_DATA_DIR
require_value SHUNDA_WEB_IMAGE_TAG
require_value SHUNDA_UPDATER_IMAGE_TAG
require_private_token SHUNDA_UPDATER_TOKEN
require_version SHUNDA_WEB_IMAGE_TAG
require_version SHUNDA_UPDATER_IMAGE_TAG

app_dir="$SHUNDA_APP_DIR"
data_dir="$SHUNDA_DATA_DIR"
compose_file="$app_dir/compose.yml"
env_file="$app_dir/.env"

if [ ! -f "$compose_file" ]; then
  fail "compose.yml is required under SHUNDA_APP_DIR"
fi
if [ ! -f "$env_file" ]; then
  fail ".env is required under SHUNDA_APP_DIR"
fi
if [ ! -d "$data_dir" ]; then
  fail "SHUNDA_DATA_DIR must exist"
fi

validate_expected_paths
validate_postgres_data
prepare_private_state_directory
validate_rendered_contract
validate_mode
compose "$target_project" pull --policy always web updater

if [ "$deploy_mode" = "initial-migration" ]; then
  trap 'on_exit' EXIT HUP INT TERM
  stop_legacy_project
fi

target_cleanup_needed=1
start_target_services
wait_healthy "$target_project" db
compose "$target_project" run --rm --no-deps web python manage.py migrate
compose "$target_project" up -d --no-deps web
wait_healthy "$target_project" updater
wait_healthy "$target_project" web

trap - EXIT HUP INT TERM
