#!/usr/bin/env bash
set -euo pipefail

project_root="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)"
script_path="$project_root/scripts/system-update-manual-cleanup.sh"
readonly original_path="$PATH"
readonly docker_prefix="docker --host unix:///var/run/docker.sock"
readonly compose_prefix="$docker_prefix compose --project-name shunda-finance --env-file /volume4/docker/docker/shunda-finance/app/.env -f /volume4/docker/docker/shunda-finance/app/compose.yml"
readonly raw_sentinel="Traceback Token=private-token Cookie=private-cookie password=private-password sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa shunda-finance-rollback-web:11111111-1111-1111-1111-111111111111 /private/app"
suite_dir="$(mktemp -d)"

cleanup_suite() {
  local status=$?
  trap - EXIT
  sudo -n rm -rf -- "$suite_dir" >/dev/null 2>&1 || rm -rf -- "$suite_dir" >/dev/null 2>&1
  exit "$status"
}

trap cleanup_suite EXIT

fail_test() {
  printf '%s\n' "$1" >&2
  exit 1
}

run_case() {
  local scenario="$1"
  local expected_status="$2"
  local invocation_mode="${3:-normal}"
  local endpoint_override="${4:-}"
  local work_dir status
  local -a command environment
  local docker_host=""
  local docker_context=""
  local docker_tls_verify=""
  local docker_cert_path=""
  case "$endpoint_override" in
    "") ;;
    DOCKER_HOST) docker_host="tcp://private-docker-host:2376" ;;
    DOCKER_CONTEXT) docker_context="private-docker-context" ;;
    DOCKER_TLS_VERIFY) docker_tls_verify="1" ;;
    DOCKER_CERT_PATH) docker_cert_path="/private/docker-certs" ;;
    *) fail_test "unknown endpoint override: $endpoint_override" ;;
  esac
  work_dir="$(mktemp -d "$suite_dir/case.XXXXXX")"
  mkdir -p "$work_dir/bin"
  export FAKE_SCENARIO="$scenario"
  export FAKE_ID_LOG="$work_dir/id.log"
  export FAKE_LOG="$work_dir/docker.log"
  export FAKE_RESTARTED="$work_dir/restarted"
  export RAW_SENTINEL="$raw_sentinel"

  cat >"$work_dir/bin/id" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
[ "${1:-}" = "-u" ] || exit 98
: >"${FAKE_ID_LOG:?}"
printf '0\n'
SH
  chmod +x "$work_dir/bin/id"

  cat >"$work_dir/bin/docker" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "docker $*" >>"${FAKE_LOG:?}"
chmod 644 "${FAKE_LOG:?}"
[ "${1:-}" = "--host" ] && [ "${2:-}" = "unix:///var/run/docker.sock" ] || exit 95
compose_prefix="--host unix:///var/run/docker.sock compose --project-name shunda-finance --env-file /volume4/docker/docker/shunda-finance/app/.env -f /volume4/docker/docker/shunda-finance/app/compose.yml"
case "$*" in
  "$compose_prefix stop updater")
    if [ "${FAKE_SCENARIO:?}" = "stop_failure" ]; then
      printf '%s\n' "${RAW_SENTINEL:?}" >&2
      exit 31
    fi
    ;;
  "$compose_prefix ps -q updater")
    if [ "${FAKE_SCENARIO:?}" = "still_running" ] || [ -f "${FAKE_RESTARTED:?}" ]; then
      printf 'private-updater-container-id\n'
    fi
    ;;
  "$compose_prefix run --rm --no-deps --entrypoint python3 updater -m updater.manual_cleanup")
    if [ "${FAKE_SCENARIO:?}" = "cleanup_failure" ]; then
      printf '%s\n' "${RAW_SENTINEL:?}" >&2
      exit 32
    fi
    printf 'private cleanup stdout\n'
    ;;
  "$compose_prefix up -d --no-deps updater")
    if [ "${FAKE_SCENARIO:?}" = "restart_failure" ]; then
      printf '%s\n' "${RAW_SENTINEL:?}" >&2
      exit 33
    fi
    : >"${FAKE_RESTARTED:?}"
    ;;
  "--host unix:///var/run/docker.sock inspect -f {{.State.Health.Status}} private-updater-container-id")
    if [ "${FAKE_SCENARIO:?}" = "health_failure" ]; then
      printf '%s\n' "${RAW_SENTINEL:?}" >&2
      exit 34
    fi
    printf 'healthy\n'
    ;;
  *)
    printf '%s\n' "${RAW_SENTINEL:?}" >&2
    exit 97
    ;;
