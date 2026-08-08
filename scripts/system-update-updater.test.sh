#!/usr/bin/env bash
set -euo pipefail

project_root="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)"
script_path="$project_root/scripts/system-update-updater.sh"
original_path="$PATH"
suite_dir="$(mktemp -d)"
production_lock_file="/run/shunda-system-update-updater.lock"
fixed_root="/volume4/docker/docker/shunda-finance"
fixed_app_dir="$fixed_root/app"
fixed_env_file="$fixed_app_dir/.env"
fixed_fixture_created=0
fixed_root_identity=""
fixed_app_identity=""
fixed_marker_identity=""
fixed_marker_value=""

cleanup_fixed_fixture() {
  if [ "$fixed_fixture_created" -eq 1 ]; then
    sudo -n /volume4/.shunda-test-bin/python3 -I -S - \
      "$fixed_root_identity" \
      "$fixed_app_identity" \
      "$fixed_marker_identity" \
      "$fixed_marker_value" \
      >/dev/null 2>&1 <<'PY' || return 1
import os
import stat
import sys

expected_root, expected_app, expected_marker, marker_value = sys.argv[1:]


def identity(value: os.stat_result) -> str:
    return f"{value.st_dev}:{value.st_ino}"


parent_descriptor = os.open(
    "/volume4/docker/docker",
    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
)
root_descriptor = -1
app_descriptor = -1
marker_descriptor = -1
try:
    root_link = os.stat(
        "shunda-finance",
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    root_descriptor = os.open(
        "shunda-finance",
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=parent_descriptor,
    )
    root_opened = os.fstat(root_descriptor)
    if (
        not stat.S_ISDIR(root_opened.st_mode)
        or root_opened.st_uid != 0
        or stat.S_IMODE(root_opened.st_mode) != 0o700
        or identity(root_link) != expected_root
        or identity(root_opened) != expected_root
    ):
        raise SystemExit(1)

    app_link = os.stat("app", dir_fd=root_descriptor, follow_symlinks=False)
    app_descriptor = os.open(
        "app",
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=root_descriptor,
    )
    app_opened = os.fstat(app_descriptor)
    if (
        not stat.S_ISDIR(app_opened.st_mode)
        or app_opened.st_uid != 0
        or stat.S_IMODE(app_opened.st_mode) != 0o700
        or identity(app_link) != expected_app
        or identity(app_opened) != expected_app
    ):
        raise SystemExit(1)

    marker_link = os.stat(
        ".shunda-updater-test-owner",
        dir_fd=root_descriptor,
        follow_symlinks=False,
    )
    marker_descriptor = os.open(
        ".shunda-updater-test-owner",
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=root_descriptor,
    )
    marker_opened = os.fstat(marker_descriptor)
    marker_payload = os.read(marker_descriptor, len(marker_value) + 1)
    if (
        not stat.S_ISREG(marker_opened.st_mode)
        or marker_opened.st_uid != 0
        or stat.S_IMODE(marker_opened.st_mode) != 0o600
        or identity(marker_link) != expected_marker
        or identity(marker_opened) != expected_marker
        or marker_payload != marker_value.encode("ascii")
    ):
        raise SystemExit(1)

    root_entries = {entry.name for entry in os.scandir(root_descriptor)}
    if root_entries != {"app", ".shunda-updater-test-owner"}:
        raise SystemExit(1)
    allowed_app_entries = {".env", "compose.yml", "real.env"}
    app_entries = {entry.name for entry in os.scandir(app_descriptor)}
    if not app_entries.issubset(allowed_app_entries):
        raise SystemExit(1)
    for name in app_entries:
        value = os.stat(name, dir_fd=app_descriptor, follow_symlinks=False)
        if stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode):
            os.unlink(name, dir_fd=app_descriptor)
        elif name == ".env" and stat.S_ISDIR(value.st_mode):
            os.rmdir(name, dir_fd=app_descriptor)
        else:
            raise SystemExit(1)
    os.fsync(app_descriptor)
    os.close(app_descriptor)
    app_descriptor = -1
    os.rmdir("app", dir_fd=root_descriptor)
    os.unlink(".shunda-updater-test-owner", dir_fd=root_descriptor)
    os.fsync(root_descriptor)
    os.close(root_descriptor)
    root_descriptor = -1
    os.rmdir("shunda-finance", dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)
finally:
    if marker_descriptor >= 0:
        os.close(marker_descriptor)
    if app_descriptor >= 0:
        os.close(app_descriptor)
    if root_descriptor >= 0:
        os.close(root_descriptor)
    os.close(parent_descriptor)
PY
    sudo -n rmdir /volume4/docker/docker /volume4/docker >/dev/null 2>&1 || return 1
    fixed_fixture_created=0
  fi
}

cleanup_suite() {
  local status=$?
  trap - EXIT
  cleanup_fixed_fixture || true
  sudo -n rm -rf -- "$suite_dir" >/dev/null 2>&1 || true
  exit "$status"
}

trap cleanup_suite EXIT

fail_test() {
  printf '%s\n' "$1" >&2
  exit 1
}

run_rejected_case() {
  local name="$1"
  local invocation_mode="$2"
  shift 2
  local case_dir status
  local -a command environment
  case_dir="$(mktemp -d "$suite_dir/case.XXXXXX")"
  mkdir -p "$case_dir/bin"
  prepare_fake_control rejected
  cat >"$case_dir/bin/docker" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
: >"${FAKE_DOCKER_CALLED:?}"
exit 97
SH
  chmod +x "$case_dir/bin/docker"

  environment=(
    PATH="$case_dir/bin:$original_path" \
    FAKE_DOCKER_CALLED="$case_dir/docker-called" \
    DOCKER_HOST= \
    DOCKER_CONTEXT= \
    DOCKER_TLS_VERIFY= \
    DOCKER_CERT_PATH= \
    "$@"
  )
  case "$invocation_mode" in
    direct) command=(env "${environment[@]}" bash "$script_path") ;;
    root) command=(sudo -n env "${environment[@]}" bash "$script_path") ;;
    *) fail_test "unknown invocation mode" ;;
  esac

  set +e
  "${command[@]}" >"$case_dir/stdout" 2>"$case_dir/stderr"
  status=$?
  set -e

  [ "$status" -ne 0 ] || fail_test "$name must be rejected"
  [ ! -e "$case_dir/docker-called" ] || fail_test "$name reached Docker before validation"
  if [ -d /volume4/.shunda-updater-fake ] && [ -s /volume4/.shunda-updater-fake/docker.log ]; then
    fail_test "$name reached fixed Docker before validation"
  fi
  [ ! -s "$case_dir/stdout" ] || fail_test "$name wrote public stdout"
  [ "$(<"$case_dir/stderr")" = "updater update requires manual intervention" ] || {
    fail_test "$name did not return the fixed safe failure message"
  }
}

install_env_fixture() {
  local fixture_path="$1"
  sudo -n rm -rf -- "$fixed_env_file"
  sudo -n install -o root -g root -m 0600 "$fixture_path" "$fixed_env_file"
}

