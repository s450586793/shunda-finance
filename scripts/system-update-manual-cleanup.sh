#!/usr/bin/env bash
set +x
set -euo pipefail

umask 077

readonly compose_project="shunda-finance"
readonly app_dir="/volume4/docker/docker/shunda-finance/app"
readonly env_file="$app_dir/.env"
readonly compose_file="$app_dir/compose.yml"
readonly docker_socket="unix:///var/run/docker.sock"
readonly success_message="cleanup completed"
readonly failure_message="cleanup requires manual intervention"
readonly health_attempts=30
readonly health_sleep_seconds=2

scratch_dir=""
restart_required=0
captured_output=""

compose() {
  docker --host "$docker_socket" compose \
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

capture_private() {
  local label="$1"
  shift
  captured_output=""
  if ! run_private "$label" "$@"; then
    return 1
  fi
  captured_output="$(<"$scratch_dir/$label.stdout")"
}

normalized_capture() {
  captured_output="${captured_output//$'\r'/}"
  captured_output="${captured_output//$'\n'/}"
}

wait_for_updater_health() {
  local attempt=0
  local updater_id=""
  local health_status=""
  while [ "$attempt" -lt "$health_attempts" ]; do
    if capture_private "updater-id" compose ps -q updater; then
      normalized_capture
      updater_id="$captured_output"
      if [ -n "$updater_id" ] && capture_private "updater-health" docker --host "$docker_socket" inspect -f '{{.State.Health.Status}}' "$updater_id"; then
        normalized_capture
        health_status="$captured_output"
        if [ "$health_status" = "healthy" ]; then
          return 0
        fi
      fi
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -lt "$health_attempts" ]; then
      run_private "health-sleep" sleep "$health_sleep_seconds" || return 1
    fi
  done
  return 1
}

restart_updater() {
  local restart_status=0
  run_private "updater-restart" compose up -d --no-deps updater || restart_status=1
  wait_for_updater_health || restart_status=1
  return "$restart_status"
}

on_signal() {
  exit 1
}

on_exit() {
  local operation_status=$?
  local restart_status=0
  trap - EXIT HUP INT TERM
  set +e
  if [ "$restart_required" -eq 1 ]; then
    restart_updater || restart_status=1
  fi
  if [ -n "$scratch_dir" ]; then
    rm -rf -- "$scratch_dir" >/dev/null 2>&1
  fi
  if [ "$operation_status" -eq 0 ] && [ "$restart_status" -eq 0 ]; then
    printf '%s\n' "$success_message"
    exit 0
  fi
  printf '%s\n' "$failure_message" >&2
  exit 1
}

trap on_exit EXIT
trap on_signal HUP INT TERM

scratch_dir="$(mktemp -d 2>/dev/null)" || exit 1
chmod 700 "$scratch_dir" 2>/dev/null || exit 1

[ "${SHUNDA_CONFIRM_MANUAL_CLEANUP:-}" = "yes" ] || exit 1
[ "$EUID" -eq 0 ] || exit 1
if [ -n "${DOCKER_HOST:-}" ] || \
  [ -n "${DOCKER_CONTEXT:-}" ] || \
  [ -n "${DOCKER_TLS_VERIFY:-}" ] || \
  [ -n "${DOCKER_CERT_PATH:-}" ]; then
  exit 1
fi

restart_required=1
run_private "updater-stop" compose stop updater || exit 1
capture_private "updater-stopped-check" compose ps -q updater || exit 1
normalized_capture
[ -z "$captured_output" ] || exit 1

run_private \
  "manual-cleanup" \
  compose run --rm --no-deps --entrypoint python3 updater -m updater.manual_cleanup || exit 1
