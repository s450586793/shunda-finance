#!/usr/bin/env bash
set -euo pipefail

project_root="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)"
script_path="$project_root/scripts/system-update-dsm-smoke.sh"
executed_case_count=0

run_filter_contract() {
  local contract_dir typo_status valid_status
  contract_dir="$(mktemp -d)"
  trap 'rm -rf "$contract_dir"' RETURN

  set +e
  env \
    SMOKE_TEST_FILTER_CONTRACT_CHILD=1 \
    SMOKE_TEST_SCENARIO=success_complet \
    bash "$project_root/scripts/system-update-dsm-smoke.test.sh" \
    >"$contract_dir/typo.stdout" \
    2>"$contract_dir/typo.stderr"
  typo_status=$?
  set -e
  [ "$typo_status" -ne 0 ] || {
    printf 'nonmatching smoke scenario filter exited 0\n' >&2
    return 1
  }
  [ ! -s "$contract_dir/typo.stdout" ] || {
    printf 'nonmatching smoke scenario filter wrote stdout\n' >&2
    return 1
  }
  [ "$(<"$contract_dir/typo.stderr")" = "SMOKE_TEST_SCENARIO did not match any smoke test scenario" ] || {
    printf 'nonmatching smoke scenario filter did not return fixed stderr\n' >&2
    return 1
  }

  set +e
  env \
    SMOKE_TEST_FILTER_CONTRACT_CHILD=1 \
    SMOKE_TEST_SCENARIO=success_complete \
    bash "$project_root/scripts/system-update-dsm-smoke.test.sh" \
    >"$contract_dir/valid.stdout" \
    2>"$contract_dir/valid.stderr"
  valid_status=$?
  set -e
  [ "$valid_status" -eq 0 ] || {
    printf 'valid smoke scenario filter exited %s\n' "$valid_status" >&2
    return 1
  }
  [ "$(<"$contract_dir/valid.stdout")" = "system-update-dsm-smoke contract tests passed" ] || {
    printf 'valid smoke scenario filter returned unexpected stdout\n' >&2
    return 1
  }
  [ ! -s "$contract_dir/valid.stderr" ] || {
    printf 'valid smoke scenario filter wrote stderr\n' >&2
    return 1
  }

  trap - RETURN
  rm -rf "$contract_dir"
}