write_fake_tools() {
  local bin_dir="$1"
  cat >"$bin_dir/python3" <<'PY'
#!/volume4/.shunda-test-bin/python3
from __future__ import annotations

import importlib.util
import os
import sys

real_python = "/volume4/.shunda-test-bin/python3"
control_scenario = "/volume4/.shunda-updater-fake/scenario"
scenario = (
    open(control_scenario, encoding="ascii").read()
    if os.path.isfile(control_scenario)
    else os.environ.get("FAKE_SCENARIO")
)
try:
    stdin_index = sys.argv.index("-")
except ValueError:
    atomic_index = next(
        (
            index
            for index, value in enumerate(sys.argv)
            if value.endswith("/scripts/system-update-updater-atomic.py")
        ),
        None,
    )
    inject_atomic_fault = (
        atomic_index is not None
        and len(sys.argv) > atomic_index + 2
        and sys.argv[atomic_index + 1] == "replace"
        and bool(sys.argv[-1])
    )
    if inject_atomic_fault and scenario in {
        "parent_fsync_failure",
        "postcondition_failure",
        "next_stat_failure",
        "postcondition_inode_replacement",
    }:
        helper_path = sys.argv[atomic_index]
        specification = importlib.util.spec_from_file_location("shunda_updater_atomic", helper_path)
        if specification is None or specification.loader is None:
            raise SystemExit(94)
        helper = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(helper)
        if scenario == "parent_fsync_failure":
            def fail_parent_fsync(path):
                raise OSError(5, "injected directory fsync failure")

            helper.fsync_replaced_parent = fail_parent_fsync
        elif scenario == "postcondition_failure":
            def fail_postcondition(path, source, expected_stat, temporary_stat):
                raise OSError(5, "injected postcondition failure")

            helper.validate_replaced_environment = fail_postcondition
        elif scenario == "next_stat_failure":
            def fail_next_stat(path, value):
                raise OSError(5, "injected next-stat failure")

            helper.write_next_stat = fail_next_stat
        else:
            validate_replaced_environment = helper.validate_replaced_environment

            def replace_postcondition_inode(path, source, expected_stat, temporary_stat):
                replacement_path = path.with_name(".system-update-updater.replacement")
                descriptor = os.open(
                    replacement_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                )
                try:
                    os.write(descriptor, source)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.replace(replacement_path, path)
                return validate_replaced_environment(path, source, expected_stat, temporary_stat)

            helper.validate_replaced_environment = replace_postcondition_inode
        sys.argv = [helper_path, *sys.argv[atomic_index + 1:]]
        helper.main()
        raise SystemExit(0)
    os.execv(real_python, [real_python, *sys.argv[1:]])

script = sys.stdin.read()

sys.argv = ["-", *sys.argv[stdin_index + 1:]]
validation_scenarios = {
    "recovery_image_mismatch",
    "recovery_ref_mismatch",
    "recovery_project_mismatch",
    "recovery_service_mismatch",
    "recovery_unhealthy",
}
is_updater_validation = "expected_image_id = Path(sys.argv[5])" in script
validation_phase = "recovery" if any("recovery-updater" in value for value in sys.argv[1:]) else "new"
validation_status = 0
try:
    exec(compile(script, "<stdin>", "exec"), {"__name__": "__main__"})
except SystemExit as error:
    validation_status = error.code if isinstance(error.code, int) else 1
    raise
finally:
    if scenario in validation_scenarios and is_updater_validation:
        with open("/volume4/.shunda-updater-fake/docker.log", "a", encoding="ascii") as handle:
            handle.write(
                f'{{"action":"updater-validation","phase":"{validation_phase}","status":{validation_status}}}\n'
            )
        os.chmod("/volume4/.shunda-updater-fake/docker.log", 0o600)
PY
  chmod +x "$bin_dir/python3"

  cat >"$bin_dir/untrusted-python3" <<'PY'
#!/volume4/.shunda-test-bin/python3
import os
import sys
from pathlib import Path

Path("/volume4/.shunda-updater-fake/untrusted-python-called").touch()
os.execv("/usr/local/bin/python3", ["/usr/local/bin/python3", *sys.argv[1:]])
PY
  chmod +x "$bin_dir/untrusted-python3"

  cat >"$bin_dir/docker" <<'PY'
#!/usr/local/bin/python3
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

FORBIDDEN_AMBIENT = {
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
    "DOCKER_API_VERSION", "DOCKER_CUSTOM_HEADERS", "DOCKER_DEFAULT_PLATFORM",
    "DOCKER_CONTENT_TRUST", "DOCKER_CONTENT_TRUST_SERVER",
    "DOCKER_CLI_EXPERIMENTAL", "DOCKER_BUILDKIT", "DOCKER_CLI_HINTS",
    "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES",
    "COMPOSE_PATH_SEPARATOR", "COMPOSE_CONVERT_WINDOWS_PATHS", "COMPOSE_ANSI",
    "COMPOSE_STATUS_STDOUT", "COMPOSE_PARALLEL_LIMIT", "COMPOSE_IGNORE_ORPHANS",
    "COMPOSE_REMOVE_ORPHANS", "COMPOSE_EXPERIMENTAL", "COMPOSE_MENU",
}
if FORBIDDEN_AMBIENT.intersection(os.environ):
    raise SystemExit(89)

private_directories = {
    "HOME": "home",
    "DOCKER_CONFIG": "docker-config",
    "XDG_CONFIG_HOME": "xdg-config",
    "XDG_CACHE_HOME": "xdg-cache",
    "XDG_DATA_HOME": "xdg-data",
    "XDG_RUNTIME_DIR": "xdg-runtime",
}
if os.environ.get("SHUNDA_FAKE_DOCKER_PREFLIGHT") != "1":
    private_parent = None
    for variable, expected_name in private_directories.items():
        raw_path = os.environ.get(variable)
        if raw_path is None:
            raise SystemExit(88)
        path = Path(raw_path)
        value = path.lstat()
        if (
            path.name != expected_name
            or not stat.S_ISDIR(value.st_mode)
            or value.st_uid != 0
            or value.st_gid != 0
            or stat.S_IMODE(value.st_mode) != 0o700
            or any(path.iterdir())
        ):
            raise SystemExit(88)
        if private_parent is None:
            private_parent = path.parent
        elif path.parent != private_parent:
            raise SystemExit(88)
    if private_parent is None or not private_parent.name.startswith("shunda-updater-update."):
        raise SystemExit(88)
    plugin_directory = os.environ.get("DOCKER_CLI_PLUGIN_EXTRA_DIRS")
    plugin_path = Path("/usr/lib/docker/cli-plugins/docker-compose")
    plugin_stat = plugin_path.lstat()
    if (
        plugin_directory != str(plugin_path.parent)
        or not stat.S_ISREG(plugin_stat.st_mode)
        or plugin_stat.st_uid != 0
        or plugin_stat.st_gid != 0
        or stat.S_IMODE(plugin_stat.st_mode) != 0o755
    ):
        raise SystemExit(87)

CONTROL_PATH = Path("/volume4/.shunda-updater-fake")
if CONTROL_PATH.is_dir():
    SCENARIO = (CONTROL_PATH / "scenario").read_text(encoding="ascii")
    LOG_PATH = CONTROL_PATH / "docker.log"
    STATE_PATH = CONTROL_PATH / "docker-state.json"
    ENV_PATH = Path("/volume4/docker/docker/shunda-finance/app/.env")
    RAW = "Traceback private-token private-password sha256:" + "f" * 64 + " private-container-id /volume4/docker/docker/shunda-finance/app/.env daemon raw failure"
else:
    SCENARIO = os.environ["FAKE_SCENARIO"]
    LOG_PATH = Path(os.environ["FAKE_LOG"])
    STATE_PATH = Path(os.environ["FAKE_STATE"])
    ENV_PATH = Path(os.environ["FAKE_ENV_FILE"])
    RAW = os.environ["RAW_SENTINEL"]
REPOSITORY = "ghcr.io/s450586793/shunda-finance-updater"
NEW_REF = f"{REPOSITORY}:v1.2.4"
OLD_REF = f"{REPOSITORY}:v1.2.3"
NEW_ID = "sha256:" + "b" * 64
OLD_ID = "sha256:" + "a" * 64
DB_ID = "bad.container:id" if SCENARIO == "malformed_container_id" else "1" * 64
WEB_ID = "2" * 64
OLD_UPDATER_ID = "3" * 64
NEW_UPDATER_ID = "4" * 64
RECOVERY_VALIDATION_SCENARIOS = {
    "recovery_image_mismatch",
    "recovery_ref_mismatch",
    "recovery_project_mismatch",
    "recovery_service_mismatch",
    "recovery_unhealthy",
}
COMPOSE_PREFIX = [
    "compose",
    "--project-name", "shunda-finance",
    "--env-file", "/volume4/docker/docker/shunda-finance/app/.env",
    "-f", "/volume4/docker/docker/shunda-finance/app/compose.yml",
]


def load_state() -> dict[str, int]:
    if not STATE_PATH.exists():
        return {"up": 0}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(value: dict[str, int]) -> None:
    STATE_PATH.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(STATE_PATH, 0o600)


def log(action: str, service: str | None = None) -> None:
    payload = {"action": action}
    if service is not None:
        payload["service"] = service
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    os.chmod(LOG_PATH, 0o600)


def container_payload(container_id: str, up_count: int) -> dict[str, object]:
    if container_id == DB_ID:
        return {
            "Id": DB_ID,
            "Image": "sha256:" + "c" * 64,
            "State": {"Status": "running", "StartedAt": "2026-08-08T01:00:00Z"},
            "Config": {
                "Image": "postgres:16",
                "Labels": {
                    "com.docker.compose.project": "shunda-finance",
                    "com.docker.compose.service": "db",
                },
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": "/private/postgres",
                    "Destination": "/var/lib/postgresql/data",
                    "RW": True,
                }
            ],
        }
    if container_id == WEB_ID:
        started_at = "2026-08-08T01:01:00Z"
        if SCENARIO == "web_drift" and up_count >= 1:
            started_at = "2026-08-08T01:09:00Z"
        return {
            "Id": WEB_ID,
            "Image": "sha256:" + "d" * 64,
            "State": {"Status": "running", "StartedAt": started_at},
            "Config": {
                "Image": "ghcr.io/s450586793/shunda-finance-web:v1.2.3",
                "Labels": {
                    "com.docker.compose.project": "shunda-finance",
                    "com.docker.compose.service": "web",
                },
            },
            "Mounts": [
                {"Type": "bind", "Source": "/private/uploads", "Destination": "/data/uploads", "RW": True},
                {"Type": "bind", "Source": "/private/exports", "Destination": "/data/exports", "RW": True},
            ],
        }

    is_new = container_id == NEW_UPDATER_ID
    is_recovery = not is_new and up_count >= 2
    project = "shunda-finance"
    service = "updater"
    image_id = NEW_ID if is_new else OLD_ID
    image_ref = NEW_REF if is_new else OLD_REF
    if is_new and (
        SCENARIO in {"updater_identity_mismatch", "recovery_cleanup_rm_failure"}
        or SCENARIO in RECOVERY_VALIDATION_SCENARIOS
    ):
        project = "other-project"
    if is_recovery and SCENARIO == "recovery_project_mismatch":
        project = "other-project"
    if is_recovery and SCENARIO == "recovery_service_mismatch":
        service = "other-service"
    if is_recovery and SCENARIO == "recovery_image_mismatch":
        image_id = "sha256:" + "e" * 64
    if is_recovery and SCENARIO == "recovery_ref_mismatch":
        image_ref = "ghcr.io/s450586793/shunda-finance-updater:v9.9.9"
    health = "unhealthy" if SCENARIO in {"health_failure", "recovery_failure"} and is_new else "healthy"
    if is_recovery and SCENARIO == "recovery_unhealthy":
        health = "unhealthy"
    if SCENARIO == "signal" and is_new:
        health = "starting"
    return {
        "Id": container_id,
        "Image": image_id,
        "State": {
            "Status": "running",
            "StartedAt": "2026-08-08T02:00:00Z" if is_new else "2026-08-08T01:02:00Z",
            "Health": {"Status": health, "FailingStreak": 0, "Log": []},
        },
        "Config": {
            "Image": image_ref,
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": service,
            },
        },
        "Mounts": [
            {"Type": "bind", "Source": "/private/app", "Destination": "/config", "RW": True},
            {"Type": "bind", "Source": "/private/state", "Destination": "/state", "RW": True},
            {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True},
        ],
    }


args = sys.argv[1:]
if args[:2] != ["--host", "unix:///var/run/docker.sock"]:
    sys.stderr.write(RAW)
    raise SystemExit(96)
args = args[2:]
state = load_state()

if args == ["pull", NEW_REF]:
    log("pull")
    if SCENARIO == "pull_failure":
        sys.stderr.write(RAW)
        raise SystemExit(31)
    raise SystemExit(0)

if args == ["image", "inspect", NEW_REF]:
    log("image-inspect")
    if SCENARIO == "inspect_failure":
        sys.stderr.write(RAW)
        raise SystemExit(32)
    if SCENARIO == "env_race":
        ENV_PATH.unlink()
        ENV_PATH.write_bytes(b"SHUNDA_UPDATER_IMAGE_TAG=v9.9.9\nATTACK=must-remain\n")
        ENV_PATH.chmod(0o600)
    payload = [{
        "Id": NEW_ID,
        "RepoTags": [NEW_REF],
        "RepoDigests": [f"{REPOSITORY}@sha256:" + "e" * 64],
        "Created": "2026-08-08T00:00:00Z",
        "Config": {"Labels": {"org.opencontainers.image.version": "v1.2.4"}},
    }]
    if SCENARIO == "malformed_image_inspect":
        payload = [{"Id": "not-an-image-id", "RepoTags": [NEW_REF]}]
    sys.stdout.write(json.dumps(payload))
    raise SystemExit(0)

if args[: len(COMPOSE_PREFIX)] == COMPOSE_PREFIX:
    subcommand = args[len(COMPOSE_PREFIX):]
    if subcommand[:2] == ["ps", "-q"] and len(subcommand) == 3:
        service = subcommand[2]
        log("compose-ps", service)
        if service == "db":
            sys.stdout.write(DB_ID + "\n")
        elif service == "web":
            sys.stdout.write(WEB_ID + "\n")
        elif service == "updater":
            if SCENARIO == "eventual_readiness" and state["up"] == 1:
                state["readiness_ps"] = state.get("readiness_ps", 0) + 1
                save_state(state)
                if state["readiness_ps"] == 1:
                    raise SystemExit(0)
            sys.stdout.write((NEW_UPDATER_ID if state["up"] == 1 else OLD_UPDATER_ID) + "\n")
        else:
            sys.stderr.write(RAW)
            raise SystemExit(97)
        raise SystemExit(0)
    if subcommand == ["up", "-d", "--no-deps", "updater"]:
        log("compose-up", "updater")
        state["up"] += 1
        save_state(state)
        if SCENARIO == "recovery_failure" and state["up"] >= 2:
            sys.stderr.write(RAW)
            raise SystemExit(33)
        raise SystemExit(0)
    sys.stderr.write(RAW)
    raise SystemExit(98)

