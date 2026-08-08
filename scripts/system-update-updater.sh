#!/bin/sh
set +x

requested_confirmation=${SHUNDA_CONFIRM_UPDATER_UPDATE-}
requested_target_tag=${SHUNDA_UPDATER_IMAGE_TAG-}
rejected_ambient=0
if [ -n "${DOCKER_HOST-}${DOCKER_CONTEXT-}${DOCKER_TLS-}${DOCKER_TLS_VERIFY-}${DOCKER_CERT_PATH-}${DOCKER_CONFIG-}${DOCKER_API_VERSION-}${DOCKER_CUSTOM_HEADERS-}${DOCKER_DEFAULT_PLATFORM-}${DOCKER_CONTENT_TRUST-}${DOCKER_CONTENT_TRUST_SERVER-}${DOCKER_CLI_PLUGIN_EXTRA_DIRS-}${DOCKER_CLI_EXPERIMENTAL-}${DOCKER_BUILDKIT-}${DOCKER_CLI_HINTS-}${COMPOSE_FILE-}${COMPOSE_PROJECT_NAME-}${COMPOSE_PROFILES-}${COMPOSE_ENV_FILES-}${COMPOSE_PATH_SEPARATOR-}${COMPOSE_CONVERT_WINDOWS_PATHS-}${COMPOSE_ANSI-}${COMPOSE_STATUS_STDOUT-}${COMPOSE_PARALLEL_LIMIT-}${COMPOSE_IGNORE_ORPHANS-}${COMPOSE_REMOVE_ORPHANS-}${COMPOSE_EXPERIMENTAL-}${COMPOSE_MENU-}" ]; then
  rejected_ambient=1
fi

unset \
  BASH_ENV ENV CDPATH GLOBIGNORE HOME PATH \
  LD_AUDIT LD_DEBUG LD_LIBRARY_PATH LD_PRELOAD \
  PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE PYTHONINSPECT \
  PYTHONWARNINGS PYTHONSAFEPATH PYTHONNOUSERSITE \
  DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS DOCKER_TLS_VERIFY DOCKER_CERT_PATH \
  DOCKER_CONFIG DOCKER_API_VERSION DOCKER_CUSTOM_HEADERS DOCKER_DEFAULT_PLATFORM \
  DOCKER_CONTENT_TRUST DOCKER_CONTENT_TRUST_SERVER DOCKER_CLI_PLUGIN_EXTRA_DIRS \
  DOCKER_CLI_EXPERIMENTAL DOCKER_BUILDKIT DOCKER_CLI_HINTS \
  COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES \
  COMPOSE_PATH_SEPARATOR COMPOSE_CONVERT_WINDOWS_PATHS COMPOSE_ANSI \
  COMPOSE_STATUS_STDOUT COMPOSE_PARALLEL_LIMIT COMPOSE_IGNORE_ORPHANS \
  COMPOSE_REMOVE_ORPHANS COMPOSE_EXPERIMENTAL COMPOSE_MENU \
  XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_HOME XDG_RUNTIME_DIR
PATH=/usr/bin:/usr/local/bin:/bin
LC_ALL=C
export PATH LC_ALL

if [ "$requested_confirmation" != "yes" ]; then
  printf '%s\n' 'updater update requires manual intervention' >&2
  exit 1
fi

if [ -x /usr/bin/stat ]; then
  os_tcb_stat=/usr/bin/stat
elif [ -x /bin/stat ]; then
  os_tcb_stat=/bin/stat
else
  exit 1
fi
if [ -x /usr/bin/readlink ]; then
  os_tcb_readlink=/usr/bin/readlink
elif [ -x /bin/readlink ]; then
  os_tcb_readlink=/bin/readlink
else
  exit 1
fi
if [ -x /usr/bin/env ]; then
  os_tcb_env=/usr/bin/env
elif [ -x /bin/env ]; then
  os_tcb_env=/bin/env
else
  exit 1
fi

mode_is_safe() {
  case "$1" in
    ""|*[!0-7]*) return 1 ;;
  esac
  mode_other=${1#"${1%?}"}
  mode_prefix=${1%?}
  mode_group=${mode_prefix#"${mode_prefix%?}"}
  case "$mode_group$mode_other" in
    *[2367]*) return 1 ;;
  esac
  return 0
}