run_case() {
  local scenario="$1"
  local expected_status="$2"
  local work_dir stdout_file stderr_file status
  if [ -n "${SMOKE_TEST_SCENARIO:-}" ] && [ "$scenario" != "$SMOKE_TEST_SCENARIO" ]; then
    CASE_SKIPPED=1
    LAST_CASE_DIR=""
    LAST_STDOUT_FILE="/dev/null"
    LAST_STDERR_FILE="/dev/null"
    LAST_LOG_FILE="/dev/null"
    return 0
  fi
  CASE_SKIPPED=0
  executed_case_count=$((executed_case_count + 1))
  work_dir="$(mktemp -d)"
  trap 'rm -rf "$work_dir"' RETURN

  mkdir -p "$work_dir/bin"
  export FAKE_SCENARIO="$scenario"
  export FAKE_LOG="$work_dir/fake-log.jsonl"
  export FAKE_CLOCK_FILE="$work_dir/fake-clock.txt"
  export FAKE_COUNTERS="$work_dir/fake-counters.json"
  export FAKE_REAL_PYTHON="/usr/bin/python3"
  printf '0' >"$FAKE_CLOCK_FILE"
  printf '{}' >"$FAKE_COUNTERS"
  export PATH="$work_dir/bin:$PATH"

  cat >"$work_dir/bin/python3" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [ "${FAKE_SCENARIO:-}" = "helper_failure" ] && [ "${1:-}" = "-" ] && [[ "${2:-}" == *.values ]]; then
  printf 'Traceback: private helper failure with masked token %s and html %s\n' \
    'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBbcdefghijklmnopqrstuvwxyzABCDEFG' \
    '<input type="hidden" name="csrfmiddlewaretoken" value="BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBbcdefghijklmnopqrstuvwxyzABCDEFG">' >&2
  exit 91
fi
exec "${FAKE_REAL_PYTHON:?}" "$@"
SH
  chmod +x "$work_dir/bin/python3"

  cat >"$work_dir/bin/curl" <<'PY'
#!/usr/bin/env python3
from __future__ import annotations
import json
import os
import re
import sys
from string import ascii_letters, digits
from pathlib import Path
from urllib.parse import parse_qs

SCENARIO = os.environ["FAKE_SCENARIO"]
LOG_PATH = Path(os.environ["FAKE_LOG"])
COUNTERS_PATH = Path(os.environ["FAKE_COUNTERS"])
CHARS = ascii_letters + digits
INITIAL_SECRET = "abcdefghijklmnopqrstuvwxyzABCDEF"
ROTATED_SECRET = "ghijklmnopqrstuvwxyzABCDEFGH0123"
INITIAL_MASK = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
SESSION_ONE = "session-before-login"
SESSION_TWO = "session-after-login"
DRIFT_TASK_ID = "22222222-2222-2222-2222-222222222222"
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def mask_secret(secret: str, mask: str) -> str:
    return mask + "".join(
        CHARS[(CHARS.index(secret_char) + CHARS.index(mask_char)) % len(CHARS)]
        for secret_char, mask_char in zip(secret, mask)
    )


INITIAL_MASKED = mask_secret(INITIAL_SECRET, INITIAL_MASK)


def load_counters() -> dict[str, object]:
    return json.loads(COUNTERS_PATH.read_text(encoding="utf-8"))


def save_counters(counters: dict[str, object]) -> None:
    COUNTERS_PATH.write_text(json.dumps(counters), encoding="utf-8")


def bump_counter(name: str) -> int:
    counters = load_counters()
    counters[name] = int(counters.get(name, 0)) + 1
    save_counters(counters)
    return int(counters[name])


def caller_task_id() -> str:
    task_id = str(load_counters().get("caller_task_id", ""))
    if not UUID_PATTERN.fullmatch(task_id):
        raise RuntimeError("caller task identity was not captured")
    return task_id


def append_log(payload: dict[str, object]) -> None:
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def parse_args(argv: list[str]) -> tuple[str, list[str]]:
    config_path = ""
    passthrough: list[str] = []
    index = 0
    while index < len(argv):
      value = argv[index]
      if value == "--config" and index + 1 < len(argv):
        config_path = argv[index + 1]
        index += 2
        continue
      passthrough.append(value)
      index += 1
    return config_path, passthrough


def parse_config(path: str) -> dict[str, object]:
    payload: dict[str, object] = {"headers": []}
    if not path:
        return payload
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line in {"fail", "silent", "show-error"}:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"')
        if key == "header":
            headers = payload.setdefault("headers", [])
            assert isinstance(headers, list)
            headers.append(value)
        else:
            payload[key] = value
    return payload


def write_cookie_jar(path: str, csrf_secret: str, session_token: str) -> None:
    Path(path).write_text(
        "# Netscape HTTP Cookie File\n"
        f"example.test\tFALSE\t/\tFALSE\t0\tcsrftoken\t{csrf_secret}\n"
        f"example.test\tFALSE\t/\tFALSE\t0\tsessionid\t{session_token}\n",
        encoding="utf-8",
    )
    counters = load_counters()
    counters["cookie_jar_path"] = path
    save_counters(counters)


def request_payload(config: dict[str, object]) -> str:
    body_path = str(config.get("data-binary", ""))
    if body_path.startswith("@"):
        body_path = body_path[1:]
    if not body_path:
        return ""
    return Path(body_path).read_text(encoding="utf-8")


def csrf_kind(config: dict[str, object]) -> str:
    for header in config.get("headers", []):
        if isinstance(header, str) and header.startswith("X-CSRFToken: "):
            token = header.split(": ", 1)[1]
            if token == ROTATED_SECRET:
                return "rotated-secret"
            if token == INITIAL_SECRET:
                return "initial-secret"
            if token == INITIAL_MASKED:
                return "initial-masked"
            return "other"
    return "absent"


def status_payload(index: int) -> dict[str, object]:
    task_id = caller_task_id()
    checking = {
        "id": task_id,
        "from_version": "v0.2.0",
        "to_version": "v0.2.1",
        "stage": "checking_health",
        "created_at": "2026-08-07T12:00:00+00:00",
        "started_at": "2026-08-07T12:00:01+00:00",
        "finished_at": None,
        "backup_complete": True,
        "rolled_back": False,
        "cleanup": "not_run",
        "error_code": "",
        "error_message": "",
    }
    if SCENARIO in {
        "success_complete",
        "success_pending",
        "success_complete_xtrace",
        "success_old_tag_config",
        "preexisting_target_replaced",
        "cleanup_public_complete_private_pending",
        "cleanup_public_pending_private_complete",
        "cleanup_original_tags_string",
        "cleanup_original_tags_non_string",
        "success_dangling_added",
        "cleanup_complete_dangling_missing",
        "cleanup_complete_dangling_retarget",
        "cleanup_complete_old_tag",
        "cleanup_complete_alias",
        "cleanup_complete_old_id",
        "cleanup_complete_unrelated_missing",
        "cleanup_complete_unrelated_retarget",
        "cleanup_complete_duplicate_old_ref",
        "cleanup_pending_missing_tag",
        "cleanup_pending_missing_alias",
        "cleanup_pending_tag_retarget",
        "cleanup_pending_alias_retarget",
        "cleanup_pending_old_id_missing",
        "cleanup_pending_duplicate_alias",
        "identity_drift",
        "empty_db_backup",
        "empty_uploads_backup",
        "state_mode_invalid",
        "raw_command_error",
        "malformed_inspect_json",
    }:
        sequence = [
            {
                "current_version": "v0.2.0",
                "latest_version": "v0.2.1",
                "latest_published_at": "2026-08-07T12:05:00+00:00",
                "update_available": True,
                "checked_at": "2026-08-07T12:00:00+00:00",
                "task": {**checking, "stage": "backing_up", "backup_complete": False},
            },
            {
                "current_version": "v0.2.0",
                "latest_version": "v0.2.1",
                "latest_published_at": "2026-08-07T12:05:00+00:00",
                "update_available": True,
                "checked_at": "2026-08-07T12:00:00+00:00",
                "task": checking,
            },
            {
                "current_version": "v0.2.1",
                "latest_version": "v0.2.1",
                "latest_published_at": "2026-08-07T12:05:00+00:00",
                "update_available": False,
                "checked_at": "2026-08-07T12:00:00+00:00",
                "task": {
                    **checking,
                    "stage": "succeeded",
                    "finished_at": "2026-08-07T12:00:21+00:00",
                    "cleanup": "pending" if SCENARIO in {"success_pending", "cleanup_public_pending_private_complete"} or SCENARIO.startswith("cleanup_pending_") else "complete",
                },
            },
        ]
    elif SCENARIO == "rollback":
        sequence = [
            {
                "current_version": "v0.2.0",
                "latest_version": "v0.2.1",
                "latest_published_at": "2026-08-07T12:05:00+00:00",
                "update_available": True,
                "checked_at": "2026-08-07T12:00:00+00:00",
                "task": checking,
            },
            {
                "current_version": "v0.2.0",
                "latest_version": "v0.2.1",
                "latest_published_at": "2026-08-07T12:05:00+00:00",
                "update_available": True,
                "checked_at": "2026-08-07T12:00:00+00:00",
                "task": checking,
            },
            {
                "current_version": "v0.2.0",
                "latest_version": "v0.2.1",
                "latest_published_at": "2026-08-07T12:05:00+00:00",
                "update_available": True,
                "checked_at": "2026-08-07T12:00:00+00:00",
                "task": {
                    **checking,
                    "stage": "failed",
                    "finished_at": "2026-08-07T12:00:13+00:00",
                    "rolled_back": True,
                    "error_code": "health_check_failed",
                    "error_message": "升级失败，请联系管理员。",
                },
            },
        ]
    elif SCENARIO == "status_race":
        sequence = [
            {
                "current_version": "v0.2.0",
                "latest_version": "v0.2.1",
                "latest_published_at": "2026-08-07T12:05:00+00:00",
                "update_available": True,
                "checked_at": "2026-08-07T12:00:00+00:00",
                "task": checking,
            },
            {
                "current_version": "v0.2.0",
                "latest_version": "v0.2.1",
                "latest_published_at": "2026-08-07T12:05:00+00:00",
                "update_available": True,
                "checked_at": "2026-08-07T12:00:00+00:00",
                "task": {**checking, "id": DRIFT_TASK_ID, "stage": "failed", "rolled_back": True, "finished_at": "2026-08-07T12:00:11+00:00"},
            },
        ]
    elif SCENARIO in {
        "image_drift",
        "image_id_drift",
        "oci_drift",
        "compose_project_drift",
        "compose_service_drift",
        "web_multiplicity",
    }:
        sequence = [
            {
                "current_version": "v0.2.0",
                "latest_version": "v0.2.1",
                "latest_published_at": "2026-08-07T12:05:00+00:00",
                "update_available": True,
                "checked_at": "2026-08-07T12:00:00+00:00",
                "task": checking,
            },
            {
                "current_version": "v0.2.0",
                "latest_version": "v0.2.1",
                "latest_published_at": "2026-08-07T12:05:00+00:00",
                "update_available": True,
                "checked_at": "2026-08-07T12:00:00+00:00",
                "task": checking,
            },
        ]
    elif SCENARIO == "failed_terminal":
        sequence = [
            {
                "current_version": "v0.2.0",
                "latest_version": "v0.2.1",
                "latest_published_at": "2026-08-07T12:05:00+00:00",
                "update_available": True,
                "checked_at": "2026-08-07T12:00:00+00:00",
                "task": {
                    **checking,
                    "stage": "failed",
                    "rolled_back": False,
                    "finished_at": "2026-08-07T12:00:08+00:00",
                    "error_code": "update_failed",
                    "error_message": "升级失败，请联系管理员。",
                },
            },
        ]
    elif SCENARIO == "manual_terminal":
        sequence = [
            {
                "current_version": "v0.2.0",
                "latest_version": "v0.2.1",
                "latest_published_at": "2026-08-07T12:05:00+00:00",
                "update_available": True,
                "checked_at": "2026-08-07T12:00:00+00:00",
                "task": {
                    **checking,
                    "stage": "manual_intervention",
                    "finished_at": "2026-08-07T12:00:08+00:00",
                    "error_code": "rollback_failed",
                    "error_message": "升级失败，需要人工处理。",
                },
            },
        ]
    elif SCENARIO in {"timeout", "signal_term"}:
        sequence = [
            {
                "current_version": "v0.2.0",
                "latest_version": "v0.2.1",
                "latest_published_at": "2026-08-07T12:05:00+00:00",
                "update_available": True,
                "checked_at": "2026-08-07T12:00:00+00:00",
                "task": checking,
            }
        ]
    else:
        sequence = []
    if not sequence:
        return {}
    return sequence[index] if index < len(sequence) else sequence[-1]


def main() -> int:
    config_path, passthrough = parse_args(sys.argv[1:])
    config = parse_config(config_path)
    url = str(config.get("url", passthrough[-1] if passthrough else ""))
    method = str(config.get("request", "GET"))
    csrf_source = csrf_kind(config)
    append_log(
        {
            "tool": "curl",
            "scenario": SCENARIO,
            "method": method,
            "url": url,
            "csrf_source": csrf_source,
            "uses_cookie_jar": bool(config.get("cookie-jar", "")),
        }
    )

    if url.endswith("/accounts/login/?next=/system/update/") and method == "GET":
        jar = str(config.get("cookie-jar", ""))
        if jar:
            write_cookie_jar(jar, INITIAL_SECRET, SESSION_ONE)
        sys.stdout.write(
            f'<input type="hidden" name="csrfmiddlewaretoken" value="{INITIAL_MASKED}">'
        )
        return 0

    if url.endswith("/accounts/login/?next=/system/update/") and method == "POST":
        body = parse_qs(request_payload(config), strict_parsing=True)
        if (
            body.get("password") != ["owner-password-safe-2026"]
            or body.get("csrfmiddlewaretoken") != [INITIAL_MASKED]
            or body.get("next") != ["/system/update/"]
        ):
            sys.stderr.write("private login failure with raw body")
            return 61
        jar = str(config.get("cookie-jar", ""))
        if jar:
            write_cookie_jar(jar, ROTATED_SECRET, SESSION_TWO)
        sys.stdout.write("logged-in")
        return 0

    if url.endswith("/system/update/check/"):
        if csrf_source != "rotated-secret":
            sys.stderr.write("private old csrf token")
            return 62
        payload = {
            "current_version": "v0.2.0",
            "latest_version": "v0.2.2" if SCENARIO == "target_mismatch" else "v0.2.1",
            "latest_published_at": "2026-08-07T12:05:00+00:00",
            "update_available": True,
            "checked_at": "2026-08-07T12:00:00+00:00",
            "task": None,
        }
        sys.stdout.write(json.dumps(payload))
        return 0

    if url.endswith("/system/update/start/"):
        if csrf_source != "rotated-secret":
            sys.stderr.write("private old csrf token on start")
            return 63
        if bump_counter("start_calls") != 1:
            sys.stderr.write("start called more than once")
            return 64
        body = json.loads(request_payload(config))
        if set(body) != {"target_version", "task_id"}:
            sys.stderr.write("start body must contain exact caller identity contract")
            return 65
        if body["target_version"] != "v0.2.1" or not isinstance(body["task_id"], str):
            sys.stderr.write("start body values are invalid")
            return 66
        if not UUID_PATTERN.fullmatch(body["task_id"]):
            sys.stderr.write("start caller task identity is not canonical")
            return 67
        counters = load_counters()
        counters["caller_task_id"] = body["task_id"]
        save_counters(counters)
        sys.stdout.write(
            json.dumps(
                {
                    "id": DRIFT_TASK_ID if SCENARIO == "start_id_mismatch" else body["task_id"],
                    "from_version": "v0.2.0",
                    "to_version": "v0.2.1",
                    "stage": "checking",
                    "created_at": "2026-08-07T12:00:00+00:00",
                    "started_at": "2026-08-07T12:00:01+00:00",
                    "finished_at": None,
                    "backup_complete": False,
                    "rolled_back": False,
                    "cleanup": "not_run",
                    "error_code": "",
                    "error_message": "",
                }
            )
        )
        return 0

    if url.endswith("/system/update/status/"):
        index = bump_counter("status_calls") - 1
        sys.stdout.write(json.dumps(status_payload(index)))
        return 0

    if url.endswith("/health/"):
        sys.stdout.write(json.dumps({"status": "ok"}))
        return 0

    sys.stderr.write("unexpected curl invocation")
    return 97


raise SystemExit(main())
PY
  chmod +x "$work_dir/bin/curl"

  cat >"$work_dir/bin/docker" <<'PY'
#!/usr/bin/env python3
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

SCENARIO = os.environ["FAKE_SCENARIO"]
LOG_PATH = Path(os.environ["FAKE_LOG"])
COUNTERS_PATH = Path(os.environ["FAKE_COUNTERS"])
WEB_REPOSITORY = "ghcr.io/s450586793/shunda-finance-web"
OLD_DIGEST = "sha256:" + "a" * 64
TARGET_DIGEST = "sha256:" + "b" * 64
OLD_IMAGE_ID = "sha256:" + "c" * 64
TARGET_IMAGE_ID = "sha256:" + "d" * 64
UNRELATED_WEB_ID = "sha256:" + "e" * 64
UNRELATED_DB_ID = "sha256:" + "f" * 64
STALE_TARGET_DIGEST = "sha256:" + "1" * 64
STALE_TARGET_IMAGE_ID = "sha256:" + "2" * 64
CAPTURED_DANGLING_ID = "sha256:" + "3" * 64
NEW_DANGLING_ID = "sha256:" + "4" * 64


def load_counters() -> dict[str, object]:
    return json.loads(COUNTERS_PATH.read_text(encoding="utf-8"))


def save_counters(counters: dict[str, object]) -> None:
    COUNTERS_PATH.write_text(json.dumps(counters), encoding="utf-8")


def bump_counter(name: str) -> int:
    counters = load_counters()
    counters[name] = int(counters.get(name, 0)) + 1
    save_counters(counters)
    return int(counters[name])


def caller_task_id() -> str:
    return str(load_counters().get("caller_task_id", ""))


def image_row(repository: str, tag: str, image_id: str, digest: str = "<none>") -> dict[str, str]:
    return {"Repository": repository, "Tag": tag, "ID": image_id, "Digest": digest}


def image_inventory(index: int) -> list[dict[str, str]]:
    task_id = caller_task_id()
    old_row = image_row(WEB_REPOSITORY, "v0.2.0", OLD_IMAGE_ID, OLD_DIGEST)
    alias_row = image_row("shunda-finance-rollback-web", task_id, OLD_IMAGE_ID)
    target_row = image_row(WEB_REPOSITORY, "v0.2.1", TARGET_IMAGE_ID, TARGET_DIGEST)
    unrelated_web = image_row(WEB_REPOSITORY, "v0.1.9", UNRELATED_WEB_ID)
    unrelated_db = image_row("postgres", "16", UNRELATED_DB_ID)
    if index == 1:
        captured_target_row = (
            image_row(WEB_REPOSITORY, "v0.2.1", STALE_TARGET_IMAGE_ID, STALE_TARGET_DIGEST)
            if SCENARIO == "preexisting_target_replaced"
            else target_row
        )
        rows = [old_row, captured_target_row, unrelated_web, unrelated_db]
        if SCENARIO in {
            "success_dangling_added",
            "cleanup_complete_dangling_missing",
            "cleanup_complete_dangling_retarget",
        }:
            rows.append(image_row("<none>", "<none>", CAPTURED_DANGLING_ID))
        return rows

    if SCENARIO.startswith("cleanup_pending_") or SCENARIO == "success_pending":
        rows = [old_row, alias_row, target_row, unrelated_web, unrelated_db]
        if SCENARIO == "cleanup_pending_missing_tag":
            rows.remove(old_row)
        elif SCENARIO == "cleanup_pending_missing_alias":
            rows.remove(alias_row)
        elif SCENARIO == "cleanup_pending_tag_retarget":
            rows[0] = image_row(WEB_REPOSITORY, "v0.2.0", TARGET_IMAGE_ID, OLD_DIGEST)
        elif SCENARIO == "cleanup_pending_alias_retarget":
            rows[1] = image_row("shunda-finance-rollback-web", task_id, TARGET_IMAGE_ID)
        elif SCENARIO == "cleanup_pending_old_id_missing":
            rows[0] = image_row(WEB_REPOSITORY, "v0.2.0", TARGET_IMAGE_ID, OLD_DIGEST)
            rows[1] = image_row("shunda-finance-rollback-web", task_id, TARGET_IMAGE_ID)
        elif SCENARIO == "cleanup_pending_duplicate_alias":
            rows.append(alias_row.copy())
        return rows

    rows = [target_row, unrelated_web, unrelated_db]
    if SCENARIO == "cleanup_complete_old_tag":
        rows.append(old_row)
    elif SCENARIO == "cleanup_complete_alias":
        rows.append(alias_row)
    elif SCENARIO == "cleanup_complete_old_id":
        rows.append(image_row("<none>", "<none>", OLD_IMAGE_ID))
    elif SCENARIO == "cleanup_complete_unrelated_missing":
        rows.remove(unrelated_web)
    elif SCENARIO == "cleanup_complete_unrelated_retarget":
        rows[1] = image_row(WEB_REPOSITORY, "v0.1.9", TARGET_IMAGE_ID)
    elif SCENARIO == "cleanup_complete_duplicate_old_ref":
        rows.extend([old_row, old_row.copy()])
    elif SCENARIO == "success_dangling_added":
        rows.extend(
            [
                image_row("<none>", "<none>", CAPTURED_DANGLING_ID),
                image_row("<none>", "<none>", NEW_DANGLING_ID),
            ]
        )
    elif SCENARIO == "cleanup_complete_dangling_retarget":
        rows.append(image_row("<none>", "<none>", NEW_DANGLING_ID))
    return rows


def append_log(payload: dict[str, object]) -> None:
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def log_docker_action(args: list[str]) -> None:
    if args[:1] == ["compose"]:
        sub = compose_subcommand(args)
        if sub[:2] == ["ps", "-q"] and len(sub) == 3:
            append_log({"tool": "docker", "action": "compose-ps", "service": sub[2]})
            return
        if sub[:3] == ["exec", "-T", "updater"]:
            append_log({"tool": "docker", "action": "updater-state"})
            return
        if sub[:3] == ["exec", "-T", "web"]:
            append_log({"tool": "docker", "action": "backup-proof"})
            return
    if args[:1] == ["inspect"]:
        role = "other"
        if len(args) == 2:
            if args[1].startswith("db-"):
                role = "db"
            elif args[1].startswith("updater-"):
                role = "updater"
            elif args[1].startswith("web-"):
                role = "web"
        append_log({"tool": "docker", "action": "inspect", "role": role})
        return
    if args[:2] == ["image", "ls"]:
        append_log({"tool": "docker", "action": "image-inventory"})
        return
    if args[:2] == ["image", "inspect"]:
        append_log({"tool": "docker", "action": "image-inspect"})
        return
    if args[:1] == ["stop"]:
        append_log({"tool": "docker", "action": "stop-web"})
        return
    append_log({"tool": "docker", "action": "unexpected"})


def container_payload(container_id: str) -> dict[str, object]:
    if container_id == "db-keep":
        started_at = "2026-08-07T10:01:00Z"
        if SCENARIO == "identity_drift" and bump_counter("inspect-db") >= 2:
            started_at = "2026-08-07T10:09:00Z"
        return {
            "Id": "db-keep",
            "Image": "db-image-ref",
            "State": {"StartedAt": started_at},
            "Config": {
                "Image": "postgres:16",
                "Labels": {
                    "com.docker.compose.project": "shunda-finance",
                    "com.docker.compose.service": "db",
                },
            },
            "Mounts": [{"Destination": "/var/lib/postgresql/data", "Name": "pg-volume"}],
        }
    if container_id == "updater-keep":
        started_at = "2026-08-07T10:01:30Z"
        if SCENARIO == "identity_drift" and bump_counter("inspect-updater") >= 2:
            started_at = "2026-08-07T10:09:30Z"
        return {
            "Id": "updater-keep",
            "Image": "updater-image-ref",
            "State": {"StartedAt": started_at},
            "Config": {
                "Image": "ghcr.io/s450586793/shunda-finance-updater:v0.2.0",
                "Labels": {
                    "com.docker.compose.project": "shunda-finance",
                    "com.docker.compose.service": "updater",
                },
            },
            "Mounts": [
                {"Destination": "/config", "Source": "/safe/app"},
                {"Destination": "/state", "Source": "/safe/state"},
            ],
        }
    if container_id == "web-old":
        old_config_image = (
            f"{WEB_REPOSITORY}:v0.2.0"
            if SCENARIO == "success_old_tag_config"
            else f"{WEB_REPOSITORY}@{OLD_DIGEST}"
        )
        return {
            "Id": "web-old",
            "Image": OLD_IMAGE_ID,
            "State": {"StartedAt": "2026-08-07T10:02:00Z"},
            "Config": {
                "Image": old_config_image,
                "Labels": {
                    "com.docker.compose.project": "shunda-finance",
                    "com.docker.compose.service": "web",
                    "org.opencontainers.image.version": "v0.2.0",
                },
            },
            "Mounts": [
                {"Destination": "/data/uploads", "Name": "uploads-volume"},
                {"Destination": "/data/exports", "Name": "exports-volume"},
            ],
        }
    config_image = f"{WEB_REPOSITORY}@{TARGET_DIGEST}"
    image_id = TARGET_IMAGE_ID
    oci_version = "v0.2.1"
    compose_project = "shunda-finance"
    compose_service = "web"
    if SCENARIO == "image_drift":
        config_image = f"{WEB_REPOSITORY}@{OLD_DIGEST}"
    elif SCENARIO == "image_id_drift":
        image_id = OLD_IMAGE_ID
    elif SCENARIO == "oci_drift":
        oci_version = "v0.2.0"
    elif SCENARIO == "compose_project_drift":
        compose_project = "other-project"
    elif SCENARIO == "compose_service_drift":
        compose_service = "other-service"
    return {
        "Id": "web-target",
        "Image": image_id,
        "State": {"StartedAt": "2026-08-07T12:00:03Z"},
        "Config": {
            "Image": config_image,
            "Labels": {
                "com.docker.compose.project": compose_project,
                "com.docker.compose.service": compose_service,
                "org.opencontainers.image.version": oci_version,
            },
        },
        "Mounts": [
            {"Destination": "/data/uploads", "Name": "uploads-volume"},
            {"Destination": "/data/exports", "Name": "exports-volume"},
        ],
    }


def state_payload() -> dict[str, object]:
    task_id = caller_task_id()
    original_tags: object = [f"{WEB_REPOSITORY}:v0.2.0"]
    if SCENARIO == "cleanup_original_tags_string":
        original_tags = f"{WEB_REPOSITORY}:v0.2.0"
    elif SCENARIO == "cleanup_original_tags_non_string":
        original_tags = [f"{WEB_REPOSITORY}:v0.2.0", 7]
    return {
        "last_check": None,
        "task": {
            "id": task_id,
            "original": {
                "repository": "ghcr.io/s450586793/shunda-finance-web",
                "version": "v0.2.0",
                "digest": OLD_DIGEST,
                "image_id": OLD_IMAGE_ID,
                "tags": original_tags,
                "rollback_alias": f"shunda-finance-rollback-web:{task_id}",
                "published_at": "2026-08-07T10:00:00+00:00",
            },
            "target": {
                "repository": "ghcr.io/s450586793/shunda-finance-web",
                "version": "v0.2.1",
                "digest": TARGET_DIGEST,
                "image_id": TARGET_IMAGE_ID,
                "tags": [f"{WEB_REPOSITORY}:v0.2.1"],
                "rollback_alias": "",
                "published_at": "2026-08-07T12:05:00+00:00",
            },
            "stage": "failed" if SCENARIO == "rollback" else "succeeded",
            "created_at": "2026-08-07T12:00:00+00:00",
            "started_at": "2026-08-07T12:00:01+00:00",
            "finished_at": "2026-08-07T12:00:21+00:00",
            "database_backup": "/data/backups/db-20260807-120000.dump",
            "uploads_backup": "/data/backups/uploads-20260807-120000.tar.gz",
            "rolled_back": SCENARIO == "rollback",
            "cleanup": "pending" if SCENARIO in {"success_pending", "cleanup_public_complete_private_pending"} or SCENARIO.startswith("cleanup_pending_") else "complete",
            "error_code": "health_check_failed" if SCENARIO == "rollback" else "",
            "error_message": "升级失败，请联系管理员。" if SCENARIO == "rollback" else "",
        },
    }


def compose_subcommand(args: list[str]) -> list[str]:
    for index, value in enumerate(args):
        if value in {"ps", "exec"}:
            return args[index:]
    return []


def main() -> int:
    args = sys.argv[1:]
    log_docker_action(args)
    if args[:1] == ["compose"]:
        sub = compose_subcommand(args)
        if sub[:3] == ["ps", "-q", "db"]:
            sys.stdout.write("db-keep\n")
            return 0
        if sub[:3] == ["ps", "-q", "updater"]:
            sys.stdout.write("updater-keep\n")
            return 0
        if sub[:3] == ["ps", "-q", "web"]:
            if int(load_counters().get("start_calls", 0)) == 0:
                sys.stdout.write("web-old\n")
            elif SCENARIO == "web_multiplicity":
                sys.stdout.write("web-target\nweb-shadow\n")
            else:
                sys.stdout.write("web-target\n")
            return 0
        if sub[:6] == ["exec", "-T", "updater", "stat", "-c", "%a"] and sub[6:] == ["/state/update-state.json"]:
            sys.stdout.write("644\n" if SCENARIO == "state_mode_invalid" else "600\n")
            return 0
        if sub[:4] == ["exec", "-T", "updater", "cat"] and sub[4:] == ["/state/update-state.json"]:
            sys.stdout.write(json.dumps(state_payload()))
            return 0
        if sub[:5] == ["exec", "-T", "web", "test", "-s"] and len(sub) == 6:
            target = sub[5]
            if SCENARIO == "empty_db_backup" and target.endswith(".dump"):
                return 1
            if SCENARIO == "empty_uploads_backup" and target.endswith(".tar.gz"):
                return 1
            return 0
    if args[:1] == ["inspect"] and len(args) == 2:
        if SCENARIO == "raw_command_error" and args[1] == "db-keep":
            sys.stderr.write("private digest image alias command failure")
            return 96
        if SCENARIO == "malformed_inspect_json" and args[1] == "db-keep":
            sys.stdout.write("{")
            return 0
        sys.stdout.write(json.dumps([container_payload(args[1])]))
        return 0
    if args == ["image", "ls", "--digests", "--no-trunc", "--format", "{{json .}}"]:
        index = bump_counter("inventory_calls")
        sys.stdout.write("\n".join(json.dumps(row) for row in image_inventory(index)))
        return 0
    if args[:2] == ["image", "inspect"] and len(args) == 3:
        if args[2] == "ghcr.io/s450586793/shunda-finance-web:v0.2.0":
            sys.stdout.write(json.dumps([{"Id": OLD_IMAGE_ID}]))
            return 0
        if args[2] == "ghcr.io/s450586793/shunda-finance-web:v0.2.1":
            sys.stdout.write(json.dumps([{"Id": TARGET_IMAGE_ID}]))
            return 0
    if args[:1] == ["stop"] and len(args) == 2 and args[1] == "web-target":
        return 0
    sys.stderr.write("unexpected docker invocation")
    return 97


raise SystemExit(main())
PY
  chmod +x "$work_dir/bin/docker"

  cat >"$work_dir/bin/jq" <<'PY'
#!/usr/bin/env python3
from __future__ import annotations
import json
import sys

args = sys.argv[1:]
expression = next(value for value in args if value.startswith("."))
path_expression, _, modifier = expression.partition("|")
payload = json.load(sys.stdin)
value = payload
for part in path_expression.strip().split(".")[1:]:
    if part == "":
        continue
    value = value[part]
if modifier.strip() == "tostring":
    if value is None:
        raise SystemExit(1)
    sys.stdout.write(str(value).lower() if isinstance(value, bool) else str(value))
    raise SystemExit(0)
if value in (None, "", []):
    raise SystemExit(1)
if isinstance(value, bool):
    sys.stdout.write("true" if value else "false")
else:
    sys.stdout.write(str(value))
PY
  chmod +x "$work_dir/bin/jq"

  cat >"$work_dir/bin/sleep" <<'PY'
#!/usr/bin/env python3
from __future__ import annotations
import json
import os
import signal
import sys
from pathlib import Path

scenario = os.environ["FAKE_SCENARIO"]
clock_path = Path(os.environ["FAKE_CLOCK_FILE"])
log_path = Path(os.environ["FAKE_LOG"])
seconds = int(float(sys.argv[1])) if len(sys.argv) > 1 else 0
current = int(clock_path.read_text(encoding="utf-8"))
if scenario == "signal_term":
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tool": "signal", "name": "TERM"}, ensure_ascii=True) + "\n")
    os.kill(os.getppid(), signal.SIGTERM)
    raise SystemExit(0)
if scenario == "timeout":
    current += 301
else:
    current += seconds
clock_path.write_text(str(current), encoding="utf-8")
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"tool": "sleep", "scenario": scenario, "seconds": seconds}, ensure_ascii=True) + "\n")
PY
  chmod +x "$work_dir/bin/sleep"

  cat >"$work_dir/bin/date" <<'PY'
