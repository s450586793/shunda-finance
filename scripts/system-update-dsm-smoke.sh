#!/usr/bin/env bash
set +x
set -euo pipefail

project_root="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)"
readonly project_root
readonly web_repository="ghcr.io/s450586793/shunda-finance-web"
readonly cleanup_pending_message="cleanup pending: follow the root-only cleanup runbook without force."

fail() {
  printf '%s\n' "$1" >&2
  exit "${2:-1}"
}

require_value() {
  local name="$1"
  local value="${!name:-}"
  [ -n "$value" ] || fail "error: $name is required" 2
}

require_version() {
  local name="$1"
  local value="${!name:-}"
  [[ "$value" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    fail "error: $name must use canonical vX.Y.Z format" 2
  }
}

scratch_dir=""
cookie_jar=""
cleanup() {
  trap - EXIT
  [ -z "$scratch_dir" ] || rm -rf -- "$scratch_dir" >/dev/null 2>&1 || true
  [ -z "$cookie_jar" ] || rm -f -- "$cookie_jar" >/dev/null 2>&1 || true
}
exit_for_signal() {
  exit "$1"
}
trap cleanup EXIT
trap 'exit_for_signal 129' HUP
trap 'exit_for_signal 130' INT
trap 'exit_for_signal 143' TERM
if ! scratch_dir="$(mktemp -d 2>/dev/null)"; then
  fail "error: private file operation failed"
fi
if ! cookie_jar="$(mktemp 2>/dev/null)"; then
  fail "error: private file operation failed"
fi
if ! chmod 700 "$scratch_dir" 2>/dev/null || ! chmod 600 "$cookie_jar" 2>/dev/null; then
  fail "error: private file operation failed"
fi

run_capture() {
  local label="$1"
  shift
  local output
  if ! output="$("$@" 2>/dev/null)"; then
    fail "error: $label failed"
  fi
  printf '%s' "$output"
}

json_get() {
  local payload="$1"
  local expression="$2"
  local value
  if ! value="$(printf '%s' "$payload" | jq -er "$expression" 2>/dev/null)"; then
    fail "error: invalid response payload"
  fi
  printf '%s' "$value"
}

html_csrf() {
  python3 - "$1" <<'PY'
import re
import sys
from pathlib import Path

match = re.search(
    r'name="csrfmiddlewaretoken"\s+value="([^"]+)"',
    Path(sys.argv[1]).read_text(encoding="utf-8"),
)
if match is None:
    raise SystemExit(1)
print(match.group(1))
PY
}

cookie_value() {
  python3 - "$1" "$2" <<'PY'
import sys
from pathlib import Path

cookie_path = Path(sys.argv[1])
name = sys.argv[2]
for line in cookie_path.read_text(encoding="utf-8").splitlines():
    if not line or line.startswith("#"):
        continue
    columns = line.split("\t")
    if len(columns) >= 7 and columns[5] == name:
        print(columns[6])
        raise SystemExit(0)
raise SystemExit(1)
PY
}

container_fingerprint() {
  local payload="$1"
  local fingerprint
  if ! fingerprint="$(
    printf '%s' "$payload" | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
if isinstance(payload, list):
    payload = payload[0]
mounts = []
for mount in payload.get("Mounts", []):
    mounts.append(
        {
            "destination": mount.get("Destination", ""),
            "identity": mount.get("Name") or mount.get("Source", ""),
        }
    )
mounts.sort(key=lambda item: item["destination"])
print(
    json.dumps(
        {
            "id": payload.get("Id", ""),
            "image_id": payload.get("Image", ""),
            "config_image": payload.get("Config", {}).get("Image", ""),
            "started_at": payload.get("State", {}).get("StartedAt", ""),
            "project": payload.get("Config", {})
            .get("Labels", {})
            .get("com.docker.compose.project", ""),
            "service": payload.get("Config", {})
            .get("Labels", {})
            .get("com.docker.compose.service", ""),
            "version": payload.get("Config", {})
            .get("Labels", {})
            .get("org.opencontainers.image.version", ""),
            "mounts": mounts,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
' 2>/dev/null
  )"; then
    fail "error: invalid container inspect payload"
  fi
  printf '%s' "$fingerprint"
}

current_seconds() {
  local value
  value="$(date +%s 2>/dev/null || true)"
  [[ "$value" =~ ^[0-9]+$ ]] || fail "error: invalid current time"
  printf '%s' "$value"
}

write_private_file() {
  local path="$1"
  shift
  if ! {
    umask 077
    : >"$path" && chmod 600 "$path" && "$@" >"$path"
  } 2>/dev/null; then
    fail "error: private file operation failed"
  fi
}

write_private_text() {
  local path="$1"
  local content="$2"
  if ! {
    umask 077
    printf '%s' "$content" >"$path" && chmod 600 "$path"
  } 2>/dev/null; then
    fail "error: private file operation failed"
  fi
}

create_form_body() {
  local path="$1"
  local username="$2"
  local password="$3"
  local csrf_token="$4"
  local next_path="$5"
  local values_file
  values_file="$path.values"
  local values_content
  printf -v values_content '%s\n%s\n%s\n%s\n' "$username" "$password" "$csrf_token" "$next_path"
  write_private_text "$values_file" "$values_content"
  umask 077
  if ! {
    python3 - "$values_file" <<'PY' >"$path" && chmod 600 "$path"
from pathlib import Path
import sys
from urllib.parse import urlencode

values = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
if len(values) != 4:
    raise SystemExit(1)
sys.stdout.write(
    urlencode(
        {
            "username": values[0],
            "password": values[1],
            "csrfmiddlewaretoken": values[2],
            "next": values[3],
        }
    )
)
PY
  } 2>/dev/null; then
    fail "error: login request body creation failed"
  fi
}

create_json_body() {
  local path="$1"
  local raw_json="$2"
  write_private_text "$path" "$raw_json"
}

create_curl_config() {
  local path="$1"
  local method="$2"
  local url="$3"
  local body_file="${4:-}"
  local csrf_token="${5:-}"
  local referer="${6:-}"
  local content_type="${7:-}"
  local include_cookie="${8:-yes}"
  if ! {
    umask 077
    {
    printf 'fail\n'
    printf 'silent\n'
    printf 'show-error\n'
    printf 'max-time = 10\n'
    printf 'url = "%s"\n' "$url"
    if [ "$method" = "POST" ]; then
      printf 'request = "POST"\n'
    fi
    if [ "$include_cookie" = "yes" ]; then
      printf 'cookie = "%s"\n' "$cookie_jar"
      printf 'cookie-jar = "%s"\n' "$cookie_jar"
    else
      printf 'cookie-jar = "%s"\n' "$cookie_jar"
    fi
    if [ -n "$body_file" ]; then
      printf 'data-binary = "@%s"\n' "$body_file"
    fi
    if [ -n "$csrf_token" ]; then
      printf 'header = "X-CSRFToken: %s"\n' "$csrf_token"
    fi
    if [ -n "$referer" ]; then
      printf 'header = "Referer: %s"\n' "$referer"
    fi
    if [ -n "$content_type" ]; then
      printf 'header = "Content-Type: %s"\n' "$content_type"
    fi
    } >"$path" && chmod 600 "$path"
  } 2>/dev/null; then
    fail "error: private file operation failed"
  fi
}

run_curl_config() {
  local label="$1"
  local method="$2"
  local url="$3"
  local body_file="${4:-}"
  local csrf_token="${5:-}"
  local referer="${6:-}"
  local content_type="${7:-}"
  local include_cookie="${8:-yes}"
  local config_file
  config_file="$scratch_dir/${label// /_}.curl.cfg"
  create_curl_config "$config_file" "$method" "$url" "$body_file" "$csrf_token" "$referer" "$content_type" "$include_cookie"
  run_capture "$label" curl --config "$config_file"
}

compose() {
  docker compose \
    --project-name "$compose_project" \
    --env-file "$env_file" \
    -f "$compose_file" \
    "$@"
}

service_container_id() {
  local service="$1"
  local output line
  local -a container_ids=()
  output="$(run_capture "compose ps $service" compose ps -q "$service")"
  while IFS= read -r line; do
    line="${line%$'\r'}"
    [ -n "$line" ] && container_ids+=("$line")
  done <<<"$output"
  [ "${#container_ids[@]}" -gt 0 ] || fail "error: missing container identity"
  [ "${#container_ids[@]}" -eq 1 ] || fail "error: compose service returned multiple containers"
  printf '%s' "${container_ids[0]}"
}

inspect_container() {
  local container_id="$1"
  [ -n "$container_id" ] || fail "error: missing container identity"
  run_capture "docker inspect" docker inspect "$container_id"
}

capture_image_inventory() {
  local path="$1"
  local inventory
  inventory="$(run_capture "docker image inventory" docker image ls --digests --no-trunc --format '{{json .}}')"
  write_private_text "$path" "$inventory"
}

validate_initial_web_identity() {
  local identity_file="$1"
  local container_id_file="$2"
  local inventory_file="$3"
  local task_id_file="$4"
  if ! python3 - "$identity_file" "$container_id_file" "$inventory_file" "$task_id_file" \
    "$web_repository" "$check_current_version" "$compose_project" >/dev/null 2>/dev/null <<'PY'
import json
import re
import sys
from pathlib import Path

identity_path, container_id_path, inventory_path, task_id_path = map(Path, sys.argv[1:5])
repository, version, compose_project = sys.argv[5:8]
identity = json.loads(identity_path.read_text(encoding="utf-8"))
container_id = container_id_path.read_text(encoding="utf-8")
task_id = task_id_path.read_text(encoding="utf-8").strip()
sha256_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")

if identity.get("id") != container_id:
    raise SystemExit(1)
if identity.get("project") != compose_project or identity.get("service") != "web":
    raise SystemExit(1)
if identity.get("version") != version:
    raise SystemExit(1)
image_id = identity.get("image_id")
if not isinstance(image_id, str) or sha256_pattern.fullmatch(image_id) is None:
    raise SystemExit(1)
config_image = identity.get("config_image")
if not isinstance(config_image, str):
    raise SystemExit(1)

rows = []
for line in inventory_path.read_text(encoding="utf-8").splitlines():
    row = json.loads(line)
    if not all(isinstance(row.get(key), str) for key in ("Repository", "Tag", "ID", "Digest")):
        raise SystemExit(1)
    rows.append(row)
old_reference = f"{repository}:{version}"
old_rows = [row for row in rows if f'{row["Repository"]}:{row["Tag"]}' == old_reference]
if len(old_rows) != 1 or old_rows[0]["ID"] != image_id:
    raise SystemExit(1)
digest = old_rows[0]["Digest"]
if sha256_pattern.fullmatch(digest) is None:
    raise SystemExit(1)
if config_image not in (old_reference, f"{repository}@{digest}"):
    raise SystemExit(1)
rollback_reference = f"shunda-finance-rollback-web:{task_id}"
if any(f'{row["Repository"]}:{row["Tag"]}' == rollback_reference for row in rows):
    raise SystemExit(1)
PY
  then
    fail "error: initial Web identity proof failed"
  fi
}

verify_cleanup_inventory() {
  local cleanup_status="$1"
  local identity_file="$2"
  local before_file="$3"
  local after_file="$4"
  local state_file_path="$5"
  local task_id_file="$6"
  if ! python3 - "$cleanup_status" "$identity_file" "$before_file" "$after_file" \
    "$state_file_path" "$task_id_file" "$web_repository" "$check_current_version" "$expected_target" \
    >/dev/null 2>/dev/null <<'PY'
from collections import Counter, defaultdict
import json
import re
import sys
from pathlib import Path

cleanup_status = sys.argv[1]
identity_path, before_path, after_path, state_path, task_id_path = map(Path, sys.argv[2:7])
repository, version, expected_target = sys.argv[7:10]
identity = json.loads(identity_path.read_text(encoding="utf-8"))
state = json.loads(state_path.read_text(encoding="utf-8"))
task_id = task_id_path.read_text(encoding="utf-8").strip()
sha256_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")

def load_inventory(path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if not all(isinstance(row.get(key), str) for key in ("Repository", "Tag", "ID", "Digest")):
            raise SystemExit(1)
        if sha256_pattern.fullmatch(row["ID"]) is None:
            raise SystemExit(1)
        rows.append(row)
    return rows

def reference(row):
    return f'{row["Repository"]}:{row["Tag"]}'

def grouped(rows):
    result = defaultdict(list)
    for row in rows:
        result[reference(row)].append((row["ID"], row["Digest"]))
    return {key: sorted(value) for key, value in result.items()}

before = load_inventory(before_path)
after = load_inventory(after_path)
before_by_ref = grouped(before)
after_by_ref = grouped(after)
old_image_id = identity.get("image_id")
config_image = identity.get("config_image", "")
if not isinstance(old_image_id, str) or sha256_pattern.fullmatch(old_image_id) is None:
    raise SystemExit(1)
old_reference = f"{repository}:{version}"
rollback_reference = f"shunda-finance-rollback-web:{task_id}"
old_rows = before_by_ref.get(old_reference)
if old_rows is None or len(old_rows) != 1:
    raise SystemExit(1)
old_image_id_from_inventory, old_digest = old_rows[0]
if old_image_id_from_inventory != old_image_id or sha256_pattern.fullmatch(old_digest) is None:
    raise SystemExit(1)
if config_image not in (old_reference, f"{repository}@{old_digest}"):
    raise SystemExit(1)

original = state.get("task", {}).get("original", {})
original_tags = original.get("tags")
if original.get("repository") != repository or original.get("version") != version:
    raise SystemExit(1)
if original.get("digest") != old_digest or original.get("image_id") != old_image_id:
    raise SystemExit(1)
if (
    not isinstance(original_tags, list)
    or not all(isinstance(tag, str) for tag in original_tags)
    or original_tags.count(old_reference) != 1
):
    raise SystemExit(1)
if original.get("rollback_alias") != rollback_reference:
    raise SystemExit(1)
if before_by_ref.get(old_reference) != [(old_image_id, old_digest)]:
    raise SystemExit(1)
if rollback_reference in before_by_ref:
    raise SystemExit(1)

target = state.get("task", {}).get("target", {})
target_reference = f"{repository}:{expected_target}"
target_tags = target.get("tags")
if target.get("repository") != repository or target.get("version") != expected_target:
    raise SystemExit(1)
if sha256_pattern.fullmatch(target.get("digest", "")) is None:
    raise SystemExit(1)
if sha256_pattern.fullmatch(target.get("image_id", "")) is None:
    raise SystemExit(1)
if (
    not isinstance(target_tags, list)
    or not all(isinstance(tag, str) for tag in target_tags)
    or target_tags.count(target_reference) != 1
    or target.get("rollback_alias") != ""
):
    raise SystemExit(1)

excluded_refs = {old_reference, rollback_reference, target_reference}
for ref, rows in before_by_ref.items():
    if ref in excluded_refs:
        continue
    after_rows = after_by_ref.get(ref, [])
    if ref == "<none>:<none>":
        if Counter(rows) - Counter(after_rows):
            raise SystemExit(1)
    elif after_rows != rows:
        raise SystemExit(1)
target_related_ids = {
    row["ID"] for row in before if reference(row) == target_reference
}
excluded_ids = {old_image_id, target["image_id"], *target_related_ids}
unrelated_ids = {row["ID"] for row in before if row["ID"] not in excluded_ids}
after_ids = {row["ID"] for row in after}
if not unrelated_ids.issubset(after_ids):
    raise SystemExit(1)

if cleanup_status == "complete":
    if old_reference in after_by_ref or rollback_reference in after_by_ref:
        raise SystemExit(1)
    if old_image_id in after_ids:
        raise SystemExit(1)
elif cleanup_status == "pending":
    if after_by_ref.get(old_reference) != [(old_image_id, old_digest)]:
        raise SystemExit(1)
    alias_rows = after_by_ref.get(rollback_reference)
    if alias_rows is None or len(alias_rows) != 1 or alias_rows[0][0] != old_image_id:
        raise SystemExit(1)
    if old_image_id not in after_ids:
        raise SystemExit(1)
else:
    raise SystemExit(1)
PY
  then
    fail "error: cleanup inventory proof failed"
  fi
}

fetch_status_payload() {
  run_curl_config "system update status" GET "$status_url"
}

assert_health_ok() {
  local payload
  payload="$(run_curl_config "health request" GET "$health_url" "" "" "" "" no)"
  [ "$(json_get "$payload" '.status')" = "ok" ] || fail "error: public health is not ok"
}

assert_state_file_mode() {
  local mode
  mode="$(run_capture "updater state mode" compose exec -T updater stat -c %a "$state_file" | tr -d '\r\n')"
  [ "$mode" = "600" ] || fail "error: updater state file mode is not 0600"
}

read_private_state() {
  assert_state_file_mode
  run_capture "updater state" compose exec -T updater cat "$state_file"
}

assert_backup_paths() {
  local state_json="$1"
  local task_id="$2"
  local state_task_id database_backup uploads_backup cleanup_status state_target
  state_task_id="$(json_get "$state_json" '.task.id')"
  [ "$state_task_id" = "$task_id" ] || fail "error: updater state task does not match started task"
  state_target="$(json_get "$state_json" '.task.target.version')"
  [ "$state_target" = "$expected_target" ] || fail "error: updater state target does not match expected target"
  database_backup="$(json_get "$state_json" '.task.database_backup')"
  uploads_backup="$(json_get "$state_json" '.task.uploads_backup')"
  [[ "$database_backup" =~ ^/data/backups/db-([0-9]{8}-[0-9]{6})\.dump$ ]] || {
    fail "error: updater state database backup is invalid"
  }
  local timestamp="${BASH_REMATCH[1]}"
  [[ "$uploads_backup" =~ ^/data/backups/uploads-([0-9]{8}-[0-9]{6})\.tar\.gz$ ]] || {
    fail "error: updater state uploads backup is invalid"
  }
  [ "$timestamp" = "${BASH_REMATCH[1]}" ] || fail "error: backup timestamps do not match"
  run_capture "database backup presence" compose exec -T web test -s "$database_backup" >/dev/null
  run_capture "uploads backup presence" compose exec -T web test -s "$uploads_backup" >/dev/null
  cleanup_status="$(json_get "$state_json" '.task.cleanup')"
  printf '%s' "$cleanup_status"
}

assert_same_fingerprint() {
  local before="$1"
  local after="$2"
  local name="$3"
  [ "$before" = "$after" ] || fail "error: $name identity changed during smoke"
}

check_confirmation() {
  [ "${SHUNDA_CONFIRM_SYSTEM_UPDATE:-}" = "yes" ] || {
    fail "error: explicit confirmation is required via SHUNDA_CONFIRM_SYSTEM_UPDATE=yes" 2
  }
  case "$smoke_mode" in
    success) ;;
    rollback)
      [ "${SHUNDA_CONFIRM_ROLLBACK_SMOKE:-}" = "yes" ] || {
        fail "error: rollback smoke requires SHUNDA_CONFIRM_ROLLBACK_SMOKE=yes" 2
      }
      ;;
    *)
      fail "error: SHUNDA_SMOKE_MODE must be success or rollback" 2
      ;;
  esac
}

validate_started_task() {
  local payload="$1"
  local expected_task_id="$2"
  local response_task_id task_target
  response_task_id="$(json_get "$payload" '.id')"
  [ "$response_task_id" = "$expected_task_id" ] || fail "error: started task does not match caller task"
  task_target="$(json_get "$payload" '.to_version')"
  [ "$task_target" = "$expected_target" ] || fail "error: started task target does not match expected target"
}

validate_status_task() {
  local payload="$1"
  local expected_task_id="$2"
  local expected_stage="${3:-}"
  local status_task_id status_target stage
  status_task_id="$(json_get "$payload" '.task.id')"
  [ "$status_task_id" = "$expected_task_id" ] || fail "error: status task does not match started task"
  status_target="$(json_get "$payload" '.task.to_version')"
  [ "$status_target" = "$expected_target" ] || fail "error: status target does not match expected target"
  if [ -n "$expected_stage" ]; then
    stage="$(json_get "$payload" '.task.stage')"
    [ "$stage" = "$expected_stage" ] || fail "error: rollback target drifted before stop"
  fi
}

confirm_rollback_target() {
  local task_id="$1"
  local confirm_payload target_state target_repository target_digest target_image_id
  local target_web_id target_web_fingerprint expected_config_image
  confirm_payload="$(fetch_status_payload)"
  validate_status_task "$confirm_payload" "$task_id" "checking_health"
  target_state="$(read_private_state)"
  [ "$(json_get "$target_state" '.task.id')" = "$task_id" ] || {
    fail "error: rollback state task does not match caller task"
  }
  [ "$(json_get "$target_state" '.task.target.version')" = "$expected_target" ] || {
    fail "error: rollback state target version drifted before stop"
  }
  target_repository="$(json_get "$target_state" '.task.target.repository')"
  [ "$target_repository" = "$web_repository" ] || {
    fail "error: rollback state target repository drifted before stop"
  }
  target_digest="$(json_get "$target_state" '.task.target.digest')"
  [[ "$target_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    fail "error: rollback state target digest is invalid"
  }
  target_image_id="$(json_get "$target_state" '.task.target.image_id')"
  [[ "$target_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    fail "error: rollback state target image ID is invalid"
  }
  expected_config_image="$target_repository@$target_digest"
  target_web_id="$(service_container_id web)"
  target_web_fingerprint="$(container_fingerprint "$(inspect_container "$target_web_id")")"
  [ "$(json_get "$target_web_fingerprint" '.id')" = "$target_web_id" ] || {
    fail "error: rollback target container identity drifted before stop"
  }
  [ "$(json_get "$target_web_fingerprint" '.service')" = "web" ] || {
    fail "error: rollback mutation refused non-web container"
  }
  [ "$(json_get "$target_web_fingerprint" '.project')" = "$compose_project" ] || {
    fail "error: rollback mutation refused non-project container"
  }
  [ "$(json_get "$target_web_fingerprint" '.config_image')" = "$expected_config_image" ] || {
    fail "error: rollback target image drifted before stop"
  }
  [ "$(json_get "$target_web_fingerprint" '.image_id')" = "$target_image_id" ] || {
    fail "error: rollback target image ID drifted before stop"
  }
  [ "$(json_get "$target_web_fingerprint" '.version')" = "$expected_target" ] || {
    fail "error: rollback target version drifted before stop"
  }
  run_capture "rollback stop web" docker stop "$target_web_id" >/dev/null
}

smoke_mode="${SHUNDA_SMOKE_MODE:-success}"
check_confirmation
require_value SHUNDA_BASE_URL
require_value SHUNDA_OWNER_USERNAME
require_value SHUNDA_OWNER_PASSWORD
require_value SHUNDA_APP_DIR
require_value SHUNDA_EXPECTED_TARGET
require_version SHUNDA_EXPECTED_TARGET

expected_target="$SHUNDA_EXPECTED_TARGET"
base_url="${SHUNDA_BASE_URL%/}"
compose_project="${SHUNDA_COMPOSE_PROJECT:-shunda-finance}"
app_dir="$SHUNDA_APP_DIR"
compose_file="$app_dir/compose.yml"
env_file="$app_dir/.env"
state_file="${SHUNDA_UPDATER_STATE_FILE:-/state/update-state.json}"
login_url="$base_url/accounts/login/?next=/system/update/"
check_url="$base_url/system/update/check/"
start_url="$base_url/system/update/start/"
status_url="$base_url/system/update/status/"
health_url="$base_url/health/"

caller_task_id_file="$scratch_dir/caller-task-id"
write_private_file "$caller_task_id_file" python3 -c 'import uuid; print(uuid.uuid4())'
caller_task_id="$(<"$caller_task_id_file")"
[[ "$caller_task_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || {
  fail "error: private caller task identity is invalid"
}

login_page_file="$scratch_dir/login-page.html"
login_page="$(run_curl_config "login page" GET "$login_url" "" "" "" "" no)"
write_private_text "$login_page_file" "$login_page"
if ! login_csrf="$(html_csrf "$login_page_file" 2>/dev/null)"; then
  fail "error: login page did not provide csrf token"
fi
if ! initial_cookie_csrf="$(cookie_value "$cookie_jar" csrftoken 2>/dev/null)"; then
  fail "error: login cookie jar is missing csrftoken"
fi

login_body="$scratch_dir/login.body"
create_form_body "$login_body" "$SHUNDA_OWNER_USERNAME" "$SHUNDA_OWNER_PASSWORD" "$login_csrf" "/system/update/"
run_curl_config "owner login" POST "$login_url" "$login_body" "" "$login_url" "application/x-www-form-urlencoded" >/dev/null

if ! request_csrf="$(cookie_value "$cookie_jar" csrftoken 2>/dev/null)"; then
  fail "error: login did not refresh csrftoken"
fi
[ "$request_csrf" != "$initial_cookie_csrf" ] || fail "error: login did not rotate csrftoken"

check_body="$scratch_dir/check.json"
create_json_body "$check_body" '{}'
check_payload="$(run_curl_config "system update check" POST "$check_url" "$check_body" "$request_csrf" "$login_url" "application/json")"

check_current_version="$(json_get "$check_payload" '.current_version')"
check_target_version="$(json_get "$check_payload" '.latest_version')"
[ "$check_target_version" = "$expected_target" ] || fail "error: expected target does not match check result"

db_before_id="$(service_container_id db)"
updater_before_id="$(service_container_id updater)"
db_before_fingerprint="$(container_fingerprint "$(inspect_container "$db_before_id")")"
updater_before_fingerprint="$(container_fingerprint "$(inspect_container "$updater_before_id")")"
old_web_id="$(service_container_id web)"
old_web_id_file="$scratch_dir/old-web-id"
old_web_identity_file="$scratch_dir/old-web-identity.json"
image_inventory_before_file="$scratch_dir/image-inventory-before.jsonl"
write_private_text "$old_web_id_file" "$old_web_id"
write_private_text "$old_web_identity_file" "$(container_fingerprint "$(inspect_container "$old_web_id")")"
capture_image_inventory "$image_inventory_before_file"
validate_initial_web_identity \
  "$old_web_identity_file" "$old_web_id_file" "$image_inventory_before_file" "$caller_task_id_file"

start_body="$scratch_dir/start.json"
create_json_body "$start_body" "{\"target_version\":\"$expected_target\",\"task_id\":\"$caller_task_id\"}"
start_payload="$(run_curl_config "system update start" POST "$start_url" "$start_body" "$request_csrf" "$login_url" "application/json")"
validate_started_task "$start_payload" "$caller_task_id"
task_id="$caller_task_id"

rollback_mutation_done=0
final_status=""
final_stage=""
deadline=$(( $(current_seconds) + 600 ))
while [ "$(current_seconds)" -lt "$deadline" ]; do
  status_payload="$(fetch_status_payload)"
  validate_status_task "$status_payload" "$task_id"
  final_stage="$(json_get "$status_payload" '.task.stage')"
  final_status="$status_payload"

  if [ "$smoke_mode" = "rollback" ] && [ "$rollback_mutation_done" -eq 0 ] && [ "$final_stage" = "checking_health" ]; then
    confirm_rollback_target "$task_id"
    rollback_mutation_done=1
  fi

  case "$final_stage" in
    succeeded)
      [ "$smoke_mode" = "success" ] || fail "error: rollback smoke reached succeeded unexpectedly"
      break
      ;;
    failed)
      if [ "$smoke_mode" = "success" ]; then
        fail "error: success smoke ended in failed"
      fi
      [ "$(json_get "$status_payload" '.task.rolled_back | tostring')" = "true" ] || {
        fail "error: rollback smoke failed without rollback confirmation"
      }
      break
      ;;
    manual_intervention)
      fail "error: task entered manual intervention"
      ;;
  esac
  sleep 2
done

[ -n "$final_status" ] || fail "error: smoke did not return a status payload"
[ "$final_stage" = "succeeded" ] || [ "$final_stage" = "failed" ] || {
  fail "error: smoke did not reach terminal state within 10 minutes"
}
[ -n "$(json_get "$final_status" '.task.started_at')" ] || fail "error: task started_at is missing"
[ "$(json_get "$final_status" '.task.backup_complete | tostring')" = "true" ] || {
  fail "error: public task did not report backups complete"
}

public_cleanup_status=""
if [ "$smoke_mode" = "success" ]; then
  public_cleanup_status="$(json_get "$final_status" '.task.cleanup')"
  case "$public_cleanup_status" in
    complete|pending) ;;
    *) fail "error: cleanup status is invalid" ;;
  esac