if args[:1] == ["inspect"] and len(args) == 2:
    container_id = args[1]
    role = {
        DB_ID: "db",
        WEB_ID: "web",
        OLD_UPDATER_ID: "updater",
        NEW_UPDATER_ID: "updater",
    }.get(container_id)
    if role is None:
        sys.stderr.write(RAW)
        raise SystemExit(99)
    log("container-inspect", role)
    if SCENARIO == "eventual_readiness" and container_id == NEW_UPDATER_ID and state["up"] == 1:
        state["readiness_inspect"] = state.get("readiness_inspect", 0) + 1
        save_state(state)
        if state["readiness_inspect"] == 1:
            sys.stderr.write(RAW)
            raise SystemExit(34)
    payload = container_payload(container_id, state["up"])
    if SCENARIO == "eventual_readiness" and container_id == NEW_UPDATER_ID and state["up"] == 1:
        if state["readiness_inspect"] == 2:
            payload["State"]["Status"] = "created"
        elif state["readiness_inspect"] == 3:
            payload["State"]["Health"]["Status"] = "starting"
    sys.stdout.write(json.dumps([payload]))
    raise SystemExit(0)

sys.stderr.write(RAW)
raise SystemExit(95)
PY
  chmod +x "$bin_dir/docker"

  cat >"$bin_dir/sleep" <<'PY'
#!/usr/bin/python3
import json
import os
import signal
from pathlib import Path

control_path = Path("/volume4/.shunda-updater-fake")
if control_path.is_dir():
    log_path = control_path / "docker.log"
    state_path = control_path / "docker-state.json"
    scenario = (control_path / "scenario").read_text(encoding="ascii")
else:
    log_path = Path(os.environ["FAKE_LOG"])
    state_path = Path(os.environ["FAKE_STATE"])
    scenario = os.environ["FAKE_SCENARIO"]
with log_path.open("a", encoding="utf-8") as handle:
    handle.write('{"action":"sleep"}\n')
os.chmod(log_path, 0o600)
state = json.loads(state_path.read_text(encoding="utf-8"))
if scenario == "signal" and not state.get("signal_sent"):
    state["signal_sent"] = 1
    state_path.write_text(json.dumps(state), encoding="utf-8")
    os.chmod(state_path, 0o600)
    candidate = os.getppid()
    while candidate > 1:
        command = Path(f"/proc/{candidate}/cmdline").read_bytes().split(b"\0")
        process_name = Path(f"/proc/{candidate}/comm").read_text(encoding="ascii").strip()
        if process_name == "bash" and any(value.endswith(b"/scripts/system-update-updater.sh") for value in command):
            os.kill(candidate, signal.SIGTERM)
            break
        status = Path(f"/proc/{candidate}/status").read_text(encoding="utf-8")
        candidate = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
    else:
        raise SystemExit(97)
PY
  chmod +x "$bin_dir/sleep"

  cat >"$bin_dir/mktemp" <<'PY'
#!/volume4/.shunda-test-bin/python3
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

control_path = Path("/volume4/.shunda-updater-fake")
scenario = (
    (control_path / "scenario").read_text(encoding="ascii")
    if (control_path / "scenario").is_file()
    else os.environ.get("FAKE_SCENARIO")
)
is_updater_scratch = (
    sys.argv[1:] == ["-d", "/var/tmp/shunda-updater-update.XXXXXX"]
)
if scenario != "lock_concurrency" or not is_updater_scratch:
    os.execv("/volume4/.shunda-test-bin/mktemp", ["/volume4/.shunda-test-bin/mktemp", *sys.argv[1:]])

ready_path = control_path / "lock-owner-ready"
release_path = control_path / "lock-owner-release"
second_call_path = control_path / "lock-second-mktemp-called"
owner_pid = None
candidate = os.getppid()
while candidate > 1:
    command = Path(f"/proc/{candidate}/cmdline").read_bytes().split(b"\0")
    process_name = Path(f"/proc/{candidate}/comm").read_text(encoding="ascii").strip()
    if process_name == "bash" and any(
        value.endswith(b"/scripts/system-update-updater.sh") for value in command
    ):
        owner_pid = candidate
        break
    status = Path(f"/proc/{candidate}/status").read_text(encoding="utf-8")
    candidate = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
if owner_pid is None:
    raise SystemExit(90)
try:
    descriptor = os.open(
        ready_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
except FileExistsError:
    descriptor = os.open(
        second_call_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    os.close(descriptor)
    os.execv("/volume4/.shunda-test-bin/mktemp", ["/volume4/.shunda-test-bin/mktemp", *sys.argv[1:]])
else:
    try:
        os.write(descriptor, str(owner_pid).encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

deadline = time.monotonic() + 15
while not release_path.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(89)
    time.sleep(0.01)
os.execv("/volume4/.shunda-test-bin/mktemp", ["/volume4/.shunda-test-bin/mktemp", *sys.argv[1:]])
PY
  chmod +x "$bin_dir/mktemp"

  cat >"$bin_dir/rm" <<'PY'
#!/volume4/.shunda-test-bin/python3
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

control_path = Path("/volume4/.shunda-updater-fake")
scenario = (
    (control_path / "scenario").read_text(encoding="ascii")
    if (control_path / "scenario").is_file()
    else os.environ.get("FAKE_SCENARIO")
)
is_scratch_cleanup = (
    len(sys.argv) == 4
    and sys.argv[1:3] == ["-rf", "--"]
    and Path(sys.argv[3]).parent == Path("/var/tmp")
    and Path(sys.argv[3]).name.startswith("shunda-updater-update.")
)
if scenario in {"success_cleanup_rm_failure", "recovery_cleanup_rm_failure"} and is_scratch_cleanup:
    raise SystemExit(88)

is_success_cleanup = (
    scenario == "success_window_signal"
    and len(sys.argv) == 4
    and sys.argv[1:3] == ["-rf", "--"]
    and Path(sys.argv[3]).parent == Path("/var/tmp")
    and Path(sys.argv[3]).name.startswith("shunda-updater-update.")
)
if not is_success_cleanup:
    os.execv("/volume4/.shunda-test-bin/rm", ["/volume4/.shunda-test-bin/rm", *sys.argv[1:]])

state_path = control_path / "docker-state.json"
state = json.loads(state_path.read_text(encoding="utf-8"))
state["commit_signal_sent"] = 1
state_path.write_text(json.dumps(state), encoding="utf-8")
os.chmod(state_path, 0o600)
with (control_path / "docker.log").open("a", encoding="ascii") as handle:
    handle.write('{"action":"commit-signal"}\n')
os.chmod(control_path / "docker.log", 0o600)
candidate = os.getppid()
while candidate > 1:
    command = Path(f"/proc/{candidate}/cmdline").read_bytes().split(b"\0")
    process_name = Path(f"/proc/{candidate}/comm").read_text(encoding="ascii").strip()
    if process_name == "bash" and any(value.endswith(b"/scripts/system-update-updater.sh") for value in command):
        os.kill(candidate, signal.SIGTERM)
        os.execv("/volume4/.shunda-test-bin/rm", ["/volume4/.shunda-test-bin/rm", *sys.argv[1:]])
    status = Path(f"/proc/{candidate}/status").read_text(encoding="utf-8")
    candidate = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
raise SystemExit(91)
PY
  chmod +x "$bin_dir/rm"
}

assert_no_public_leak() {
  local file="$1"
  local forbidden
  for forbidden in \
    private-token \
    private-password \
    must-remain \
    Traceback \
    'sha256:' \
    private-db-container-id \
    private-web-container-id \
    private-old-updater-container-id \
    private-new-updater-container-id \
    /volume4/docker/docker/shunda-finance/app \
    /run/shunda-system-update-updater.lock \
    /private/ \
    'daemon raw failure'; do
    if grep -Fq -- "$forbidden" "$file"; then
      fail_test "sensitive updater sentinel leaked"
    fi
  done
}

prepare_fake_control() {
  local scenario="$1"
  if [ -d /volume4/.shunda-updater-fake ]; then
    printf '%s' "$scenario" >/volume4/.shunda-updater-fake/scenario
    : >/volume4/.shunda-updater-fake/docker.log
    printf '{"up":0}' >/volume4/.shunda-updater-fake/docker-state.json
    /usr/bin/rm -f -- \
      /volume4/.shunda-updater-fake/lock-owner-ready \
      /volume4/.shunda-updater-fake/lock-owner-release \
      /volume4/.shunda-updater-fake/lock-second-mktemp-called
  fi
}

run_update_case() {
  local scenario="$1"
  local expected_status="$2"
  local invocation_mode="${3:-normal}"
  local case_dir status before_inode
  local -a command environment
  case_dir="$(mktemp -d "$suite_dir/update.XXXXXX")"
  mkdir -p "$case_dir/bin"
  write_fake_tools "$case_dir/bin"
  prepare_fake_control "$scenario"
  install_env_fixture "$suite_dir/original.env"
  before_inode="$(sudo -n stat -c '%d:%i' "$fixed_env_file")"
  environment=(
    "PATH=$case_dir/bin:$original_path"
    "SHUNDA_CONFIRM_UPDATER_UPDATE=yes"
    "SHUNDA_UPDATER_IMAGE_TAG=v1.2.4"
    "SHUNDA_UPDATER_TOKEN=private-token"
    "PASSWORD=private-password"
    "DOCKER_HOST="
    "DOCKER_CONTEXT="
    "DOCKER_TLS_VERIFY="
    "DOCKER_CERT_PATH="
    "FAKE_SCENARIO=$scenario"
    "FAKE_LOG=$case_dir/docker.log"
    "FAKE_STATE=$case_dir/docker-state.json"
    "FAKE_ENV_FILE=$fixed_env_file"
    "RAW_SENTINEL=Traceback private-token private-password sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff private-db-container-id /volume4/docker/docker/shunda-finance/app/.env daemon raw failure"
  )
  case "$invocation_mode" in
    normal) command=(sudo -n env "${environment[@]}" bash "$script_path") ;;
    xtrace) command=(sudo -n env "${environment[@]}" bash -x "$script_path") ;;
    *) fail_test "unknown updater invocation mode" ;;
  esac

  set +e
  "${command[@]}" >"$case_dir/stdout" 2>"$case_dir/stderr"
  status=$?
  set -e
  [ "$expected_status" = "any" ] || [ "$status" -eq "$expected_status" ] || {
    sed -n '1,100p' "$case_dir/stdout" >&2 || true
    sed -n '1,100p' "$case_dir/stderr" >&2 || true
    fail_test "$scenario exited $status, expected $expected_status"
  }

  LAST_CASE_DIR="$case_dir"
  LAST_STDOUT_FILE="$case_dir/stdout"
  LAST_STDERR_FILE="$case_dir/stderr"
  if [ -d /volume4/.shunda-updater-fake ]; then
    LAST_LOG_FILE="/volume4/.shunda-updater-fake/docker.log"
    LAST_STATE_FILE="/volume4/.shunda-updater-fake/docker-state.json"
  else
    LAST_LOG_FILE="$case_dir/docker.log"
    LAST_STATE_FILE="$case_dir/docker-state.json"
  fi
  LAST_BEFORE_INODE="$before_inode"
  LAST_STATUS="$status"
}

wait_for_private_path() {
  /volume4/.shunda-test-bin/python3 -I -S - "$1" <<'PY'
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
deadline = time.monotonic() + 10
while not path.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(1)
    time.sleep(0.01)
PY
}