tcb_validate_directory() {
  [ -d "$1" ] || return 1
  directory_metadata="$("$os_tcb_stat" -Lc '%u:%a' -- "$1" 2>/dev/null)" || return 1
  directory_uid=${directory_metadata%%:*}
  directory_mode=${directory_metadata#*:}
  [ "$directory_uid" = "0" ] || return 1
  mode_is_safe "$directory_mode"
}

tcb_validate_python() {
  python_candidate=$1
  case "$python_candidate" in
    /usr/bin/python3|/usr/local/bin/python3) ;;
    *) return 1 ;;
  esac
  [ -e "$python_candidate" ] || [ -L "$python_candidate" ] || return 1
  python_requested_uid="$("$os_tcb_stat" -c '%u' -- "$python_candidate" 2>/dev/null)" || return 1
  [ "$python_requested_uid" = "0" ] || return 1
  if [ ! -L "$python_candidate" ]; then
    [ -f "$python_candidate" ] || return 1
    python_requested_mode="$("$os_tcb_stat" -c '%a' -- "$python_candidate" 2>/dev/null)" || return 1
    mode_is_safe "$python_requested_mode" || return 1
  fi
  python_resolved="$("$os_tcb_readlink" -f -- "$python_candidate" 2>/dev/null)" || return 1
  case "$python_resolved" in
    /usr/bin/*)
      tcb_validate_directory / || return 1
      tcb_validate_directory /usr || return 1
      tcb_validate_directory /usr/bin || return 1
      ;;
    /usr/local/bin/*)
      tcb_validate_directory / || return 1
      tcb_validate_directory /usr || return 1
      tcb_validate_directory /usr/local || return 1
      tcb_validate_directory /usr/local/bin || return 1
      ;;
    *) return 1 ;;
  esac
  [ -f "$python_resolved" ] && [ -x "$python_resolved" ] || return 1
  python_resolved_metadata="$("$os_tcb_stat" -Lc '%u:%a' -- "$python_resolved" 2>/dev/null)" || return 1
  python_resolved_uid=${python_resolved_metadata%%:*}
  python_resolved_mode=${python_resolved_metadata#*:}
  [ "$python_resolved_uid" = "0" ] || return 1
  mode_is_safe "$python_resolved_mode"
}

python_binary=""
for python_candidate in /usr/bin/python3 /usr/local/bin/python3; do
  if tcb_validate_python "$python_candidate"; then
    python_binary=$python_candidate
    break
  fi
done
[ -n "$python_binary" ] || exit 1

validated_candidates="$(
  "$os_tcb_env" -i PATH=/usr/bin:/usr/local/bin:/bin \
    "$python_binary" -I -S - "$python_binary" 2>/dev/null <<'PY'
import os
import stat
import sys
from pathlib import Path

python_candidate = sys.argv[1]
binary_parents = {Path("/usr/bin"), Path("/usr/local/bin")}
synology_docker = Path("/usr/local/bin/docker")
synology_docker_target = "/var/packages/ContainerManager/target/usr/bin/docker"
synology_package_link = Path("/var/packages/ContainerManager/target")
synology_package_target = "/volume4/@appstore/ContainerManager"
synology_docker_resolved = Path("/volume4/@appstore/ContainerManager/usr/bin/docker")
plugin_parents = {
    Path("/usr/lib/docker/cli-plugins"),
    Path("/usr/libexec/docker/cli-plugins"),
    Path("/usr/local/lib/docker/cli-plugins"),
    Path("/usr/local/libexec/docker/cli-plugins"),
}
groups = [
    ("bash", ["/bin/bash", "/usr/bin/bash"], binary_parents),
    ("python", [python_candidate], binary_parents),
    ("docker", ["/usr/bin/docker", "/usr/local/bin/docker"], binary_parents),
    ("mktemp", ["/bin/mktemp", "/usr/bin/mktemp", "/usr/local/bin/mktemp"], binary_parents),
    ("mkdir", ["/bin/mkdir", "/usr/bin/mkdir", "/usr/local/bin/mkdir"], binary_parents),
    ("chmod", ["/bin/chmod", "/usr/bin/chmod", "/usr/local/bin/chmod"], binary_parents),
    ("cmp", ["/bin/cmp", "/usr/bin/cmp", "/usr/local/bin/cmp"], binary_parents),
    ("rm", ["/bin/rm", "/usr/bin/rm", "/usr/local/bin/rm"], binary_parents),
    ("sleep", ["/bin/sleep", "/usr/bin/sleep", "/usr/local/bin/sleep"], binary_parents),
    (
        "compose-plugin",
        [
            "/usr/local/lib/docker/cli-plugins/docker-compose",
            "/usr/local/libexec/docker/cli-plugins/docker-compose",
            "/usr/lib/docker/cli-plugins/docker-compose",
            "/usr/libexec/docker/cli-plugins/docker-compose",
        ],
        plugin_parents,
    ),
]


def safe_mode(value: os.stat_result) -> bool:
    return value.st_uid == 0 and stat.S_IMODE(value.st_mode) & 0o022 == 0


def validate_directory_chain(path: Path) -> None:
    current = Path("/")
    root = current.lstat()
    if not stat.S_ISDIR(root.st_mode) or not safe_mode(root):
        raise ValueError("unsafe root")
    for component in path.parts[1:]:
        current /= component
        value = current.lstat()
        if not stat.S_ISDIR(value.st_mode) or not safe_mode(value):
            raise ValueError("unsafe directory")


def validate_requested_parents(path: Path) -> None:
    current = Path("/")
    for component in path.parent.parts[1:]:
        current /= component
        value = current.lstat()
        if stat.S_ISLNK(value.st_mode):
            if value.st_uid != 0:
                raise ValueError("unsafe parent symlink")
            validate_directory_chain(current.resolve(strict=True))
        elif not stat.S_ISDIR(value.st_mode) or not safe_mode(value):
            raise ValueError("unsafe requested parent")


def validate_synology_docker_link(requested: Path, resolved: Path) -> None:
    if (
        requested != synology_docker
        or os.readlink(requested) != synology_docker_target
        or resolved != synology_docker_resolved
    ):
        raise ValueError("unexpected Synology Docker candidate")
    validate_directory_chain(synology_package_link.parent)
    package_link_value = synology_package_link.lstat()
    if (
        not stat.S_ISLNK(package_link_value.st_mode)
        or package_link_value.st_uid != 0
        or os.readlink(synology_package_link) != synology_package_target
    ):
        raise ValueError("unsafe Synology package link")
    validate_directory_chain(synology_docker_resolved.parent)


def validate_candidate(raw_path: str, allowed_parents: set[Path]) -> str:
    requested = Path(raw_path)
    requested_value = requested.lstat()
    if stat.S_ISLNK(requested_value.st_mode):
        if requested_value.st_uid != 0:
            raise ValueError("unsafe candidate symlink")
    elif not stat.S_ISREG(requested_value.st_mode) or not safe_mode(requested_value):
        raise ValueError("unsafe requested candidate")
    else:
        if not os.access(requested, os.X_OK):
            raise ValueError("candidate is not executable")
    validate_requested_parents(requested)
    resolved = requested.resolve(strict=True)
    if resolved.parent not in allowed_parents:
        validate_synology_docker_link(requested, resolved)
    validate_directory_chain(resolved.parent)
    resolved_value = resolved.stat()
    if (
        not stat.S_ISREG(resolved_value.st_mode)
        or not safe_mode(resolved_value)
        or not os.access(resolved, os.X_OK)
    ):
        raise ValueError("unsafe resolved candidate")
    return raw_path


selected = []
for _, candidates, allowed_parents in groups:
    for candidate in candidates:
        try:
            selected.append(validate_candidate(candidate, allowed_parents))
        except (OSError, ValueError):
            continue
        break
    else:
        raise SystemExit(1)
print(" ".join(selected))
PY
)" || exit 1
set -- $validated_candidates
[ "$#" -eq 10 ] || exit 1
bash_binary=$1
python_binary=$2
docker_binary=$3
mktemp_binary=$4
mkdir_binary=$5
chmod_binary=$6
cmp_binary=$7
rm_binary=$8
sleep_binary=$9
shift 9
compose_plugin=$1

case "$0" in
  */*) updater_script_directory=${0%/*} ;;
  *) updater_script_directory=. ;;
esac
updater_script_directory="$(CDPATH= cd -- "$updater_script_directory" && pwd -P)" || exit 1
atomic_helper="$updater_script_directory/system-update-updater-atomic.py"

exec "$os_tcb_env" -i \
  PATH=/usr/bin:/usr/local/bin:/bin \
  SHUNDA_CONFIRM_UPDATER_UPDATE="$requested_confirmation" \
  SHUNDA_UPDATER_IMAGE_TAG="$requested_target_tag" \
  SHUNDA_UPDATER_REJECTED_AMBIENT="$rejected_ambient" \
  SHUNDA_UPDATER_PYTHON_BINARY="$python_binary" \
  SHUNDA_UPDATER_DOCKER_BINARY="$docker_binary" \
  SHUNDA_UPDATER_MKTEMP_BINARY="$mktemp_binary" \
  SHUNDA_UPDATER_MKDIR_BINARY="$mkdir_binary" \
  SHUNDA_UPDATER_CHMOD_BINARY="$chmod_binary" \
  SHUNDA_UPDATER_CMP_BINARY="$cmp_binary" \
  SHUNDA_UPDATER_RM_BINARY="$rm_binary" \
  SHUNDA_UPDATER_SLEEP_BINARY="$sleep_binary" \
  SHUNDA_UPDATER_COMPOSE_PLUGIN="$compose_plugin" \
  "$python_binary" -I -S "$atomic_helper" lock-exec \
  "$bash_binary" "$0" "$@" <<'SHUNDA_UPDATER_BASH'
set +x
set -euo pipefail

[ "$#" -ge 1 ] || exit 1
updater_script_path="$1"
shift
case "$updater_script_path" in
  */*) updater_script_directory="${updater_script_path%/*}" ;;
  *) updater_script_directory="." ;;