#!/usr/bin/env python3
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

clock_path = Path(os.environ["FAKE_CLOCK_FILE"])
log_path = Path(os.environ["FAKE_LOG"])
if sys.argv[1:] != ["+%s"]:
    sys.stderr.write("unexpected date invocation")
    raise SystemExit(98)
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"tool": "date", "args": sys.argv[1:]}, ensure_ascii=True) + "\n")
sys.stdout.write(clock_path.read_text(encoding="utf-8"))
PY
  chmod +x "$work_dir/bin/date"

  cat >"$work_dir/bin/mktemp" <<'PY'
#!/usr/bin/python3
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

scenario = os.environ["FAKE_SCENARIO"]
counters_path = Path(os.environ["FAKE_COUNTERS"])
counters = json.loads(counters_path.read_text(encoding="utf-8"))
index = int(counters.get("mktemp_calls", 0)) + 1
args = sys.argv[1:]
effective_args = [] if scenario == "private_create_failure" and index == 1 else args
result = subprocess.run(["/usr/bin/mktemp", *effective_args], check=True, capture_output=True, text=True)
path = result.stdout.strip()
counters["mktemp_calls"] = index
counters["scratch_path" if index == 1 else "private_cookie_path"] = path
counters_path.write_text(json.dumps(counters), encoding="utf-8")
sys.stdout.write(path + "\n")
PY
  /usr/bin/chmod +x "$work_dir/bin/mktemp"

  cat >"$work_dir/bin/chmod" <<'PY'