prepare_lock_command() {
  local scenario="$1"
  local case_dir="$2"
  local -a environment
  mkdir -p "$case_dir/bin"
  write_fake_tools "$case_dir/bin"
  environment=(
    "PATH=$case_dir/bin:$original_path"
    "SHUNDA_CONFIRM_UPDATER_UPDATE=yes"
    "SHUNDA_UPDATER_IMAGE_TAG=v1.2.4"
    "SHUNDA_UPDATER_TOKEN=private-token"
    "PASSWORD=private-password"
    "DOCKER_HOST="
    "DOCKER_CONTEXT="
    "DOCKER_TLS_VERIFY="
    "DOCKER_CERT_PATH="
    "FAKE_SCENARIO=$scenario"
    "FAKE_LOG=$case_dir/docker.log"
    "FAKE_STATE=$case_dir/docker-state.json"
    "FAKE_ENV_FILE=$fixed_env_file"
    "RAW_SENTINEL=Traceback private-token private-password sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff private-db-container-id /volume4/docker/docker/shunda-finance/app/.env daemon raw failure"
  )
  LOCK_COMMAND=(sudo -n env "${environment[@]}" bash "$script_path")
}

run_lock_concurrency_case() {
  local case_dir first_pid first_status second_status before_inode
  local second_reached_mktemp=0
  local docker_before_release=0
  local scratch_before_release=""
  case_dir="$(mktemp -d "$suite_dir/lock-concurrency.XXXXXX")"
  prepare_fake_control lock_concurrency
  install_env_fixture "$suite_dir/original.env"
  before_inode="$(sudo -n stat -c '%d:%i' "$fixed_env_file")"
  prepare_lock_command lock_concurrency "$case_dir"

  set +e
  "${LOCK_COMMAND[@]}" >"$case_dir/first.stdout" 2>"$case_dir/first.stderr" &
  first_pid=$!
  set -e
  if ! wait_for_private_path /volume4/.shunda-updater-fake/lock-owner-ready; then
    : >/volume4/.shunda-updater-fake/lock-owner-release
    kill "$first_pid" >/dev/null 2>&1 || true
    set +e
    wait "$first_pid"
    set -e
    fail_test "concurrent lock owner did not reach the pre-scratch barrier"
  fi

  set +e
  "${LOCK_COMMAND[@]}" >"$case_dir/second.stdout" 2>"$case_dir/second.stderr"
  second_status=$?
  set -e
  [ ! -e /volume4/.shunda-updater-fake/lock-second-mktemp-called ] || second_reached_mktemp=1
  [ ! -s /volume4/.shunda-updater-fake/docker.log ] || docker_before_release=1
  scratch_before_release="$(find /var/tmp -mindepth 1 -maxdepth 1 -name 'shunda-updater-update.*' -print -quit)"
  : >/volume4/.shunda-updater-fake/lock-owner-release
  set +e
  wait "$first_pid"
  first_status=$?
  set -e

  [ "$second_status" -ne 0 ] || fail_test "concurrent second invocation exited 0, expected nonzero"
  [ "$first_status" -eq 0 ] || fail_test "concurrent lock owner exited $first_status, expected 0"
  [ "$second_reached_mktemp" -eq 0 ] || fail_test "concurrent second invocation reached mktemp"
  [ "$docker_before_release" -eq 0 ] || fail_test "concurrent second invocation reached Docker"
  [ -z "$scratch_before_release" ] || fail_test "concurrent invocation created scratch before lock rejection"
  [ "$(<"$case_dir/first.stdout")" = "updater update completed" ] || {
    fail_test "concurrent lock owner did not report one success"
  }
  [ ! -s "$case_dir/first.stderr" ] || fail_test "concurrent lock owner wrote stderr"
  [ ! -s "$case_dir/second.stdout" ] || fail_test "concurrent second invocation wrote stdout"
  [ "$(<"$case_dir/second.stderr")" = "updater update requires manual intervention" ] || {
    fail_test "concurrent second invocation did not return the fixed failure"
  }
  assert_no_public_leak "$case_dir/first.stdout"
  assert_no_public_leak "$case_dir/first.stderr"
  assert_no_public_leak "$case_dir/second.stdout"
  assert_no_public_leak "$case_dir/second.stderr"

  LAST_BEFORE_INODE="$before_inode"
  LAST_LOG_FILE="/volume4/.shunda-updater-fake/docker.log"
  LAST_STATE_FILE="/volume4/.shunda-updater-fake/docker-state.json"
  assert_target_env
  assert_success_order
  [ -f "$production_lock_file" ] && [ ! -L "$production_lock_file" ] || {
    fail_test "production updater lock is not a regular file"
  }
  [ "$(sudo -n stat -c '%u:%g:%a:%s' "$production_lock_file")" = "0:0:600:0" ] || {
    fail_test "production updater lock metadata is unsafe"
  }
}

assert_pretransaction_lock_rejection() {
  local scenario="$1"
  run_update_case "$scenario" 1
  assert_fixed_update_failure
  assert_no_public_leak "$LAST_STDOUT_FILE"
  assert_no_public_leak "$LAST_STDERR_FILE"
  [ ! -s "$LAST_LOG_FILE" ] || fail_test "$scenario reached Docker"
  [ -z "$(find /var/tmp -mindepth 1 -maxdepth 1 -name 'shunda-updater-update.*' -print -quit)" ] || {
    fail_test "$scenario created updater scratch"
  }
}

run_terminated_lock_owner() {
  local case_dir owner_pid owner_status updater_pid
  case_dir="$(mktemp -d "$suite_dir/lock-terminated.XXXXXX")"
  prepare_fake_control lock_concurrency
  install_env_fixture "$suite_dir/original.env"
  prepare_lock_command lock_concurrency "$case_dir"

  set +e
  "${LOCK_COMMAND[@]}" >"$case_dir/stdout" 2>"$case_dir/stderr" &
  owner_pid=$!
  set -e
  if ! wait_for_private_path /volume4/.shunda-updater-fake/lock-owner-ready; then
    kill "$owner_pid" >/dev/null 2>&1 || true
    set +e
    wait "$owner_pid" 2>/dev/null
    set -e
    fail_test "terminated lock owner did not reach the pre-scratch barrier"
  fi
  updater_pid="$(/usr/bin/sudo -n /usr/bin/cat /volume4/.shunda-updater-fake/lock-owner-ready)"
  [[ "$updater_pid" =~ ^[0-9]+$ ]] || fail_test "terminated lock owner PID is invalid"
  /usr/bin/sudo -n /usr/bin/kill -TERM "$updater_pid"
  : >/volume4/.shunda-updater-fake/lock-owner-release
  set +e
  wait "$owner_pid" 2>/dev/null
  owner_status=$?
  set -e
  [ "$owner_status" -ne 0 ] || fail_test "terminated lock owner exited 0"
  [ ! -s "$case_dir/stdout" ] || fail_test "terminated lock owner wrote stdout"
  [ "$(<"$case_dir/stderr")" = "updater update requires manual intervention" ] || {
    fail_test "terminated lock owner did not return the fixed failure"
  }
  assert_no_public_leak "$case_dir/stdout"
  assert_no_public_leak "$case_dir/stderr"
  [ ! -s /volume4/.shunda-updater-fake/docker.log ] || fail_test "terminated lock owner reached Docker"
  [ -z "$(find /var/tmp -mindepth 1 -maxdepth 1 -name 'shunda-updater-update.*' -print -quit)" ] || {
    fail_test "terminated lock owner created updater scratch"
  }
}

assert_fixed_update_failure() {
  [ ! -s "$LAST_STDOUT_FILE" ] || fail_test "failed updater update wrote stdout"
  [ "$(<"$LAST_STDERR_FILE")" = "updater update requires manual intervention" ] || {
    fail_test "updater failure output was not fixed and safe"
  }
}

assert_original_env() {
  sudo -n cmp -s "$fixed_env_file" "$suite_dir/original.env" || fail_test "original env bytes were not restored"
  [ "$(sudo -n stat -c '%a' "$fixed_env_file")" = "600" ] || fail_test "restored env mode is not 0600"
}

assert_target_env() {
  sudo -n cmp -s "$fixed_env_file" "$suite_dir/target.env" || fail_test "target env bytes are not exact"
  [ "$(sudo -n stat -c '%a' "$fixed_env_file")" = "600" ] || fail_test "target env mode is not 0600"
  [ "$(sudo -n stat -c '%d:%i' "$fixed_env_file")" != "$LAST_BEFORE_INODE" ] || fail_test "env replacement was not atomic"
  [ "$(sudo -n find "$fixed_app_dir" -maxdepth 1 -type f ! -name .env ! -name compose.yml | wc -l)" -eq 0 ] || {
    fail_test "atomic env temporary file was left behind"
  }
}

assert_symbolic_log_safe() {
  [ -f "$LAST_LOG_FILE" ] || fail_test "expected symbolic Docker log"
  assert_no_public_leak "$LAST_LOG_FILE"
  if grep -Eq '(^|[^a-z])(db|web)([^a-z]|$)' "$LAST_LOG_FILE"; then
    : # Fingerprint reads are expected; mutation checks are handled structurally below.
  fi
  python3 - "$LAST_LOG_FILE" <<'PY'
import json
import sys
from pathlib import Path

entries = [json.loads(line) for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()]
for entry in entries:
    if entry.get("action") == "compose-up" and entry != {"action": "compose-up", "service": "updater"}:
        raise SystemExit("non-updater service was rebuilt")
PY
}

assert_success_order() {
  local expected_commit_signal="${1:-no}"
  python3 - "$LAST_LOG_FILE" "$LAST_STATE_FILE" "$expected_commit_signal" <<'PY'
import json
import sys
from pathlib import Path

entries = [json.loads(line) for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()]
state = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
expected_commit_signal = sys.argv[3]
if expected_commit_signal == "yes":
    if not entries or entries.pop() != {"action": "commit-signal"}:
        raise SystemExit("success cleanup signal was not injected at the commit point")
    if state.get("commit_signal_sent") != 1:
        raise SystemExit("success cleanup signal marker was not recorded")
elif "commit_signal_sent" in state:
    raise SystemExit("unexpected success cleanup signal marker")
expected = [
    {"action": "pull"},
    {"action": "image-inspect"},
    {"action": "compose-ps", "service": "db"},
    {"action": "container-inspect", "service": "db"},
    {"action": "compose-ps", "service": "web"},
    {"action": "container-inspect", "service": "web"},
    {"action": "compose-ps", "service": "updater"},
    {"action": "container-inspect", "service": "updater"},
    {"action": "compose-up", "service": "updater"},
    {"action": "compose-ps", "service": "updater"},
    {"action": "container-inspect", "service": "updater"},
    {"action": "compose-ps", "service": "db"},
    {"action": "container-inspect", "service": "db"},
    {"action": "compose-ps", "service": "web"},
    {"action": "container-inspect", "service": "web"},
]
if entries != expected:
    raise SystemExit(f"unexpected updater success order: {entries!r}")
PY
}