fi

private_state="$(read_private_state)"
cleanup_status="$(assert_backup_paths "$private_state" "$task_id")"
if [ "$smoke_mode" = "success" ]; then
  case "$cleanup_status" in
    complete|pending) ;;
    *) fail "error: cleanup status is invalid" ;;
  esac
  [ "$cleanup_status" = "$public_cleanup_status" ] || {
    fail "error: cleanup status does not match terminal status"
  }
fi
db_after_fingerprint="$(container_fingerprint "$(inspect_container "$(service_container_id db)")")"
updater_after_fingerprint="$(container_fingerprint "$(inspect_container "$(service_container_id updater)")")"
assert_same_fingerprint "$db_before_fingerprint" "$db_after_fingerprint" "db"
assert_same_fingerprint "$updater_before_fingerprint" "$updater_after_fingerprint" "updater"

if [ "$smoke_mode" = "success" ]; then
  [ "$(json_get "$final_status" '.current_version')" = "$expected_target" ] || {
    fail "error: success smoke did not publish the expected version"
  }
  [ "$(json_get "$private_state" '.task.rolled_back | tostring')" = "false" ] || {
    fail "error: success smoke unexpectedly rolled back"
  }
  private_state_file="$scratch_dir/private-state.json"
  image_inventory_after_file="$scratch_dir/image-inventory-after.jsonl"
  write_private_text "$private_state_file" "$private_state"
  capture_image_inventory "$image_inventory_after_file"
  verify_cleanup_inventory "$cleanup_status" "$old_web_identity_file" \
    "$image_inventory_before_file" "$image_inventory_after_file" \
    "$private_state_file" "$caller_task_id_file"
  assert_health_ok
  if [ "$cleanup_status" = "pending" ]; then
    printf '%s\n' "$cleanup_pending_message"
  fi
  printf 'smoke complete for %s\n' "$expected_target"
else
  [ "$rollback_mutation_done" -eq 1 ] || fail "error: rollback smoke never stopped the target web container"
  [ "$(json_get "$final_status" '.current_version')" = "$check_current_version" ] || {
    fail "error: rollback smoke did not restore the previous version"
  }
  assert_health_ok
  run_capture "original image retained" docker image inspect "$web_repository:$check_current_version" >/dev/null
  run_capture "target image retained" docker image inspect "$web_repository:$expected_target" >/dev/null
  printf 'rollback smoke verified for %s\n' "$expected_target"
fi