esac
updater_script_directory="$(CDPATH= cd -- "$updater_script_directory" && pwd -P)" || exit 1
readonly updater_script_directory
readonly atomic_helper="$updater_script_directory/system-update-updater-atomic.py"

umask 077

readonly compose_project="shunda-finance"
readonly app_dir="/volume4/docker/docker/shunda-finance/app"
readonly env_file="$app_dir/.env"
readonly compose_file="$app_dir/compose.yml"
readonly docker_socket="unix:///var/run/docker.sock"
readonly updater_repository="ghcr.io/s450586793/shunda-finance-updater"
readonly success_message="updater update completed"
readonly failure_message="updater update requires manual intervention"
readonly health_attempts=30
readonly health_sleep_seconds=2
[ -n "${SHUNDA_UPDATER_PYTHON_BINARY:-}" ] || exit 1
[ -n "${SHUNDA_UPDATER_DOCKER_BINARY:-}" ] || exit 1
[ -n "${SHUNDA_UPDATER_MKTEMP_BINARY:-}" ] || exit 1
[ -n "${SHUNDA_UPDATER_MKDIR_BINARY:-}" ] || exit 1
[ -n "${SHUNDA_UPDATER_CHMOD_BINARY:-}" ] || exit 1
[ -n "${SHUNDA_UPDATER_CMP_BINARY:-}" ] || exit 1
[ -n "${SHUNDA_UPDATER_RM_BINARY:-}" ] || exit 1
[ -n "${SHUNDA_UPDATER_SLEEP_BINARY:-}" ] || exit 1
[ -n "${SHUNDA_UPDATER_COMPOSE_PLUGIN:-}" ] || exit 1
readonly python_binary="$SHUNDA_UPDATER_PYTHON_BINARY"
readonly docker_binary="$SHUNDA_UPDATER_DOCKER_BINARY"
readonly mktemp_binary="$SHUNDA_UPDATER_MKTEMP_BINARY"
readonly mkdir_binary="$SHUNDA_UPDATER_MKDIR_BINARY"
readonly chmod_binary="$SHUNDA_UPDATER_CHMOD_BINARY"
readonly cmp_binary="$SHUNDA_UPDATER_CMP_BINARY"
readonly rm_binary="$SHUNDA_UPDATER_RM_BINARY"
readonly sleep_binary="$SHUNDA_UPDATER_SLEEP_BINARY"
readonly compose_plugin="$SHUNDA_UPDATER_COMPOSE_PLUGIN"
readonly compose_plugin_directory="${compose_plugin%/*}"