assert_recovery_contract() {
  python3 - "$LAST_LOG_FILE" <<'PY'
import json
import sys
from pathlib import Path

entries = [json.loads(line) for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()]
up_calls = [entry for entry in entries if entry.get("action") == "compose-up"]
if up_calls != [
    {"action": "compose-up", "service": "updater"},
    {"action": "compose-up", "service": "updater"},
]:
    raise SystemExit("recovery did not rebuild only updater exactly once")
if not any(entry == {"action": "container-inspect", "service": "updater"} for entry in entries):
    raise SystemExit("recovery did not inspect updater identity and health")
for service in ("db", "web"):
    reads = [entry for entry in entries if entry == {"action": "container-inspect", "service": service}]
    if len(reads) < 2:
        raise SystemExit(f"recovery did not re-prove {service} fingerprint")
PY
}

assert_exact_recovery_validation_failure() {
  local scenario="$1"
  python3 - "$LAST_LOG_FILE" "$scenario" <<'PY'
import json
import sys
from pathlib import Path

entries = [json.loads(line) for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()]
scenario = sys.argv[2]
prefix = [
    {"action": "pull"},
    {"action": "image-inspect"},
    {"action": "compose-ps", "service": "db"},
    {"action": "container-inspect", "service": "db"},
    {"action": "compose-ps", "service": "web"},
    {"action": "container-inspect", "service": "web"},
    {"action": "compose-ps", "service": "updater"},
    {"action": "container-inspect", "service": "updater"},
    {"action": "compose-up", "service": "updater"},
    {"action": "compose-ps", "service": "updater"},
    {"action": "container-inspect", "service": "updater"},
    {"action": "updater-validation", "phase": "new", "status": 1},
    {"action": "compose-up", "service": "updater"},
]
suffix = [
    {"action": "compose-ps", "service": "db"},
    {"action": "container-inspect", "service": "db"},
    {"action": "compose-ps", "service": "web"},
    {"action": "container-inspect", "service": "web"},
]
if scenario == "recovery_unhealthy":
    recovery = []
    for attempt in range(30):
        recovery.extend([
            {"action": "compose-ps", "service": "updater"},
            {"action": "container-inspect", "service": "updater"},
            {"action": "updater-validation", "phase": "recovery", "status": 2},
        ])
        if attempt < 29:
            recovery.append({"action": "sleep"})
else:
    recovery = [
        {"action": "compose-ps", "service": "updater"},
        {"action": "container-inspect", "service": "updater"},
        {"action": "updater-validation", "phase": "recovery", "status": 1},
    ]
expected = prefix + recovery + suffix
if entries != expected:
    raise SystemExit(f"unexpected {scenario} recovery order: {entries!r}")
PY
}

assert_eventual_readiness_order() {
  python3 - "$LAST_LOG_FILE" <<'PY'
import json
import sys
from pathlib import Path

entries = [json.loads(line) for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()]
expected = [
    {"action": "pull"},
    {"action": "image-inspect"},
    {"action": "compose-ps", "service": "db"},
    {"action": "container-inspect", "service": "db"},
    {"action": "compose-ps", "service": "web"},
    {"action": "container-inspect", "service": "web"},
    {"action": "compose-ps", "service": "updater"},
    {"action": "container-inspect", "service": "updater"},
    {"action": "compose-up", "service": "updater"},
    {"action": "compose-ps", "service": "updater"},
    {"action": "sleep"},
    {"action": "compose-ps", "service": "updater"},
    {"action": "container-inspect", "service": "updater"},
    {"action": "sleep"},
    {"action": "compose-ps", "service": "updater"},
    {"action": "container-inspect", "service": "updater"},
    {"action": "sleep"},
    {"action": "compose-ps", "service": "updater"},
    {"action": "container-inspect", "service": "updater"},
    {"action": "sleep"},
    {"action": "compose-ps", "service": "updater"},
    {"action": "container-inspect", "service": "updater"},
    {"action": "compose-ps", "service": "db"},
    {"action": "container-inspect", "service": "db"},
    {"action": "compose-ps", "service": "web"},
    {"action": "container-inspect", "service": "web"},
]
if entries != expected:
    raise SystemExit(f"unexpected eventual readiness order: {entries!r}")
PY
}

assert_readiness_failure_order() {
  local scenario="$1"
  python3 - "$LAST_LOG_FILE" "$scenario" <<'PY'
import json
import sys
from pathlib import Path

entries = [json.loads(line) for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()]
scenario = sys.argv[2]
up_positions = [index for index, entry in enumerate(entries) if entry == {"action": "compose-up", "service": "updater"}]
if len(up_positions) != 2:
    raise SystemExit("readiness failure did not rebuild only new and old updater")
attempts = entries[up_positions[0] + 1:up_positions[1]]
if scenario == "updater_identity_mismatch":
    expected = [
        {"action": "compose-ps", "service": "updater"},
        {"action": "container-inspect", "service": "updater"},
    ]
else:
    expected = []
    for attempt in range(30):
        expected.extend([
            {"action": "compose-ps", "service": "updater"},
            {"action": "container-inspect", "service": "updater"},
        ])
        if attempt < 29:
            expected.append({"action": "sleep"})
if attempts != expected:
    raise SystemExit(f"unexpected {scenario} readiness attempts: {attempts!r}")
PY
}

run_ambient_trust_case() {
  local kind="$1"
  local case_dir docker_boundary plugin_sentinel status
  local -a command environment
  case_dir="$(mktemp -d "$suite_dir/ambient.XXXXXX")"
  mkdir -p \
    "$case_dir/trusted-bin" \
    "$case_dir/malicious-bin" \
    "$case_dir/pythonpath" \
    "$case_dir/ambient-home/.docker/cli-plugins" \
    "$case_dir/ambient-docker/cli-plugins" \
    "$case_dir/ambient-xdg/docker/cli-plugins"
  plugin_sentinel="$case_dir/home-plugin-called"
  write_fake_tools "$case_dir/trusted-bin"
  prepare_fake_control success
  cat >"$case_dir/malicious-bin/docker" <<'SH'
#!/bin/bash
set -euo pipefail
: >"${FAKE_AMBIENT_SENTINEL:?}"
exit 97
SH
  chmod +x "$case_dir/malicious-bin/docker"
  cat >"$case_dir/ambient-home/.docker/cli-plugins/docker-compose" <<'SH'
#!/bin/sh
: >"${SHUNDA_HOME_PLUGIN_SENTINEL:?}"
exit 97
SH
  cp \
    "$case_dir/ambient-home/.docker/cli-plugins/docker-compose" \
    "$case_dir/ambient-docker/cli-plugins/docker-compose"
  cp \
    "$case_dir/ambient-home/.docker/cli-plugins/docker-compose" \
    "$case_dir/ambient-xdg/docker/cli-plugins/docker-compose"
  chmod +x \
    "$case_dir/ambient-home/.docker/cli-plugins/docker-compose" \
    "$case_dir/ambient-docker/cli-plugins/docker-compose" \
    "$case_dir/ambient-xdg/docker/cli-plugins/docker-compose"
  cat >"$case_dir/pythonpath/sitecustomize.py" <<'PY'
import os
from pathlib import Path

Path(os.environ["FAKE_AMBIENT_SENTINEL"]).touch()
PY
  install_env_fixture "$suite_dir/original.env"
  environment=(
    "PATH=$case_dir/malicious-bin:$case_dir/trusted-bin:$original_path"
    "PYTHONPATH=$case_dir/pythonpath"
    "PYTHONHOME="
    "HOME=$case_dir/ambient-home"
    "XDG_CONFIG_HOME=$case_dir/ambient-xdg"
    "XDG_CACHE_HOME=$case_dir/ambient-xdg"
    "XDG_DATA_HOME=$case_dir/ambient-xdg"
    "SHUNDA_CONFIRM_UPDATER_UPDATE=yes"
    "SHUNDA_UPDATER_IMAGE_TAG=v1.2.4"
    "DOCKER_HOST="
    "DOCKER_CONTEXT="
    "DOCKER_TLS_VERIFY="
    "DOCKER_CERT_PATH="
    "FAKE_SCENARIO=success"
    "FAKE_LOG=$case_dir/docker.log"
    "FAKE_STATE=$case_dir/docker-state.json"
    "FAKE_ENV_FILE=$fixed_env_file"
    "FAKE_AMBIENT_SENTINEL=$case_dir/ambient-called"
    "SHUNDA_HOME_PLUGIN_SENTINEL=$plugin_sentinel"
    "RAW_SENTINEL=daemon raw failure"
  )
  command=(sudo -n env "${environment[@]}" bash "$script_path")
  set +e
  "${command[@]}" >"$case_dir/stdout" 2>"$case_dir/stderr"
  status=$?
  set -e
  [ ! -e "$case_dir/ambient-called" ] || fail_test "$kind ambient injection executed"
  [ ! -e "$plugin_sentinel" ] || fail_test "$kind HOME plugin executed"
  if [ "$status" -ne 0 ]; then
    docker_boundary="no Docker action"
    if [ -s /volume4/.shunda-updater-fake/docker.log ]; then
      docker_boundary="last Docker action: $(/usr/bin/tail -n 1 /volume4/.shunda-updater-fake/docker.log)"
    fi
    fail_test "$kind trusted execution exited $status ($docker_boundary): $(<"$case_dir/stderr")"
  fi
}

run_clean_bootstrap_case() {
  local case_dir status
  local bash_env_sentinel path_bash_sentinel docker_sentinel
  case_dir="$(mktemp -d "$suite_dir/bootstrap.XXXXXX")"
  mkdir -p "$case_dir/malicious-bin"
  bash_env_sentinel="$case_dir/bash-env-called"
  path_bash_sentinel="$case_dir/path-bash-called"
  docker_sentinel="$case_dir/docker-called"

  cat >"$case_dir/bash-env" <<'SH'
: >"${SHUNDA_BASH_ENV_SENTINEL:?}"
SH
  cat >"$case_dir/malicious-bin/bash" <<'SH'
#!/bin/sh
: >"${SHUNDA_PATH_BASH_SENTINEL:?}"
exit 97
SH
  cat >"$case_dir/malicious-bin/docker" <<'SH'
#!/bin/sh
: >"${SHUNDA_BOOTSTRAP_DOCKER_SENTINEL:?}"
exit 97
SH
  chmod +x "$case_dir/malicious-bin/bash" "$case_dir/malicious-bin/docker"

  [ ! -e /volume4 ] && [ ! -L /volume4 ] || {
    fail_test "clean bootstrap test requires host /volume4 to be absent"
  }
  set +e
  sudo -n /usr/bin/env -i \
    PATH="$case_dir/malicious-bin:/usr/bin:/bin" \
    BASH_ENV="$case_dir/bash-env" \
    SHUNDA_BASH_ENV_SENTINEL="$bash_env_sentinel" \
    SHUNDA_PATH_BASH_SENTINEL="$path_bash_sentinel" \
    SHUNDA_BOOTSTRAP_DOCKER_SENTINEL="$docker_sentinel" \
    "$script_path" \
    >"$case_dir/stdout" \
    2>"$case_dir/stderr"
  status=$?
  set -e

  [ "$status" -ne 0 ] || fail_test "clean bootstrap unexpectedly accepted missing confirmation"
  [ ! -e "$path_bash_sentinel" ] || fail_test "clean bootstrap executed PATH bash"
  [ ! -e "$bash_env_sentinel" ] || fail_test "clean bootstrap sourced BASH_ENV"
  [ ! -e "$docker_sentinel" ] || fail_test "clean bootstrap reached ambient Docker"
  [ ! -s "$case_dir/stdout" ] || fail_test "clean bootstrap wrote public stdout"
  [ "$(<"$case_dir/stderr")" = "updater update requires manual intervention" ] || {
    fail_test "clean bootstrap did not return the fixed safe failure message"
  }
  [ ! -e /volume4 ] && [ ! -L /volume4 ] || {
    fail_test "clean bootstrap created host /volume4"
  }
}

