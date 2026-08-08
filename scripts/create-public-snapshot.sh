#!/usr/bin/env bash
set -euo pipefail

readonly fixed_error="public snapshot creation failed"
destination=""
destination_ready=0
completed=0

cleanup_on_exit() {
  local status=$?
  trap - EXIT
  if [ "$completed" -ne 1 ] && [ "$destination_ready" -eq 1 ]; then
    find "$destination" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + \
      >/dev/null 2>&1 || true
  fi
  if [ "$status" -ne 0 ]; then
    printf '%s\n' "$fixed_error" >&2
  fi
  exit "$status"
}
trap cleanup_on_exit EXIT

fail_closed() {
  return 1
}

[ "$#" -eq 1 ] || fail_closed
[ -n "${SHUNDA_PUBLIC_SENSITIVE_ANCHORS_FILE:-}" ] || fail_closed

script_dir="$(CDPATH= cd -- "$(dirname "$0")" && pwd -P)" || fail_closed
source_root="$(git -C "$script_dir/.." rev-parse --show-toplevel 2>/dev/null)" || fail_closed
manifest_path="$source_root/scripts/public-snapshot-manifest.txt"
[ -f "$manifest_path" ] && [ ! -L "$manifest_path" ] || fail_closed

git -C "$source_root" diff --quiet --ignore-submodules -- 2>/dev/null || fail_closed
git -C "$source_root" diff --cached --quiet --ignore-submodules -- 2>/dev/null || fail_closed
source_head="$(git -C "$source_root" rev-parse --verify HEAD 2>/dev/null)" || fail_closed

anchor_path="$(realpath -e -- "$SHUNDA_PUBLIC_SENSITIVE_ANCHORS_FILE" 2>/dev/null)" || fail_closed
[ -f "$anchor_path" ] && [ ! -L "$anchor_path" ] || fail_closed
[ "$(stat -c '%a' -- "$anchor_path" 2>/dev/null)" = "600" ] || fail_closed
case "$anchor_path" in
  "$source_root"|"$source_root"/*) fail_closed ;;
esac

destination="$(realpath -m -- "$1" 2>/dev/null)" || fail_closed
[ "$destination" != "/" ] || fail_closed
case "$destination" in
  "$source_root"|"$source_root"/*) fail_closed ;;
esac

if [ -e "$destination" ] || [ -L "$destination" ]; then
  [ -d "$destination" ] && [ ! -L "$destination" ] || fail_closed
  [ -z "$(find "$destination" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ] || \
    fail_closed
else
  [ -d "$(dirname "$destination")" ] || fail_closed
  mkdir -- "$destination" >/dev/null 2>&1 || fail_closed
fi
destination_ready=1

declare -a manifest_entries=()
declare -A seen_entries=()
while IFS= read -r entry || [ -n "$entry" ]; do
  entry="${entry%$'\r'}"
  [ -n "$entry" ] || continue
  case "$entry" in \#*) continue ;; esac
  [[ "$entry" =~ ^[.A-Za-z0-9_-]+(/[.A-Za-z0-9_-]+)*$ ]] || fail_closed
  case "/$entry/" in
    */../*|*/.git/*|*/.superpowers/*|*/.venv/*|*/.workflow/*|*/node_modules/*|*/__pycache__/*) fail_closed ;;
  esac
  case "$entry" in
    .env|db.sqlite3|*.sqlite|*.sqlite3|*.pem|*.key|*.p12|*.pfx) fail_closed ;;
  esac
  [ -z "${seen_entries[$entry]:-}" ] || fail_closed
  seen_entries[$entry]=1
  git -C "$source_root" ls-tree -r --name-only "$source_head" -- "$entry" \
    2>/dev/null | grep -q . || fail_closed
  manifest_entries+=("$entry")
done <"$manifest_path"
[ "${#manifest_entries[@]}" -gt 0 ] || fail_closed

git -C "$source_root" archive --format=tar "$source_head" -- "${manifest_entries[@]}" \
  2>/dev/null | tar -xf - -C "$destination" 2>/dev/null || fail_closed

python3 "$source_root/scripts/scan-public-tree.py" "$destination" "$anchor_path" \
  >/dev/null 2>&1 || fail_closed

git -C "$destination" -c core.hooksPath=/dev/null init -q -b main \
  >/dev/null 2>&1 || fail_closed
git -C "$destination" -c core.hooksPath=/dev/null add -- "${manifest_entries[@]}" \
  >/dev/null 2>&1 || fail_closed
git -C "$destination" diff --cached --quiet && fail_closed
git -C "$destination" \
  -c core.hooksPath=/dev/null \
  -c commit.gpgsign=false \
  -c user.name="Shunda Public Snapshot" \
  -c user.email="snapshot@example.invalid" \
  commit -qm "chore: 创建公开源码快照" >/dev/null 2>&1 || fail_closed

[ "$(git -C "$destination" branch --show-current 2>/dev/null)" = "main" ] || fail_closed
[ "$(git -C "$destination" rev-list --count HEAD 2>/dev/null)" = "1" ] || fail_closed
[ "$(git -C "$destination" rev-list --parents -n 1 HEAD 2>/dev/null | awk '{print NF}')" = "1" ] || \
  fail_closed
[ -z "$(git -C "$destination" status --porcelain=v1 --untracked-files=all 2>/dev/null)" ] || \
  fail_closed

completed=1
printf 'public snapshot created\n'