#!/usr/bin/python3
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

scenario = os.environ["FAKE_SCENARIO"]
counters = json.loads(Path(os.environ["FAKE_COUNTERS"]).read_text(encoding="utf-8"))
target = sys.argv[-1]
if scenario == "private_chmod_failure" and target == counters.get("private_cookie_path"):
    sys.stderr.write(f"chmod: cannot access '{target}': raw permission denied\n")
    raise SystemExit(88)
if scenario == "private_write_failure" and target.endswith("/caller-task-id"):
    path = Path(target)
    path.unlink()
    path.mkdir()
    raise SystemExit(0)
raise SystemExit(subprocess.run(["/usr/bin/chmod", *sys.argv[1:]]).returncode)
PY
  /usr/bin/chmod +x "$work_dir/bin/chmod"

  stdout_file="$work_dir/stdout.txt"
  stderr_file="$work_dir/stderr.txt"

  local -a shell_cmd
  if [ "${RUN_WITH_XTRACE-}" = "yes" ]; then
    shell_cmd=(bash -x "$script_path")
  else
    shell_cmd=(bash "$script_path")
  fi

  set +e
  env \
    SHUNDA_BASE_URL="${SHUNDA_BASE_URL-http://example.test:1111}" \
    SHUNDA_APP_DIR="${SHUNDA_APP_DIR-/safe/app}" \
    SHUNDA_OWNER_USERNAME="${SHUNDA_OWNER_USERNAME-owner-safe}" \
    SHUNDA_OWNER_PASSWORD="${SHUNDA_OWNER_PASSWORD-owner-password-safe-2026}" \
    SHUNDA_EXPECTED_TARGET="${SHUNDA_EXPECTED_TARGET-v0.2.1}" \
    SHUNDA_CONFIRM_SYSTEM_UPDATE="${SHUNDA_CONFIRM_SYSTEM_UPDATE-yes}" \
    SHUNDA_CONFIRM_ROLLBACK_SMOKE="${SHUNDA_CONFIRM_ROLLBACK_SMOKE-}" \
    SHUNDA_SMOKE_MODE="${SHUNDA_SMOKE_MODE-success}" \
    "${shell_cmd[@]}" >"$stdout_file" 2>"$stderr_file"
  status=$?
  set -e

  if ! assert_no_private_output "$work_dir" "$stdout_file" "$stderr_file" "$FAKE_LOG" "$FAKE_COUNTERS"; then
    printf 'scenario %s secrecy gate failed\n' "$scenario" >&2
    cat "$work_dir/secrecy-gate.stderr" >&2 || true
    return 1
  fi

  if [ "$status" -ne "$expected_status" ]; then
    echo "scenario $scenario exited $status, expected $expected_status" >&2
    cat "$stdout_file" >&2 || true
    cat "$stderr_file" >&2 || true
    return 1
  fi

  LAST_CASE_DIR="$work_dir"
  LAST_STDOUT_FILE="$stdout_file"
  LAST_STDERR_FILE="$stderr_file"
  LAST_LOG_FILE="$FAKE_LOG"
  trap - RETURN
}