[ -f "$script_path" ] || fail_test "production updater script is missing"
[ -x "$script_path" ] || fail_test "production updater script is not executable"
[ "$EUID" -ne 0 ] || fail_test "contract must run from a non-root account"
/usr/bin/sudo -n true

if [ "${SHUNDA_TEST_BATCH:-}" = "BOOTSTRAP" ]; then
  run_clean_bootstrap_case
  printf 'system-update-updater clean bootstrap contract tests passed\n'
  exit 0
fi

if [ "${SHUNDA_TEST_NAMESPACE:-}" != "1" ]; then
  namespace_fake_dir="$suite_dir/namespace-fakes"
  mkdir -p "$namespace_fake_dir"
  write_fake_tools "$namespace_fake_dir"
  set +e
  "$project_root/scripts/system-update-updater-test-namespace.sh" \
    "$suite_dir" \
    "$namespace_fake_dir" \
    "$project_root" \
    "$(id -u)" \
    "$(id -g)" \
    "${SHUNDA_TEST_BATCH:-}"
  namespace_status=$?
  set -e
  exit "$namespace_status"
fi

if [ "${SHUNDA_TEST_BATCH:-}" = "ROOTFS" ]; then
  [ -x /usr/bin/docker ] && [ ! -L /usr/bin/docker ] || {
    fail_test "private rootfs did not install fake /usr/bin/docker"
  }
  [ -x /usr/local/bin/docker ] && [ ! -L /usr/local/bin/docker ] || {
    fail_test "private rootfs did not install fake /usr/local/bin/docker"
  }
  /usr/bin/cmp -s /usr/bin/docker /usr/local/bin/docker || {
    fail_test "private rootfs Docker candidates are not the same fake"
  }
  [ -f /var/run/docker.sock ] && [ ! -S /var/run/docker.sock ] || {
    fail_test "private rootfs did not mask docker.sock"
  }
  [ "$(/usr/bin/stat -c '%U:%G:%a' /var/run/docker.sock)" = "root:root:0" ] || {
    fail_test "private rootfs docker.sock mask is not root-owned mode 000"
  }
  printf 'system-update-updater private rootfs contract tests passed\n'
  exit 0
fi

[ ! -e "$fixed_root" ] && [ ! -L "$fixed_root" ] || {
  fail_test "contract refuses to replace a pre-existing fixed DSM fixture path"
}
sudo -n install -d -o root -g root -m 0700 /volume4/docker/docker
read -r \
  fixed_root_identity \
  fixed_app_identity \
  fixed_marker_identity \
  fixed_marker_value <<<"$(
  sudo -n /volume4/.shunda-test-bin/python3 -I -S - "$fixed_root" <<'PY'
import os
import secrets
import sys
from pathlib import Path

root_path = Path(sys.argv[1])
app_path = root_path / "app"
marker_path = root_path / ".shunda-updater-test-owner"
marker_value = secrets.token_hex(32).encode("ascii")
os.mkdir(root_path, 0o700)
os.mkdir(app_path, 0o700)
marker_descriptor = os.open(
    marker_path,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
    0o600,
)
try:
    os.write(marker_descriptor, marker_value)
    os.fsync(marker_descriptor)
    marker_stat = os.fstat(marker_descriptor)
finally:
    os.close(marker_descriptor)
root_stat = root_path.lstat()
app_stat = app_path.lstat()
for directory in (app_path, root_path):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
print(
    f"{root_stat.st_dev}:{root_stat.st_ino}",
    f"{app_stat.st_dev}:{app_stat.st_ino}",
    f"{marker_stat.st_dev}:{marker_stat.st_ino}",
    marker_value.decode("ascii"),
)
PY
)"
fixed_fixture_created=1

if [ "${SHUNDA_TEST_BATCH:-}" = "LOCK" ]; then
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.3\r\nOPAQUE=private-password' >"$suite_dir/original.env"
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.4\r\nOPAQUE=private-password' >"$suite_dir/target.env"
  printf 'services: {}\n' >"$suite_dir/compose.yml"
  sudo -n install -o root -g root -m 0644 "$suite_dir/compose.yml" "$fixed_app_dir/compose.yml"

  run_lock_concurrency_case
  lock_identity="$(sudo -n stat -c '%d:%i' "$production_lock_file")"
  run_update_case "success" 0
  assert_target_env
  assert_success_order
  [ "$(sudo -n stat -c '%d:%i' "$production_lock_file")" = "$lock_identity" ] || {
    fail_test "exited updater owner replaced the persistent lock"
  }

  /usr/bin/sudo -n /volume4/.shunda-test-bin/rm -f -- "$production_lock_file"
  lock_symlink_target="$suite_dir/lock-symlink-target"
  printf 'must-not-truncate' >"$lock_symlink_target"
  /usr/bin/sudo -n /usr/bin/ln -s "$lock_symlink_target" "$production_lock_file"
  assert_pretransaction_lock_rejection unsafe_lock_symlink
  [ "$(<"$lock_symlink_target")" = "must-not-truncate" ] || {
    fail_test "unsafe lock symlink target was changed"
  }
  [ -L "$production_lock_file" ] || fail_test "unsafe lock symlink was replaced"

  /usr/bin/sudo -n /volume4/.shunda-test-bin/rm -f -- "$production_lock_file"
  lock_payload="$suite_dir/lock-payload"
  printf 'must-not-truncate' >"$lock_payload"
  /usr/bin/sudo -n /usr/bin/install -o root -g root -m 0644 "$lock_payload" "$production_lock_file"
  assert_pretransaction_lock_rejection unsafe_lock_mode
  /usr/bin/sudo -n /usr/bin/cmp -s "$lock_payload" "$production_lock_file" || {
    fail_test "wrong-mode lock payload was changed"
  }
  [ "$(sudo -n stat -c '%u:%g:%a' "$production_lock_file")" = "0:0:644" ] || {
    fail_test "wrong-mode lock was modified"
  }

  /usr/bin/sudo -n /volume4/.shunda-test-bin/rm -f -- "$production_lock_file"
  /usr/bin/sudo -n /usr/bin/mkfifo -m 0600 "$production_lock_file"
  assert_pretransaction_lock_rejection unsafe_lock_fifo
  [ -p "$production_lock_file" ] || fail_test "non-regular lock object was replaced"

  /usr/bin/sudo -n /volume4/.shunda-test-bin/rm -f -- "$production_lock_file"
  run_terminated_lock_owner
  terminated_lock_identity="$(sudo -n stat -c '%d:%i' "$production_lock_file")"
  run_update_case "success" 0
  assert_target_env
  assert_success_order
  [ "$(sudo -n stat -c '%d:%i' "$production_lock_file")" = "$terminated_lock_identity" ] || {
    fail_test "terminated updater owner did not leave the reusable lock file"
  }
  printf 'system-update-updater production lock contract tests passed\n'
  exit 0
fi

if [ "${SHUNDA_TEST_BATCH:-}" = "RETENTION" ]; then
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.3\r\nOPAQUE=private-password' >"$suite_dir/original.env"
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.4\r\nOPAQUE=private-password' >"$suite_dir/target.env"
  printf 'services: {}\n' >"$suite_dir/compose.yml"
  sudo -n install -o root -g root -m 0644 "$suite_dir/compose.yml" "$fixed_app_dir/compose.yml"
  mapfile -t evidence_before < <(
    sudo -n find /var/tmp -mindepth 1 -maxdepth 1 -type d -name 'shunda-updater-update.*' -print
  )
  run_update_case "updater_identity_mismatch" 1
  assert_fixed_update_failure
  assert_original_env
  assert_recovery_contract
  mapfile -t evidence_after < <(
    sudo -n find /var/tmp -mindepth 1 -maxdepth 1 -type d -name 'shunda-updater-update.*' -print
  )
  [ "${#evidence_after[@]}" -eq "${#evidence_before[@]}" ] || {
    fail_test "successful recovery retained private evidence"
  }
  printf 'system-update-updater recovery retention contract tests passed\n'
  exit 0
fi

if [ "${SHUNDA_TEST_BATCH:-}" = "CLEANUP_RM" ]; then
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.3\r\nOPAQUE=private-password' >"$suite_dir/original.env"
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.4\r\nOPAQUE=private-password' >"$suite_dir/target.env"
  printf 'services: {}\n' >"$suite_dir/compose.yml"
  sudo -n install -o root -g root -m 0644 "$suite_dir/compose.yml" "$fixed_app_dir/compose.yml"

  for cleanup_scenario in recovery_cleanup_rm_failure success_cleanup_rm_failure; do
    mapfile -t evidence_before < <(
      sudo -n find /var/tmp -mindepth 1 -maxdepth 1 -type d -name 'shunda-updater-update.*' -printf '%f\n' | sort
    )
    run_update_case "$cleanup_scenario" 1
    assert_fixed_update_failure
    assert_symbolic_log_safe
    assert_no_public_leak "$LAST_STDOUT_FILE"
    assert_no_public_leak "$LAST_STDERR_FILE"
    if [ "$cleanup_scenario" = "success_cleanup_rm_failure" ]; then
      assert_target_env
      assert_success_order
    else
      assert_original_env
      assert_recovery_contract
    fi
    mapfile -t evidence_after < <(
      sudo -n find /var/tmp -mindepth 1 -maxdepth 1 -type d -name 'shunda-updater-update.*' -printf '%f\n' | sort
    )
    new_evidence=()
    for candidate in "${evidence_after[@]}"; do
      seen=0
      for previous in "${evidence_before[@]}"; do
        if [ "$candidate" = "$previous" ]; then
          seen=1
          break
        fi
      done
      [ "$seen" -eq 1 ] || new_evidence+=("$candidate")
    done
    [ "${#new_evidence[@]}" -eq 1 ] || fail_test "$cleanup_scenario did not retain one private evidence directory"
    [ "$(sudo -n stat -c '%u:%g:%a' "/var/tmp/${new_evidence[0]}")" = "0:0:700" ] || {
      fail_test "$cleanup_scenario evidence directory is not root-only"
    }
    [ -n "$(sudo -n find "/var/tmp/${new_evidence[0]}" -mindepth 1 -print -quit)" ] || {
      fail_test "$cleanup_scenario evidence directory is empty"
    }
    [ "$(sudo -n stat -c '%u:%g:%a' "/var/tmp/${new_evidence[0]}/cleanup-failed")" = "0:0:600" ] || {
      fail_test "$cleanup_scenario did not record root-only cleanup failure evidence"
    }
  done
  printf 'system-update-updater cleanup rm failure contract tests passed\n'
  exit 0
fi