esac
SH
  chmod +x "$work_dir/bin/docker"

  cat >"$work_dir/bin/sleep" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
[ "${1:-}" = "2" ] || exit 96
SH
  chmod +x "$work_dir/bin/sleep"

  environment=(
    "PATH=$work_dir/bin:$original_path"
    "SHUNDA_CONFIRM_MANUAL_CLEANUP=${SHUNDA_CONFIRM_MANUAL_CLEANUP-yes}"
    "SHUNDA_APP_DIR=/browser/controlled/app"
    "SHUNDA_COMPOSE_PROJECT=browser-controlled-project"
    "COMPOSE_FILE=/browser/controlled/compose.yml"
    "SHUNDA_UPDATER_TOKEN=private-token"
    "HTTP_COOKIE=private-cookie"
    "PASSWORD=private-password"
    "DOCKER_HOST=$docker_host"
    "DOCKER_CONTEXT=$docker_context"
    "DOCKER_TLS_VERIFY=$docker_tls_verify"
    "DOCKER_CERT_PATH=$docker_cert_path"
    "FAKE_SCENARIO=$scenario"
    "FAKE_ID_LOG=$FAKE_ID_LOG"
    "FAKE_LOG=$FAKE_LOG"
    "FAKE_RESTARTED=$FAKE_RESTARTED"
    "RAW_SENTINEL=$RAW_SENTINEL"
  )
  case "$invocation_mode" in
    direct) command=(env "${environment[@]}" bash "$script_path") ;;
    normal) command=(sudo -n env "${environment[@]}" bash "$script_path") ;;
    xtrace) command=(sudo -n env "${environment[@]}" bash -x "$script_path") ;;
    *) fail_test "unknown invocation mode: $invocation_mode" ;;
  esac

  set +e
  "${command[@]}" >"$work_dir/stdout" 2>"$work_dir/stderr"
  status=$?
  set -e

  [ ! -e "$FAKE_ID_LOG" ] || {
    printf 'scenario %s executed id from PATH\n' "$scenario" >&2
    return 1
  }
  if [ "$status" -ne "$expected_status" ]; then
    printf 'scenario %s exited %s, expected %s\n' "$scenario" "$status" "$expected_status" >&2
    sed -n '1,120p' "$work_dir/stdout" >&2 || true
    sed -n '1,120p' "$work_dir/stderr" >&2 || true
    return 1
  fi

  LAST_CASE_DIR="$work_dir"
  LAST_STDOUT_FILE="$work_dir/stdout"
  LAST_STDERR_FILE="$work_dir/stderr"
  LAST_LOG_FILE="$FAKE_LOG"
}

assert_regular_success_output() {
  [ "$(<"$LAST_STDOUT_FILE")" = "cleanup completed" ] || fail_test "unexpected success stdout"
  [ ! -s "$LAST_STDERR_FILE" ] || fail_test "unexpected success stderr"
}

assert_fixed_failure_output() {
  [ ! -s "$LAST_STDOUT_FILE" ] || fail_test "failure must not write stdout"
  [ "$(<"$LAST_STDERR_FILE")" = "cleanup requires manual intervention" ] || {
    fail_test "failure must expose only the fixed manual-intervention message"
  }
}

assert_no_sentinel_leak() {
  local file="$1"
  local forbidden
  for forbidden in \
    Traceback \
    private-token \
    private-cookie \
    private-password \
    sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    shunda-finance-rollback-web:11111111-1111-1111-1111-111111111111 \
    /private/app \
    /browser/controlled/app \
    browser-controlled-project; do
    if grep -Fq -- "$forbidden" "$file"; then
      fail_test "sensitive sentinel leaked: $forbidden"
    fi
  done
}

assert_no_forbidden_docker_argv() {
  local call
  [ -f "$LAST_LOG_FILE" ] || return 0
  while IFS= read -r call; do
    case "$call" in
      "$docker_prefix "*) ;;
      *) fail_test "Docker call did not use the fixed local host option" ;;
    esac
    case " $call " in
      *" db "*|*" web "*) fail_test "cleanup mutated db or web" ;;
    esac
    case "$call" in
      *" image rm "*|*" image prune"*|*" --force "*) fail_test "cleanup used forbidden image deletion argv" ;;
    esac
  done <"$LAST_LOG_FILE"
}