assert_no_private_output() {
  local work_dir="$1"
  local stdout_file="$2"
  local stderr_file="$3"
  local log_file="$4"
  local counters_file="$5"
  local private_stderr="$work_dir/secrecy-gate.stderr"
  python3 - "$work_dir" "$stdout_file" "$stderr_file" "$log_file" "$counters_file" \
    2>"$private_stderr" <<'PY'
import json
import re
import sys
from pathlib import Path

work_dir = sys.argv[1]
stdout_path, stderr_path, log_path, counters_path = map(Path, sys.argv[2:6])
counters = json.loads(counters_path.read_text(encoding="utf-8"))
texts = [stdout_path.read_text(encoding="utf-8"), stderr_path.read_text(encoding="utf-8")]
if log_path.exists():
    texts.append(log_path.read_text(encoding="utf-8"))
combined = "\n".join(texts)
task_id = str(counters.get("caller_task_id", ""))
cookie_path = str(counters.get("cookie_jar_path", ""))
scratch_path = str(counters.get("scratch_path", ""))
private_cookie_path = str(counters.get("private_cookie_path", ""))
forbidden = (
    "owner-password-safe-2026",
    "abcdefghijklmnopqrstuvwxyzABCDEF",
    "ghijklmnopqrstuvwxyzABCDEFGH0123",
    "session-before-login",
    "session-after-login",
    "private old csrf token",
    "private digest image alias command failure",
    "Traceback",
    "JSONDecodeError",
    "shunda-finance-rollback-web:",
    "/safe/app",
    "db-keep",
    "updater-keep",
    "web-old",
    "web-target",
    work_dir,
    cookie_path,
    scratch_path,
    private_cookie_path,
    task_id,
)
for index, value in enumerate(forbidden):
    if value and value in combined:
        sys.stderr.write(f"content-{index}\n")
        raise SystemExit(1)
if re.search(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", combined):
    sys.stderr.write("uuid\n")
    raise SystemExit(1)
if re.search(r"sha256:[0-9a-f]{64}", combined):
    sys.stderr.write("digest\n")
    raise SystemExit(1)
if scratch_path and Path(scratch_path).exists():
    sys.stderr.write("scratch-exists\n")
    raise SystemExit(1)
if private_cookie_path and Path(private_cookie_path).exists():
    sys.stderr.write("cookie-exists\n")
    raise SystemExit(1)
PY
}

assert_contains() {
  [ "${CASE_SKIPPED:-0}" -eq 0 ] || return 0
  local file="$1"
  local needle="$2"
  if ! grep -Fq "$needle" "$file"; then
    echo "missing expected text: $needle" >&2
    cat "$file" >&2 || true
    exit 1
  fi
}

assert_not_contains() {
  [ "${CASE_SKIPPED:-0}" -eq 0 ] || return 0
  local file="$1"
  local needle="$2"
  if grep -Fq "$needle" "$file"; then
    echo "found forbidden text: $needle" >&2
    cat "$file" >&2 || true
    exit 1
  fi
}

assert_python() {
  [ "${CASE_SKIPPED:-0}" -eq 0 ] || return 0
  local assertion_scenario="$1"
  local assertion_stderr="$LAST_CASE_DIR/private-assertion.stderr"
  export LAST_CASE_DIR
  if ! python3 - "$@" 2>"$assertion_stderr" <<'PY'
import json
import os
import sys
from string import ascii_letters, digits
from pathlib import Path

log_path = Path(os.environ["LAST_LOG_FILE"])
entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()] if log_path.exists() else []
stdout_text = Path(os.environ["LAST_STDOUT_FILE"]).read_text(encoding="utf-8")
stderr_text = Path(os.environ["LAST_STDERR_FILE"]).read_text(encoding="utf-8")
scenario = sys.argv[1]
chars = ascii_letters + digits
initial_secret = "abcdefghijklmnopqrstuvwxyzABCDEF"
rotated_secret = "ghijklmnopqrstuvwxyzABCDEFGH0123"
initial_mask = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
counter_path = Path(os.environ["FAKE_COUNTERS"])
counters = json.loads(counter_path.read_text(encoding="utf-8"))
task_id = str(counters.get("caller_task_id", ""))
assert len(rotated_secret) == 32

def mask_secret(secret: str, mask: str) -> str:
    return mask + "".join(
        chars[(chars.index(secret_char) + chars.index(mask_char)) % len(chars)]
        for secret_char, mask_char in zip(secret, mask)
    )

initial_masked = mask_secret(initial_secret, initial_mask)
login_html = f'<input type="hidden" name="csrfmiddlewaretoken" value="{initial_masked}">'

def events():
    output = []
    for entry in entries:
        if entry["tool"] == "curl":
            output.append(f'{entry["method"]} {entry["url"]}')
        elif entry["tool"] == "docker":
            action = entry["action"]
            if action == "compose-ps":
                output.append(f'docker compose ps {entry["service"]}')
            elif action == "inspect":
                output.append(f'docker inspect {entry["role"]}')
            else:
                output.append(f'docker {action}')
        elif entry["tool"] == "sleep":
            output.append(f'sleep {entry["seconds"]}')
    return output

if scenario == "no-actions":
    assert entries == [], entries
elif scenario == "success":
    order = events()
    required = [
        "GET http://example.test:1111/accounts/login/?next=/system/update/",
        "POST http://example.test:1111/accounts/login/?next=/system/update/",
        "POST http://example.test:1111/system/update/check/",
        "docker compose ps db",
        "docker compose ps updater",
        "docker inspect db",
        "docker inspect updater",
        "POST http://example.test:1111/system/update/start/",
        "GET http://example.test:1111/system/update/status/",
    ]
    position = -1
    for item in required:
        next_position = order.index(item, position + 1)
        assert next_position > position, order
        position = next_position
    check_entry = next(entry for entry in entries if entry["tool"] == "curl" and entry["url"].endswith("/system/update/check/"))
    start_entry = next(entry for entry in entries if entry["tool"] == "curl" and entry["url"].endswith("/system/update/start/"))
    assert check_entry["csrf_source"] == "rotated-secret"
    assert start_entry["csrf_source"] == "rotated-secret"
    cookie_jar_path = str(counters.get("cookie_jar_path", ""))
    assert cookie_jar_path
    assert not Path(cookie_jar_path).exists()
    assert not any(entry["tool"] == "docker" and entry["action"] == "stop-web" for entry in entries)
    health_calls = [entry for entry in entries if entry["tool"] == "curl" and entry["url"].endswith("/health/")]
    assert len(health_calls) == 1, health_calls
    inventory_calls = [entry for entry in entries if entry["tool"] == "docker" and entry["action"] == "image-inventory"]
    assert len(inventory_calls) == 2
elif scenario == "success-pending":
    assert stdout_text.count("cleanup pending: follow the root-only cleanup runbook without force.") == 1
    inventory_calls = [entry for entry in entries if entry["tool"] == "docker" and entry["action"] == "image-inventory"]
    assert len(inventory_calls) == 2
elif scenario == "cleanup-failure":
    inventory_calls = [entry for entry in entries if entry["tool"] == "docker" and entry["action"] == "image-inventory"]
    assert len(inventory_calls) == 2
    assert "smoke complete" not in stdout_text
    assert "cleanup pending" not in stdout_text
elif scenario == "cleanup-correlation":
    inventory_calls = [entry for entry in entries if entry["tool"] == "docker" and entry["action"] == "image-inventory"]
    assert len(inventory_calls) == 1
    assert not any(entry["tool"] == "curl" and entry["url"].endswith("/health/") for entry in entries)
    assert "smoke complete" not in stdout_text
    assert "cleanup pending" not in stdout_text
elif scenario == "rollback":
    stop_calls = [entry for entry in entries if entry["tool"] == "docker" and entry["action"] == "stop-web"]
    assert len(stop_calls) == 1
    status_calls = [entry for entry in entries if entry["tool"] == "curl" and entry["url"].endswith("/system/update/status/")]
    assert len(status_calls) >= 2, status_calls
    health_calls = [entry for entry in entries if entry["tool"] == "curl" and entry["url"].endswith("/health/")]
    assert len(health_calls) == 1, health_calls
    image_calls = [entry for entry in entries if entry["tool"] == "docker" and entry["action"] == "image-inspect"]
    assert len(image_calls) == 2
elif scenario in {"target-mismatch", "status-race", "image-drift"}:
    assert not any(entry["tool"] == "docker" and entry["action"] == "stop-web" for entry in entries)
elif scenario == "timeout":
    sleep_calls = [entry for entry in entries if entry["tool"] == "sleep"]
    assert sleep_calls, entries
elif scenario == "signal":
    signal_positions = [index for index, entry in enumerate(entries) if entry["tool"] == "signal"]
    assert signal_positions == [len(entries) - 1]
    assert entries[-1]["name"] == "TERM"
    assert stdout_text == ""
elif scenario == "helper-failure":
    assert events() == ["GET http://example.test:1111/accounts/login/?next=/system/update/"], entries
elif scenario == "private-io":
    assert entries == []
elif scenario == "malformed-inspect-json":
    assert any(entry["tool"] == "docker" and entry["action"] == "inspect" and entry["role"] == "db" for entry in entries)
else:
    raise AssertionError(f"unknown assert scenario {scenario}")

for forbidden in (
    "owner-password-safe-2026",
    initial_secret,
    rotated_secret,
    initial_masked,
    login_html,
    "session-before-login",
    "session-after-login",
    "private old csrf token",
    "private digest image alias command failure",
    "sha256:" + "a" * 64,
    "sha256:" + "b" * 64,
    "sha256:" + "c" * 64,
    "sha256:" + "d" * 64,
    "sha256:" + "e" * 64,
    "sha256:" + "f" * 64,
    "22222222-2222-2222-2222-222222222222",
    "/safe/app",
    "db-keep",
    "updater-keep",
    "web-old",
    "web-target",
    f"shunda-finance-rollback-web:{task_id}" if task_id else "never-present-private-alias",
    task_id if task_id else "never-present-caller-task-id",
    os.environ["LAST_CASE_DIR"],
    "Traceback",
    "JSONDecodeError",
    "\"task\":",
):
    assert forbidden not in stdout_text
    assert forbidden not in stderr_text
    if log_path.exists():
        assert forbidden not in log_path.read_text(encoding="utf-8")
PY
  then
    printf 'scenario %s private assertion failed\n' "$assertion_scenario" >&2
    return 1
  fi
}