if [ "${SHUNDA_TEST_BATCH:-}" = "TCB" ]; then
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.3\r\nOPAQUE=private-password' >"$suite_dir/original.env"
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.4\r\nOPAQUE=private-password' >"$suite_dir/target.env"
  printf 'services: {}\n' >"$suite_dir/compose.yml"
  sudo -n install -o root -g root -m 0644 "$suite_dir/compose.yml" "$fixed_app_dir/compose.yml"
  run_update_case "tcb_preflight" any
  [ ! -e /volume4/.shunda-updater-fake/untrusted-python-called ] || {
    fail_test "untrusted fallback Python executed before validation"
  }
  [ "$LAST_STATUS" -eq 0 ] || fail_test "trusted Python fallback did not complete"
  assert_target_env
  assert_success_order
  printf 'system-update-updater trusted candidate contract tests passed\n'
  exit 0
fi

if [ "${SHUNDA_TEST_BATCH:-}" = "DSM_TCB" ]; then
  [ -L /usr/bin/python3 ] || fail_test "DSM Python candidate is not a symlink"
  [ "$(/usr/bin/readlink /usr/bin/python3)" = "python3.8" ] || {
    fail_test "DSM Python candidate does not target python3.8"
  }
  [ ! -e /usr/bin/docker ] && [ ! -L /usr/bin/docker ] || {
    fail_test "DSM fixture unexpectedly installed /usr/bin/docker"
  }
  [ -L /usr/local/bin/docker ] || fail_test "DSM Docker candidate is not a symlink"
  [ "$(/usr/bin/readlink /usr/local/bin/docker)" = "/var/packages/ContainerManager/target/usr/bin/docker" ] || {
    fail_test "DSM Docker candidate target is not exact"
  }
  [ -L /var/packages/ContainerManager/target ] || {
    fail_test "DSM ContainerManager target is not a symlink"
  }
  [ "$(/usr/bin/readlink /var/packages/ContainerManager/target)" = "/volume4/@appstore/ContainerManager" ] || {
    fail_test "DSM ContainerManager target is not exact"
  }
  [ "$(/usr/bin/readlink -f /usr/local/bin/docker)" = "/volume4/@appstore/ContainerManager/usr/bin/docker" ] || {
    fail_test "DSM Docker candidate does not resolve to the package binary"
  }
  for trusted_path in \
    /usr/bin/python3 \
    /usr/bin/python3.8 \
    /usr/local/bin/docker \
    /var/packages/ContainerManager/target \
    /volume4/@appstore/ContainerManager/usr/bin/docker; do
    [ "$(/usr/bin/stat -c '%u' -- "$trusted_path")" = "0" ] || {
      fail_test "DSM candidate chain is not root-owned: $trusted_path"
    }
  done
  for trusted_directory in \
    /var/packages \
    /var/packages/ContainerManager \
    /volume4 \
    /volume4/@appstore \
    /volume4/@appstore/ContainerManager \
    /volume4/@appstore/ContainerManager/usr \
    /volume4/@appstore/ContainerManager/usr/bin; do
    [ "$(/usr/bin/stat -Lc '%u:%a' -- "$trusted_directory")" = "0:755" ] || {
      fail_test "DSM candidate parent is not root-owned mode 0755: $trusted_directory"
    }
  done

  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.3\r\nOPAQUE=private-password' >"$suite_dir/original.env"
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.4\r\nOPAQUE=private-password' >"$suite_dir/target.env"
  printf 'services: {}\n' >"$suite_dir/compose.yml"
  sudo -n install -o root -g root -m 0644 "$suite_dir/compose.yml" "$fixed_app_dir/compose.yml"
  run_update_case "tcb_preflight" 0
  assert_target_env
  assert_success_order
  printf 'system-update-updater DSM trusted candidate contract tests passed\n'
  exit 0
fi

if [ "${SHUNDA_TEST_BATCH:-}" = "A" ]; then
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.3\r\nOPAQUE=private-password' >"$suite_dir/original.env"
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.4\r\nOPAQUE=private-password' >"$suite_dir/target.env"
  printf 'services: {}\n' >"$suite_dir/compose.yml"
  sudo -n install -o root -g root -m 0644 "$suite_dir/compose.yml" "$fixed_app_dir/compose.yml"

  for durable_failure in parent_fsync_failure postcondition_failure next_stat_failure; do
    run_update_case "$durable_failure" 1
    assert_fixed_update_failure
    assert_original_env
    [ "$(grep -c 'compose-up' "$LAST_LOG_FILE")" -eq 1 ] || {
      fail_test "$durable_failure did not rebuild only the old updater"
    }
  done

  run_update_case "success_window_signal" 0
  [ "$(<"$LAST_STDOUT_FILE")" = "updater update completed" ] || {
    fail_test "commit-point signal did not preserve the committed success result"
  }
  [ ! -s "$LAST_STDERR_FILE" ] || fail_test "commit-point signal wrote stderr"
  assert_target_env
  assert_success_order yes
  printf 'system-update-updater batch A contract tests passed\n'
  exit 0
fi

if [ "${SHUNDA_TEST_BATCH:-}" = "B" ]; then
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.3\r\nOPAQUE=private-password' >"$suite_dir/original.env"
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.4\r\nOPAQUE=private-password' >"$suite_dir/target.env"
  printf 'services: {}\n' >"$suite_dir/compose.yml"
  sudo -n install -o root -g root -m 0644 "$suite_dir/compose.yml" "$fixed_app_dir/compose.yml"
  run_ambient_trust_case "PATH/PYTHONPATH"
  printf 'system-update-updater batch B contract tests passed\n'
  exit 0
fi

if [ "${SHUNDA_TEST_BATCH:-}" = "B_CLEANUP" ]; then
  owned_root="$fixed_root.owned"
  sudo -n mv "$fixed_root" "$owned_root"
  sudo -n install -d -o root -g root -m 0700 "$fixed_root"
  sudo -n touch "$fixed_root/replacement-sentinel"
  if cleanup_fixed_fixture; then
    fail_test "fixture cleanup accepted a replaced fixed path"
  fi
  replacement_survived=0
  if sudo -n test -f "$fixed_root/replacement-sentinel"; then
    replacement_survived=1
  fi
  sudo -n rm -rf -- "$fixed_root" "$owned_root"
  sudo -n rmdir /volume4/docker/docker /volume4/docker >/dev/null 2>&1 || true
  fixed_fixture_created=0
  [ "$replacement_survived" -eq 1 ] || fail_test "fixture cleanup deleted a replaced fixed path"
  printf 'system-update-updater batch B cleanup contract tests passed\n'
  exit 0
fi

if [ "${SHUNDA_TEST_BATCH:-}" = "C1" ]; then
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.3\r\nOPAQUE=private-password' >"$suite_dir/original.env"
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.4\r\nOPAQUE=private-password' >"$suite_dir/target.env"
  printf 'services: {}\n' >"$suite_dir/compose.yml"
  sudo -n install -o root -g root -m 0644 "$suite_dir/compose.yml" "$fixed_app_dir/compose.yml"

  run_update_case "postcondition_inode_replacement" 1
  assert_fixed_update_failure
  assert_original_env
  [ "$(grep -c 'compose-up' "$LAST_LOG_FILE")" -eq 1 ] || {
    fail_test "postcondition inode replacement did not rebuild only the old updater"
  }

  run_update_case "malformed_container_id" 1
  assert_fixed_update_failure
  assert_original_env
  if grep -Fq 'compose-up' "$LAST_LOG_FILE"; then
    fail_test "malformed container ID reached updater mutation"
  fi

  for ambient_name in \
    DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS DOCKER_TLS_VERIFY DOCKER_CERT_PATH \
    DOCKER_CONFIG DOCKER_API_VERSION DOCKER_CUSTOM_HEADERS DOCKER_DEFAULT_PLATFORM \
    DOCKER_CONTENT_TRUST DOCKER_CONTENT_TRUST_SERVER DOCKER_CLI_PLUGIN_EXTRA_DIRS \
    DOCKER_CLI_EXPERIMENTAL DOCKER_BUILDKIT DOCKER_CLI_HINTS \
    COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES \
    COMPOSE_PATH_SEPARATOR COMPOSE_CONVERT_WINDOWS_PATHS COMPOSE_ANSI \
    COMPOSE_STATUS_STDOUT COMPOSE_PARALLEL_LIMIT COMPOSE_IGNORE_ORPHANS \
    COMPOSE_REMOVE_ORPHANS COMPOSE_EXPERIMENTAL COMPOSE_MENU; do
    install_env_fixture "$suite_dir/original.env"
    run_rejected_case \
      "$ambient_name override" \
      root \
      SHUNDA_CONFIRM_UPDATER_UPDATE=yes \
      SHUNDA_UPDATER_IMAGE_TAG=v1.2.4 \
      "$ambient_name=private-override"
  done
  printf 'system-update-updater batch C1 contract tests passed\n'
  exit 0
fi

if [ "${SHUNDA_TEST_BATCH:-}" = "C2A" ]; then
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.3\r\nOPAQUE=private-password' >"$suite_dir/original.env"
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.4\r\nOPAQUE=private-password' >"$suite_dir/target.env"
  printf 'services: {}\n' >"$suite_dir/compose.yml"
  sudo -n install -o root -g root -m 0644 "$suite_dir/compose.yml" "$fixed_app_dir/compose.yml"

  run_update_case "web_drift" 1
  assert_fixed_update_failure
  assert_original_env
  mapfile -t evidence_dirs < <(
    sudo -n find /var/tmp -mindepth 1 -maxdepth 1 -type d -name 'shunda-updater-update.*' -print
  )
  [ "${#evidence_dirs[@]}" -eq 1 ] || fail_test "web drift did not retain one private evidence directory"
  if sudo -n cmp -s \
    "${evidence_dirs[0]}/baseline-web.fingerprint" \
    "${evidence_dirs[0]}/recovery-web.fingerprint"; then
    fail_test "web drift disappeared during recovery proof"
  fi

  for recovery_validation_scenario in \
    recovery_image_mismatch \
    recovery_ref_mismatch \
    recovery_project_mismatch \
    recovery_service_mismatch \
    recovery_unhealthy; do
    run_update_case "$recovery_validation_scenario" 1
    assert_fixed_update_failure
    assert_original_env
    assert_exact_recovery_validation_failure "$recovery_validation_scenario"
    assert_symbolic_log_safe
  done
  printf 'system-update-updater batch C2A contract tests passed\n'
  exit 0
fi