assert_exact_success_order() {
  local -a expected calls
  expected=(
    "$compose_prefix stop updater"
    "$compose_prefix ps -q updater"
    "$compose_prefix run --rm --no-deps --entrypoint python3 updater -m updater.manual_cleanup"
    "$compose_prefix up -d --no-deps updater"
    "$compose_prefix ps -q updater"
    "$docker_prefix inspect -f {{.State.Health.Status}} private-updater-container-id"
  )
  mapfile -t calls <"$LAST_LOG_FILE"
  [ "${#calls[@]}" -eq "${#expected[@]}" ] || fail_test "unexpected Docker call count"
  local index
  for index in "${!expected[@]}"; do
    [ "${calls[$index]}" = "${expected[$index]}" ] || fail_test "unexpected Docker call order"
  done
}

test -x "$script_path"
test -x "$project_root/scripts/system-update-manual-cleanup.test.sh"
[ "$EUID" -ne 0 ] || fail_test "contract must run from a non-root account"
sudo -n true

SHUNDA_CONFIRM_MANUAL_CLEANUP=""
run_case "no_confirmation" 1
[ ! -e "$LAST_LOG_FILE" ] || [ ! -s "$LAST_LOG_FILE" ] || fail_test "confirmation failure reached Docker"
assert_fixed_failure_output
rm -rf "$LAST_CASE_DIR"
unset SHUNDA_CONFIRM_MANUAL_CLEANUP

run_case "non_root" 1 direct
[ ! -e "$LAST_LOG_FILE" ] || [ ! -s "$LAST_LOG_FILE" ] || fail_test "non-root failure reached Docker"
assert_fixed_failure_output
rm -rf "$LAST_CASE_DIR"

for endpoint_override in DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH; do
  run_case "endpoint_override" 1 normal "$endpoint_override"
  [ ! -e "$LAST_LOG_FILE" ] || [ ! -s "$LAST_LOG_FILE" ] || {
    fail_test "$endpoint_override failure reached Docker"
  }
  assert_fixed_failure_output
  rm -rf "$LAST_CASE_DIR"
done

run_case "success" 0
assert_regular_success_output
assert_exact_success_order
assert_no_forbidden_docker_argv
rm -rf "$LAST_CASE_DIR"

run_case "cleanup_failure" 1
assert_fixed_failure_output
assert_no_sentinel_leak "$LAST_STDOUT_FILE"
assert_no_sentinel_leak "$LAST_STDERR_FILE"
grep -Fq "$compose_prefix up -d --no-deps updater" "$LAST_LOG_FILE" || fail_test "cleanup failure did not restart updater"
grep -Fq "$docker_prefix inspect -f {{.State.Health.Status}} private-updater-container-id" "$LAST_LOG_FILE" || {
  fail_test "cleanup failure did not verify updater health"
}
assert_no_forbidden_docker_argv
rm -rf "$LAST_CASE_DIR"

run_case "still_running" 1
assert_fixed_failure_output
if grep -Fq "$compose_prefix run --rm --no-deps --entrypoint python3 updater -m updater.manual_cleanup" "$LAST_LOG_FILE"; then
  fail_test "cleanup ran while updater was still running"
fi
grep -Fq "$compose_prefix up -d --no-deps updater" "$LAST_LOG_FILE" || fail_test "stopped verification failure did not restart updater"
grep -Fq "$docker_prefix inspect -f {{.State.Health.Status}} private-updater-container-id" "$LAST_LOG_FILE" || {
  fail_test "stopped verification failure did not verify updater health"
}
assert_no_forbidden_docker_argv
rm -rf "$LAST_CASE_DIR"

for failure_scenario in stop_failure restart_failure health_failure; do
  run_case "$failure_scenario" 1
  assert_fixed_failure_output
  assert_no_sentinel_leak "$LAST_STDOUT_FILE"
  assert_no_sentinel_leak "$LAST_STDERR_FILE"
  grep -Fq "$compose_prefix up -d --no-deps updater" "$LAST_LOG_FILE" || fail_test "$failure_scenario did not attempt updater restart"
  assert_no_forbidden_docker_argv
  rm -rf "$LAST_CASE_DIR"
done

run_case "success_xtrace" 0 xtrace
[ "$(<"$LAST_STDOUT_FILE")" = "cleanup completed" ] || fail_test "unexpected xtrace success stdout"
assert_no_sentinel_leak "$LAST_STDOUT_FILE"
assert_no_sentinel_leak "$LAST_STDERR_FILE"
assert_exact_success_order
assert_no_forbidden_docker_argv
rm -rf "$LAST_CASE_DIR"

printf 'system-update-manual-cleanup contract tests passed\n'