test -x "$script_path"

if [ "${SMOKE_TEST_FILTER_CONTRACT_CHILD:-0}" != "1" ] && [ -z "${SMOKE_TEST_SCENARIO:-}" ]; then
  run_filter_contract
fi

SHUNDA_CONFIRM_SYSTEM_UPDATE=""
run_case "no_confirm" 2
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "explicit confirmation"
assert_python "no-actions"
rm -rf "$LAST_CASE_DIR"

unset SHUNDA_CONFIRM_SYSTEM_UPDATE
SHUNDA_BASE_URL=""
run_case "no_actions" 2
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "SHUNDA_BASE_URL is required"
assert_python "no-actions"
rm -rf "$LAST_CASE_DIR"
unset SHUNDA_BASE_URL

SHUNDA_OWNER_USERNAME=""
run_case "no_actions" 2
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "SHUNDA_OWNER_USERNAME is required"
assert_python "no-actions"
rm -rf "$LAST_CASE_DIR"
unset SHUNDA_OWNER_USERNAME

SHUNDA_OWNER_PASSWORD=""
run_case "no_actions" 2
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "SHUNDA_OWNER_PASSWORD is required"
assert_python "no-actions"
rm -rf "$LAST_CASE_DIR"
unset SHUNDA_OWNER_PASSWORD