if [ "${SHUNDA_TEST_BATCH:-}" = "C2B" ]; then
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.3\r\nOPAQUE=private-password' >"$suite_dir/original.env"
  printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.4\r\nOPAQUE=private-password' >"$suite_dir/target.env"
  printf 'services: {}\n' >"$suite_dir/compose.yml"
  sudo -n install -o root -g root -m 0644 "$suite_dir/compose.yml" "$fixed_app_dir/compose.yml"

  run_update_case "eventual_readiness" 0
  [ "$(<"$LAST_STDOUT_FILE")" = "updater update completed" ] || fail_test "eventual readiness did not succeed"
  [ ! -s "$LAST_STDERR_FILE" ] || fail_test "eventual readiness wrote stderr"
  assert_target_env
  assert_eventual_readiness_order

  for readiness_failure in updater_identity_mismatch health_failure; do
    run_update_case "$readiness_failure" 1
    assert_fixed_update_failure
    assert_original_env
    assert_readiness_failure_order "$readiness_failure"
  done

  mapfile -t evidence_before < <(
    sudo -n find /var/tmp -mindepth 1 -maxdepth 1 -type d -name 'shunda-updater-update.*' -printf '%f\n' | sort
  )
  run_update_case "recovery_failure" 1
  assert_fixed_update_failure
  assert_original_env
  assert_recovery_contract
  mapfile -t evidence_after < <(
    sudo -n find /var/tmp -mindepth 1 -maxdepth 1 -type d -name 'shunda-updater-update.*' -printf '%f\n' | sort
  )
  new_evidence=()
  for candidate in "${evidence_after[@]}"; do
    seen=0
    for previous in "${evidence_before[@]}"; do
      if [ "$candidate" = "$previous" ]; then
        seen=1
        break
      fi
    done
    [ "$seen" -eq 1 ] || new_evidence+=("$candidate")
  done
  [ "${#new_evidence[@]}" -eq 1 ] || fail_test "recovery failure did not retain exactly one new evidence directory"
  sudo -n /volume4/.shunda-test-bin/python3 -I -S - \
    "/var/tmp/${new_evidence[0]}" <<'PY'
import os
import stat
import sys
from pathlib import Path

evidence_path = Path(sys.argv[1])
directory_stat = evidence_path.lstat()
if (
    not stat.S_ISDIR(directory_stat.st_mode)
    or directory_stat.st_uid != 0
    or directory_stat.st_gid != 0
    or stat.S_IMODE(directory_stat.st_mode) != 0o700
):
    raise SystemExit(1)
entries = list(evidence_path.iterdir())
if not entries:
    raise SystemExit(1)
expected_directories = {
    "home",
    "docker-config",
    "xdg-config",
    "xdg-cache",
    "xdg-data",
    "xdg-runtime",
}
seen_directories = set()
for entry in entries:
    value = entry.lstat()
    if stat.S_ISDIR(value.st_mode):
        if (
            entry.name not in expected_directories
            or value.st_uid != 0
            or value.st_gid != 0
            or stat.S_IMODE(value.st_mode) != 0o700
            or any(entry.iterdir())
        ):
            raise SystemExit(1)
        seen_directories.add(entry.name)
    elif not (
        stat.S_ISREG(value.st_mode)
        and value.st_uid == 0
        and value.st_gid == 0
        and stat.S_IMODE(value.st_mode) == 0o600
    ):
        raise SystemExit(1)
if seen_directories != expected_directories:
    raise SystemExit(1)
PY
  if grep -Fq '/var/tmp/' "$LAST_STDOUT_FILE" "$LAST_STDERR_FILE"; then
    fail_test "recovery evidence path was exposed publicly"
  fi
  printf 'system-update-updater batch C2B contract tests passed\n'
  exit 0
fi

run_rejected_case \
  "non-root execution" \
  direct \
  SHUNDA_CONFIRM_UPDATER_UPDATE=yes \
  SHUNDA_UPDATER_IMAGE_TAG=v1.2.3

run_rejected_case \
  "missing confirmation" \
  root \
  SHUNDA_CONFIRM_UPDATER_UPDATE= \
  SHUNDA_UPDATER_IMAGE_TAG=v1.2.3

run_rejected_case \
  "noncanonical tag" \
  root \
  SHUNDA_CONFIRM_UPDATER_UPDATE=yes \
  SHUNDA_UPDATER_IMAGE_TAG=v01.2.3

run_rejected_case \
  "missing fixed env file" \
  root \
  SHUNDA_CONFIRM_UPDATER_UPDATE=yes \
  SHUNDA_UPDATER_IMAGE_TAG=v1.2.3

printf 'OTHER=value\n' >"$suite_dir/missing-key.env"
install_env_fixture "$suite_dir/missing-key.env"
cp "$suite_dir/missing-key.env" "$suite_dir/expected.env"
run_rejected_case \
  "missing updater key" \
  root \
  SHUNDA_CONFIRM_UPDATER_UPDATE=yes \
  SHUNDA_UPDATER_IMAGE_TAG=v1.2.3
sudo -n cmp -s "$fixed_env_file" "$suite_dir/expected.env" || fail_test "missing-key env bytes changed"

printf 'SHUNDA_UPDATER_IMAGE_TAG=v1.2.2\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.3\n' >"$suite_dir/duplicate.env"
install_env_fixture "$suite_dir/duplicate.env"
cp "$suite_dir/duplicate.env" "$suite_dir/expected.env"
run_rejected_case \
  "duplicate updater key" \
  root \
  SHUNDA_CONFIRM_UPDATER_UPDATE=yes \
  SHUNDA_UPDATER_IMAGE_TAG=v1.2.3
sudo -n cmp -s "$fixed_env_file" "$suite_dir/expected.env" || fail_test "duplicate-key env bytes changed"

printf 'SHUNDA_UPDATER_IMAGE_TAG=v1.2.2\n' >"$suite_dir/valid.env"
install_env_fixture "$suite_dir/valid.env"
sudo -n chmod 0644 "$fixed_env_file"
run_rejected_case \
  "non-private env mode" \
  root \
  SHUNDA_CONFIRM_UPDATER_UPDATE=yes \
  SHUNDA_UPDATER_IMAGE_TAG=v1.2.3
sudo -n cmp -s "$fixed_env_file" "$suite_dir/valid.env" || fail_test "wrong-mode env bytes changed"

sudo -n rm -f "$fixed_env_file"
sudo -n install -o root -g root -m 0600 "$suite_dir/valid.env" "$fixed_app_dir/real.env"
sudo -n ln -s "$fixed_app_dir/real.env" "$fixed_env_file"
run_rejected_case \
  "symlink env file" \
  root \
  SHUNDA_CONFIRM_UPDATER_UPDATE=yes \
  SHUNDA_UPDATER_IMAGE_TAG=v1.2.3
sudo -n cmp -s "$fixed_app_dir/real.env" "$suite_dir/valid.env" || fail_test "symlink target bytes changed"

sudo -n rm -f "$fixed_env_file" "$fixed_app_dir/real.env"
sudo -n mkdir "$fixed_env_file"
run_rejected_case \
  "non-regular env file" \
  root \
  SHUNDA_CONFIRM_UPDATER_UPDATE=yes \
  SHUNDA_UPDATER_IMAGE_TAG=v1.2.3

for endpoint_override in DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH; do
  install_env_fixture "$suite_dir/valid.env"
  run_rejected_case \
    "$endpoint_override override" \
    root \
    SHUNDA_CONFIRM_UPDATER_UPDATE=yes \
    SHUNDA_UPDATER_IMAGE_TAG=v1.2.3 \
    "$endpoint_override=private-override"
done

for invalid_tag in v01.2.3 v1.02.3 v1.2.03 v1.2 v1.2.3.4 1.2.3 latest 'v1.2.3 '; do
  install_env_fixture "$suite_dir/valid.env"
  run_rejected_case \
    "invalid tag" \
    root \
    SHUNDA_CONFIRM_UPDATER_UPDATE=yes \
    "SHUNDA_UPDATER_IMAGE_TAG=$invalid_tag"
done

install_env_fixture "$suite_dir/valid.env"
sudo -n chown "$(id -u):$(id -g)" "$fixed_env_file"
run_rejected_case \
  "non-root-owned env file" \
  root \
  SHUNDA_CONFIRM_UPDATER_UPDATE=yes \
  SHUNDA_UPDATER_IMAGE_TAG=v1.2.3
sudo -n cmp -s "$fixed_env_file" "$suite_dir/valid.env" || fail_test "wrong-owner env bytes changed"

printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.3\r\nOPAQUE=private-password' >"$suite_dir/original.env"
printf '# Token=private-token\r\nSHUNDA_UPDATER_IMAGE_TAG=v1.2.4\r\nOPAQUE=private-password' >"$suite_dir/target.env"
printf 'services: {}\n' >"$suite_dir/compose.yml"
sudo -n install -o root -g root -m 0644 "$suite_dir/compose.yml" "$fixed_app_dir/compose.yml"

for pre_mutation_failure in pull_failure inspect_failure malformed_image_inspect; do
  run_update_case "$pre_mutation_failure" 1
  assert_fixed_update_failure
  assert_original_env
  assert_no_public_leak "$LAST_STDOUT_FILE"
  assert_no_public_leak "$LAST_STDERR_FILE"
  assert_symbolic_log_safe
  if grep -Fq 'compose-up' "$LAST_LOG_FILE"; then
    fail_test "$pre_mutation_failure rebuilt updater"
  fi
done

run_update_case "env_race" 1
assert_fixed_update_failure
printf 'SHUNDA_UPDATER_IMAGE_TAG=v9.9.9\nATTACK=must-remain\n' >"$suite_dir/raced.env"
sudo -n cmp -s "$fixed_env_file" "$suite_dir/raced.env" || fail_test "env race did not fail closed"
if grep -Fq 'compose-up' "$LAST_LOG_FILE"; then
  fail_test "env race rebuilt updater"
fi
assert_no_public_leak "$LAST_STDOUT_FILE"
assert_no_public_leak "$LAST_STDERR_FILE"
assert_symbolic_log_safe

run_update_case "success" 0
[ "$(<"$LAST_STDOUT_FILE")" = "updater update completed" ] || fail_test "unexpected updater success stdout"
[ ! -s "$LAST_STDERR_FILE" ] || fail_test "updater success wrote stderr"
assert_target_env
assert_success_order
assert_symbolic_log_safe
assert_no_public_leak "$LAST_STDOUT_FILE"
assert_no_public_leak "$LAST_STDERR_FILE"

for recovery_scenario in health_failure updater_identity_mismatch web_drift signal; do
  run_update_case "$recovery_scenario" 1
  assert_fixed_update_failure
  assert_original_env
  assert_recovery_contract
  assert_symbolic_log_safe
  assert_no_public_leak "$LAST_STDOUT_FILE"
  assert_no_public_leak "$LAST_STDERR_FILE"
done

run_update_case "recovery_failure" 1
assert_fixed_update_failure
assert_original_env
assert_symbolic_log_safe
assert_no_public_leak "$LAST_STDOUT_FILE"
assert_no_public_leak "$LAST_STDERR_FILE"
python3 - "$LAST_LOG_FILE" <<'PY'
import json
import sys
from pathlib import Path

entries = [json.loads(line) for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()]
up_calls = [entry for entry in entries if entry.get("action") == "compose-up"]
if up_calls != [
    {"action": "compose-up", "service": "updater"},
    {"action": "compose-up", "service": "updater"},
]:
    raise SystemExit("recovery failure did not attempt exact updater restoration")
PY

run_update_case "success" 0 xtrace
[ "$(<"$LAST_STDOUT_FILE")" = "updater update completed" ] || fail_test "unexpected xtrace success stdout"
assert_target_env
assert_success_order
assert_symbolic_log_safe
assert_no_public_leak "$LAST_STDOUT_FILE"
assert_no_public_leak "$LAST_STDERR_FILE"

printf 'system-update-updater contract tests passed\n'