scratch_dir=""
operation_succeeded=0

compose() {
  "$docker_binary" --host "$docker_socket" compose \
    --project-name "$compose_project" \
    --env-file "$env_file" \
    -f "$compose_file" \
    "$@"
}


run_private() {
  local label="$1"
  shift
  "$@" \
    >"$scratch_dir/$label.stdout" \
    2>"$scratch_dir/$label.stderr"
}

capture_environment() {
  "$python_binary" -I -S "$atomic_helper" capture \
    "$app_dir" \
    "$env_file" \
    "$compose_file" \
    "$target_tag" \
    "$scratch_dir/original.env" \
    "$scratch_dir/target.env" \
    "$scratch_dir/old-tag" \
    "$scratch_dir/env-stat.json" \
    >"$scratch_dir/env-capture.stdout" \
    2>"$scratch_dir/env-capture.stderr"
}

validate_pulled_image() {
  "$python_binary" -I -S - \
    "$scratch_dir/image-inspect.stdout" \
    "$target_ref" \
    "$target_tag" \
    "$scratch_dir/new-image-id" \
    >"$scratch_dir/image-validate.stdout" \
    2>"$scratch_dir/image-validate.stderr" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
target_ref = sys.argv[2]
target_tag = sys.argv[3]
output_path = Path(sys.argv[4])
digest_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")
if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
    raise SystemExit(1)
image = payload[0]
image_id = image.get("Id")
repo_tags = image.get("RepoTags")
labels = image.get("Config", {}).get("Labels", {}) if isinstance(image.get("Config"), dict) else {}
if (
    not isinstance(image_id, str)
    or digest_pattern.fullmatch(image_id) is None
    or not isinstance(repo_tags, list)
    or target_ref not in repo_tags
    or not isinstance(labels, dict)
    or labels.get("org.opencontainers.image.version") != target_tag
):
    raise SystemExit(1)
descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
try:
    os.write(descriptor, image_id.encode("ascii"))
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

capture_service_fingerprint() {
  local service="$1"
  local output_path="$2"
  local label="$3"
  local container_id=""

  run_private "$label-ps" compose ps -q "$service" || return 1
  container_id="$(<"$scratch_dir/$label-ps.stdout")"
  container_id="${container_id//$'\r'/}"
  container_id="${container_id//$'\n'/}"
  [[ "$container_id" =~ ^[0-9a-f]{64}$ ]] || return 1
  run_private "$label-inspect" "$docker_binary" --host "$docker_socket" inspect "$container_id" || return 1
  "$python_binary" -I -S - \
    "$scratch_dir/$label-inspect.stdout" \
    "$container_id" \
    "$service" \
    "$compose_project" \
    "$output_path" \
    >"$scratch_dir/$label-validate.stdout" \
    2>"$scratch_dir/$label-validate.stderr" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_id = sys.argv[2]
service = sys.argv[3]
project = sys.argv[4]
output_path = Path(sys.argv[5])
digest_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")
if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
    raise SystemExit(1)
container = payload[0]
state = container.get("State")
config = container.get("Config")
mounts = container.get("Mounts")
if (
    container.get("Id") != expected_id
    or not isinstance(container.get("Image"), str)
    or digest_pattern.fullmatch(container["Image"]) is None
    or not isinstance(state, dict)
    or not isinstance(state.get("StartedAt"), str)
    or not state["StartedAt"]
    or not isinstance(config, dict)
    or not isinstance(config.get("Image"), str)
    or not isinstance(config.get("Labels"), dict)
    or config["Labels"].get("com.docker.compose.project") != project
    or config["Labels"].get("com.docker.compose.service") != service
    or not isinstance(mounts, list)
    or any(not isinstance(mount, dict) for mount in mounts)
):
    raise SystemExit(1)
fingerprint = {
    "Id": container["Id"],
    "Image": container["Image"],
    "StartedAt": state["StartedAt"],
    "Mounts": mounts,
}
descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
try:
    os.write(descriptor, json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

capture_old_updater() {
  local container_id=""
  run_private "old-updater-ps" compose ps -q updater || return 1
  container_id="$(<"$scratch_dir/old-updater-ps.stdout")"
  container_id="${container_id//$'\r'/}"
  container_id="${container_id//$'\n'/}"
  [[ "$container_id" =~ ^[0-9a-f]{64}$ ]] || return 1
  run_private "old-updater-inspect" "$docker_binary" --host "$docker_socket" inspect "$container_id" || return 1
  "$python_binary" -I -S - \
    "$scratch_dir/old-updater-inspect.stdout" \
    "$container_id" \
    "$old_ref" \
    "$compose_project" \
    "$scratch_dir/old-image-id" \
    >"$scratch_dir/old-updater-validate.stdout" \
    2>"$scratch_dir/old-updater-validate.stderr" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_id = sys.argv[2]
expected_ref = sys.argv[3]
project = sys.argv[4]
output_path = Path(sys.argv[5])
digest_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")
if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
    raise SystemExit(1)
container = payload[0]
state = container.get("State")
config = container.get("Config")
labels = config.get("Labels") if isinstance(config, dict) else None
health = state.get("Health") if isinstance(state, dict) else None
image_id = container.get("Image")
if (
    container.get("Id") != expected_id
    or not isinstance(image_id, str)
    or digest_pattern.fullmatch(image_id) is None
    or not isinstance(config, dict)
    or config.get("Image") != expected_ref
    or not isinstance(labels, dict)
    or labels.get("com.docker.compose.project") != project
    or labels.get("com.docker.compose.service") != "updater"
    or not isinstance(state, dict)
    or state.get("Status") != "running"
    or not isinstance(health, dict)
    or health.get("Status") != "healthy"
):
    raise SystemExit(1)
descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
try:
    os.write(descriptor, image_id.encode("ascii"))
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

atomic_replace_environment() {
  local source_path="$1"
  local expected_path="$2"
  local expected_stat_path="$3"
  local next_stat_path="$4"
  local marker_path="${5:-}"

  "$python_binary" -I -S "$atomic_helper" replace \
    "$env_file" \
    "$source_path" \
    "$expected_path" \
    "$expected_stat_path" \
    "$next_stat_path" \
    "$marker_path" \
    >"$scratch_dir/env-replace.stdout" \
    2>"$scratch_dir/env-replace.stderr"
}

classify_environment_for_recovery() {
  "$python_binary" -I -S "$atomic_helper" classify \
    "$env_file" \
    "$scratch_dir/original.env" \
    "$scratch_dir/target.env" \
    "$scratch_dir/env-stat.json" \
    "$scratch_dir/recovery-stat.json" \
    "$scratch_dir/recovery-state" \
    >"$scratch_dir/recovery-classify.stdout" \
    2>"$scratch_dir/recovery-classify.stderr"
}

validate_updater_once() {
  local expected_ref="$1"
  local expected_image_id_path="$2"
  local label="$3"
  local container_id=""
  local validate_status=0

  run_private "$label-ps" compose ps -q updater || return 2
  container_id="$(<"$scratch_dir/$label-ps.stdout")"
  container_id="${container_id//$'\r'/}"
  container_id="${container_id//$'\n'/}"
  [ -n "$container_id" ] || return 2
  [[ "$container_id" =~ ^[0-9a-f]{64}$ ]] || return 1
  run_private "$label-inspect" "$docker_binary" --host "$docker_socket" inspect "$container_id" || return 2
  "$python_binary" -I -S - \
    "$scratch_dir/$label-inspect.stdout" \
    "$container_id" \
    "$expected_ref" \
    "$compose_project" \
    "$expected_image_id_path" \
    >"$scratch_dir/$label-validate.stdout" \
    2>"$scratch_dir/$label-validate.stderr" <<'PY' || validate_status=$?
import json
import re
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_id = sys.argv[2]
expected_ref = sys.argv[3]
project = sys.argv[4]
expected_image_id = Path(sys.argv[5]).read_text(encoding="ascii")
digest_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")
if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
    raise SystemExit(1)
container = payload[0]
state = container.get("State")
config = container.get("Config")
labels = config.get("Labels") if isinstance(config, dict) else None
health = state.get("Health") if isinstance(state, dict) else None
if (
    container.get("Id") != expected_id
    or container.get("Image") != expected_image_id
    or digest_pattern.fullmatch(expected_image_id) is None
    or not isinstance(config, dict)
    or config.get("Image") != expected_ref
    or not isinstance(labels, dict)
    or labels.get("com.docker.compose.project") != project
    or labels.get("com.docker.compose.service") != "updater"
    or not isinstance(state, dict)
    or not isinstance(health, dict)
):
    raise SystemExit(1)
if state.get("Status") != "running" or health.get("Status") != "healthy":
    raise SystemExit(2)
PY
  return "$validate_status"
}

wait_for_updater() {
  local expected_ref="$1"
  local expected_image_id_path="$2"
  local label_prefix="$3"
  local attempt=0
  local validate_status=0
  while [ "$attempt" -lt "$health_attempts" ]; do
    if validate_updater_once "$expected_ref" "$expected_image_id_path" "$label_prefix-$attempt"; then
      return 0
    else
      validate_status=$?
    fi
    [ "$validate_status" -eq 2 ] || return 1
    attempt=$((attempt + 1))
    if [ "$attempt" -lt "$health_attempts" ]; then
      run_private "$label_prefix-sleep-$attempt" "$sleep_binary" "$health_sleep_seconds" || return 1
    fi
  done
  return 1
}

prove_services_unchanged() {
  local label_prefix="$1"
  local status=0
  capture_service_fingerprint db "$scratch_dir/$label_prefix-db.fingerprint" "$label_prefix-db" || status=1
  capture_service_fingerprint web "$scratch_dir/$label_prefix-web.fingerprint" "$label_prefix-web" || status=1
  if [ "$status" -eq 0 ]; then
    "$cmp_binary" -s "$scratch_dir/baseline-db.fingerprint" "$scratch_dir/$label_prefix-db.fingerprint" || status=1
    "$cmp_binary" -s "$scratch_dir/baseline-web.fingerprint" "$scratch_dir/$label_prefix-web.fingerprint" || status=1
  fi
  return "$status"
}

recover_original_updater() {
  local recovery_status=0
  atomic_replace_environment \
    "$scratch_dir/original.env" \
    "$scratch_dir/target.env" \
    "$scratch_dir/recovery-stat.json" \
    "$scratch_dir/restored-stat.json" || return 1
  run_private "recovery-updater-up" compose up -d --no-deps updater || recovery_status=1
  wait_for_updater "$old_ref" "$scratch_dir/old-image-id" "recovery-updater" || recovery_status=1
  prove_services_unchanged "recovery" || recovery_status=1
  return "$recovery_status"
}

remove_scratch_dir() {
  [ -n "$scratch_dir" ] || return 1
  "$rm_binary" -rf -- "$scratch_dir" >/dev/null 2>&1 || return 1
  [ ! -e "$scratch_dir" ] && [ ! -L "$scratch_dir" ]
}

record_cleanup_failure() {
  local marker_path="$scratch_dir/cleanup-failed"
  [ -n "$scratch_dir" ] || return 1
  [ ! -e "$marker_path" ] && [ ! -L "$marker_path" ] || return 1
  (umask 077; : >"$marker_path") || return 1
  [ -f "$marker_path" ] && [ ! -L "$marker_path" ]
}

on_signal() {
  exit 1
}

on_exit() {
  local operation_status=$?
  local recovery_state=""
  local had_recovery_intent=0
  local recovery_attempted=0
  local recovery_proven=0
  local evidence_required=0
  trap - EXIT
  trap '' HUP INT TERM
  set +x
  set +e

  if [ "$operation_status" -eq 0 ] && [ "$operation_succeeded" -eq 1 ]; then
    if remove_scratch_dir; then
      printf '%s\n' "$success_message"
      exit 0
    fi
    record_cleanup_failure || true
    printf '%s\n' "$failure_message" >&2
    exit 1
  fi

  if [ -n "$scratch_dir" ] && [ -f "$scratch_dir/env-mutated" ]; then
    had_recovery_intent=1
  fi
  if [ "$operation_status" -ne 0 ] || [ "$operation_succeeded" -ne 1 ]; then
    if [ "$had_recovery_intent" -eq 1 ]; then
      if classify_environment_for_recovery; then
        recovery_state="$(<"$scratch_dir/recovery-state")"
        if [ "$recovery_state" = "target" ]; then
          recovery_attempted=1
          if recover_original_updater; then
            recovery_proven=1
          else
            evidence_required=1
          fi
        elif [ "$recovery_state" = "original" ]; then
          recovery_proven=1
        else
          evidence_required=1
        fi
      else
        evidence_required=1
      fi
    fi
  fi
  if [ -n "$scratch_dir" ] && {
    [ "$had_recovery_intent" -eq 0 ] || \
      { [ "$recovery_proven" -eq 1 ] && [ "$evidence_required" -eq 0 ]; }
  }; then
    if ! remove_scratch_dir; then
      evidence_required=1
      record_cleanup_failure || true
    fi
  fi
  printf '%s\n' "$failure_message" >&2
  exit 1
}

trap on_exit EXIT
trap on_signal HUP INT TERM

readonly requested_confirmation="${SHUNDA_CONFIRM_UPDATER_UPDATE:-}"
readonly requested_target_tag="${SHUNDA_UPDATER_IMAGE_TAG:-}"
[ "${SHUNDA_UPDATER_REJECTED_AMBIENT:-1}" = "0" ] || exit 1
[ "$EUID" -eq 0 ] || exit 1
for rejected_name in \
  DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS DOCKER_TLS_VERIFY DOCKER_CERT_PATH \
  DOCKER_CONFIG DOCKER_API_VERSION DOCKER_CUSTOM_HEADERS DOCKER_DEFAULT_PLATFORM \
  DOCKER_CONTENT_TRUST DOCKER_CONTENT_TRUST_SERVER DOCKER_CLI_PLUGIN_EXTRA_DIRS \
  DOCKER_CLI_EXPERIMENTAL DOCKER_BUILDKIT DOCKER_CLI_HINTS \
  COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES \
  COMPOSE_PATH_SEPARATOR COMPOSE_CONVERT_WINDOWS_PATHS COMPOSE_ANSI \
  COMPOSE_STATUS_STDOUT COMPOSE_PARALLEL_LIMIT COMPOSE_IGNORE_ORPHANS \
  COMPOSE_REMOVE_ORPHANS COMPOSE_EXPERIMENTAL COMPOSE_MENU; do
  [ -z "${!rejected_name:-}" ] || exit 1
done
unset \
  BASH_ENV ENV CDPATH GLOBIGNORE PATH PYTHONPATH PYTHONHOME PYTHONSTARTUP \
  PYTHONUSERBASE PYTHONINSPECT PYTHONWARNINGS PYTHONSAFEPATH PYTHONNOUSERSITE \
  DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS DOCKER_TLS_VERIFY DOCKER_CERT_PATH \
  DOCKER_CONFIG DOCKER_API_VERSION DOCKER_CUSTOM_HEADERS DOCKER_DEFAULT_PLATFORM \
  DOCKER_CONTENT_TRUST DOCKER_CONTENT_TRUST_SERVER DOCKER_CLI_PLUGIN_EXTRA_DIRS \
  DOCKER_CLI_EXPERIMENTAL DOCKER_BUILDKIT DOCKER_CLI_HINTS \
  COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES \
  COMPOSE_PATH_SEPARATOR COMPOSE_CONVERT_WINDOWS_PATHS COMPOSE_ANSI \
  COMPOSE_STATUS_STDOUT COMPOSE_PARALLEL_LIMIT COMPOSE_IGNORE_ORPHANS \
  COMPOSE_REMOVE_ORPHANS COMPOSE_EXPERIMENTAL COMPOSE_MENU
PATH="/usr/bin:/usr/local/bin"
export PATH

[ "$requested_confirmation" = "yes" ] || exit 1
readonly target_tag="$requested_target_tag"
[[ "$target_tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || exit 1
readonly target_ref="$updater_repository:$target_tag"

scratch_dir="$("$mktemp_binary" -d /var/tmp/shunda-updater-update.XXXXXX 2>/dev/null)" || exit 1
"$chmod_binary" 700 "$scratch_dir" >/dev/null 2>&1 || exit 1
for private_directory in home docker-config xdg-config xdg-cache xdg-data xdg-runtime; do
  "$mkdir_binary" -m 0700 -- "$scratch_dir/$private_directory" >/dev/null 2>&1 || exit 1
done
HOME="$scratch_dir/home"
DOCKER_CONFIG="$scratch_dir/docker-config"
XDG_CONFIG_HOME="$scratch_dir/xdg-config"
XDG_CACHE_HOME="$scratch_dir/xdg-cache"
XDG_DATA_HOME="$scratch_dir/xdg-data"
XDG_RUNTIME_DIR="$scratch_dir/xdg-runtime"
DOCKER_CLI_PLUGIN_EXTRA_DIRS="$compose_plugin_directory"
export \
  HOME \
  DOCKER_CONFIG \
  DOCKER_CLI_PLUGIN_EXTRA_DIRS \
  XDG_CONFIG_HOME \
  XDG_CACHE_HOME \
  XDG_DATA_HOME \
  XDG_RUNTIME_DIR

capture_environment || exit 1
readonly old_tag="$(<"$scratch_dir/old-tag")"
readonly old_ref="$updater_repository:$old_tag"

run_private "pull" "$docker_binary" --host "$docker_socket" pull "$target_ref" || exit 1
run_private "image-inspect" "$docker_binary" --host "$docker_socket" image inspect "$target_ref" || exit 1
validate_pulled_image || exit 1

capture_service_fingerprint db "$scratch_dir/baseline-db.fingerprint" "baseline-db" || exit 1
capture_service_fingerprint web "$scratch_dir/baseline-web.fingerprint" "baseline-web" || exit 1
capture_old_updater || exit 1

atomic_replace_environment \
  "$scratch_dir/target.env" \
  "$scratch_dir/original.env" \
  "$scratch_dir/env-stat.json" \
  "$scratch_dir/target-stat.json" \
  "$scratch_dir/env-mutated" || exit 1

run_private "new-updater-up" compose up -d --no-deps updater || exit 1
wait_for_updater "$target_ref" "$scratch_dir/new-image-id" "new-updater" || exit 1
prove_services_unchanged "success" || exit 1

operation_succeeded=1
SHUNDA_UPDATER_BASH