SHUNDA_EXPECTED_TARGET=""
run_case "no_actions" 2
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "SHUNDA_EXPECTED_TARGET is required"
assert_python "no-actions"
rm -rf "$LAST_CASE_DIR"
unset SHUNDA_EXPECTED_TARGET

SHUNDA_SMOKE_MODE="rollback"
SHUNDA_CONFIRM_ROLLBACK_SMOKE=""
run_case "no_actions" 2
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "SHUNDA_CONFIRM_ROLLBACK_SMOKE=yes"
assert_python "no-actions"
rm -rf "$LAST_CASE_DIR"
unset SHUNDA_SMOKE_MODE SHUNDA_CONFIRM_ROLLBACK_SMOKE

for private_io_case in \
  private_create_failure \
  private_write_failure \
  private_chmod_failure
do
  run_case "$private_io_case" 1
  export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
  assert_contains "$LAST_STDERR_FILE" "private file operation failed"
  assert_python "private-io"
  rm -rf "$LAST_CASE_DIR"
done

run_case "signal_term" 143
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_python "signal"
rm -rf "$LAST_CASE_DIR"

run_case "success_complete" 0
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDOUT_FILE" "smoke complete for v0.2.1"
assert_python "success"
rm -rf "$LAST_CASE_DIR"

RUN_WITH_XTRACE="yes"
run_case "success_complete_xtrace" 0
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDOUT_FILE" "smoke complete for v0.2.1"
assert_python "success"
rm -rf "$LAST_CASE_DIR"
unset RUN_WITH_XTRACE

run_case "success_old_tag_config" 0
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDOUT_FILE" "smoke complete for v0.2.1"
assert_python "success"
rm -rf "$LAST_CASE_DIR"

run_case "success_pending" 0
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDOUT_FILE" "cleanup pending: follow the root-only cleanup runbook without force."
assert_python "success-pending"
rm -rf "$LAST_CASE_DIR"

for cleanup_correlation_case in \
  cleanup_public_complete_private_pending \
  cleanup_public_pending_private_complete
do
  run_case "$cleanup_correlation_case" 1
  export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
  assert_contains "$LAST_STDERR_FILE" "cleanup status does not match terminal status"
  assert_python "cleanup-correlation"
  rm -rf "$LAST_CASE_DIR"
done

run_case "preexisting_target_replaced" 0
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDOUT_FILE" "smoke complete for v0.2.1"
assert_python "success"
rm -rf "$LAST_CASE_DIR"

for original_tags_case in \
  cleanup_original_tags_string \
  cleanup_original_tags_non_string
do
  run_case "$original_tags_case" 1
  export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
  assert_contains "$LAST_STDERR_FILE" "cleanup inventory proof failed"
  assert_python "cleanup-failure"
  rm -rf "$LAST_CASE_DIR"
done

run_case "success_dangling_added" 0
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDOUT_FILE" "smoke complete for v0.2.1"
assert_python "success"
rm -rf "$LAST_CASE_DIR"

for dangling_failure_case in \
  cleanup_complete_dangling_missing \
  cleanup_complete_dangling_retarget
do
  run_case "$dangling_failure_case" 1
  export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
  assert_contains "$LAST_STDERR_FILE" "cleanup inventory proof failed"
  assert_python "cleanup-failure"
  rm -rf "$LAST_CASE_DIR"
done

for cleanup_case in \
  cleanup_complete_old_tag \
  cleanup_complete_alias \
  cleanup_complete_old_id \
  cleanup_complete_unrelated_missing \
  cleanup_complete_unrelated_retarget \
  cleanup_complete_duplicate_old_ref \
  cleanup_pending_missing_tag \
  cleanup_pending_missing_alias \
  cleanup_pending_tag_retarget \
  cleanup_pending_alias_retarget \
  cleanup_pending_old_id_missing \
  cleanup_pending_duplicate_alias
do
  run_case "$cleanup_case" 1
  export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
  assert_contains "$LAST_STDERR_FILE" "cleanup inventory proof failed"
  assert_python "cleanup-failure"
  rm -rf "$LAST_CASE_DIR"
done

run_case "target_mismatch" 1
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "expected target"
assert_python "target-mismatch"
rm -rf "$LAST_CASE_DIR"

run_case "start_id_mismatch" 1
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "started task does not match caller task"
rm -rf "$LAST_CASE_DIR"

SHUNDA_SMOKE_MODE="rollback"
SHUNDA_CONFIRM_ROLLBACK_SMOKE="yes"
run_case "rollback" 0
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDOUT_FILE" "rollback smoke verified for v0.2.1"
assert_python "rollback"
rm -rf "$LAST_CASE_DIR"
unset SHUNDA_SMOKE_MODE SHUNDA_CONFIRM_ROLLBACK_SMOKE

SHUNDA_SMOKE_MODE="rollback"
SHUNDA_CONFIRM_ROLLBACK_SMOKE="yes"
run_case "status_race" 1
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "status task does not match started task"
assert_python "status-race"
rm -rf "$LAST_CASE_DIR"
unset SHUNDA_SMOKE_MODE SHUNDA_CONFIRM_ROLLBACK_SMOKE

SHUNDA_SMOKE_MODE="rollback"
SHUNDA_CONFIRM_ROLLBACK_SMOKE="yes"
run_case "image_drift" 1
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "rollback target image drifted before stop"
assert_python "image-drift"
rm -rf "$LAST_CASE_DIR"
unset SHUNDA_SMOKE_MODE SHUNDA_CONFIRM_ROLLBACK_SMOKE

for rollback_identity_case in \
  "image_id_drift:rollback target image ID drifted before stop" \
  "oci_drift:rollback target version drifted before stop" \
  "compose_project_drift:rollback mutation refused non-project container" \
  "compose_service_drift:rollback mutation refused non-web container" \
  "web_multiplicity:multiple containers"
do
  scenario="${rollback_identity_case%%:*}"
  expected_error="${rollback_identity_case#*:}"
  SHUNDA_SMOKE_MODE="rollback"
  SHUNDA_CONFIRM_ROLLBACK_SMOKE="yes"
  run_case "$scenario" 1
  export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
  assert_contains "$LAST_STDERR_FILE" "$expected_error"
  assert_python "image-drift"
  rm -rf "$LAST_CASE_DIR"
  unset SHUNDA_SMOKE_MODE SHUNDA_CONFIRM_ROLLBACK_SMOKE
done

run_case "failed_terminal" 1
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "success smoke ended in failed"
rm -rf "$LAST_CASE_DIR"

run_case "manual_terminal" 1
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "manual intervention"
rm -rf "$LAST_CASE_DIR"

run_case "timeout" 1
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "10 minutes"
assert_python "timeout"
rm -rf "$LAST_CASE_DIR"

run_case "identity_drift" 1
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "identity changed"
rm -rf "$LAST_CASE_DIR"

run_case "empty_db_backup" 1
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "database backup presence failed"
rm -rf "$LAST_CASE_DIR"

run_case "empty_uploads_backup" 1
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "uploads backup presence failed"
rm -rf "$LAST_CASE_DIR"

run_case "state_mode_invalid" 1
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "state file mode is not 0600"
rm -rf "$LAST_CASE_DIR"

run_case "raw_command_error" 1
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "docker inspect failed"
assert_not_contains "$LAST_STDERR_FILE" "private digest image alias command failure"
rm -rf "$LAST_CASE_DIR"

run_case "helper_failure" 1
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "login request body creation failed"
assert_python "helper-failure"
rm -rf "$LAST_CASE_DIR"

run_case "malformed_inspect_json" 1
export LAST_LOG_FILE LAST_STDOUT_FILE LAST_STDERR_FILE
assert_contains "$LAST_STDERR_FILE" "invalid container inspect payload"
assert_python "malformed-inspect-json"
rm -rf "$LAST_CASE_DIR"

if [ -n "${SMOKE_TEST_SCENARIO:-}" ] && [ "$executed_case_count" -eq 0 ]; then
  printf 'SMOKE_TEST_SCENARIO did not match any smoke test scenario\n' >&2
  exit 1
fi

echo "system-update-dsm-smoke contract tests passed"
