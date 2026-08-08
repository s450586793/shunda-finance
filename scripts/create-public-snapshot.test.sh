#!/usr/bin/env bash
set -euo pipefail

project_root="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)"
tool_path="$project_root/scripts/create-public-snapshot.sh"
suite_dir="$(mktemp -d)"

cleanup_suite() {
  local status=$?
  trap - EXIT
  rm -rf -- "$suite_dir"
  exit "$status"
}
trap cleanup_suite EXIT

fail_test() {
  printf '%s\n' "$1" >&2
  exit 1
}

[ -x "$tool_path" ] || fail_test "missing executable public snapshot tool"

source_repo="$suite_dir/source"
mkdir -p "$source_repo/scripts" "$source_repo/apps" "$source_repo/.superpowers"
cp "$tool_path" "$source_repo/scripts/create-public-snapshot.sh"
cp "$project_root/scripts/scan-public-tree.py" "$source_repo/scripts/scan-public-tree.py"
cat >"$source_repo/scripts/public-snapshot-manifest.txt" <<'EOF'
README.md
apps
scripts
EOF
printf 'public source\n' >"$source_repo/README.md"
printf 'print("public")\n' >"$source_repo/apps/example.py"
printf 'private report\n' >"$source_repo/.superpowers/report.md"

git -C "$source_repo" init -q -b feature
git -C "$source_repo" add README.md apps scripts .superpowers
git -C "$source_repo" -c user.name=Fixture -c user.email=fixture@example.invalid \
  commit -qm 'fixture source'
source_head="$(git -C "$source_repo" rev-parse HEAD)"

anchors="$suite_dir/anchors.txt"
printf 'PRIVATE-COMPANY\nPRIVATE-TAX-ID\n' >"$anchors"
chmod 600 "$anchors"

destination="$suite_dir/public"
SHUNDA_PUBLIC_SENSITIVE_ANCHORS_FILE="$anchors" \
  "$source_repo/scripts/create-public-snapshot.sh" "$destination"

[ "$(git -C "$destination" branch --show-current)" = "main" ] || \
  fail_test "snapshot branch must be main"
[ "$(git -C "$destination" rev-list --count HEAD)" = "1" ] || \
  fail_test "snapshot must contain exactly one commit"
[ "$(git -C "$destination" rev-list --parents -n 1 HEAD | awk '{print NF}')" = "1" ] || \
  fail_test "snapshot root commit must not have a parent"
[ -f "$destination/apps/example.py" ] || fail_test "allowlisted content missing"
[ ! -e "$destination/.superpowers" ] || fail_test "non-manifest content was copied"

git -C "$destination" fetch -q "$source_repo" \
  "$source_head:refs/private/source-head"
set +e
git -C "$destination" merge-base --is-ancestor "$source_head" HEAD
ancestor_status=$?
set -e
[ "$ancestor_status" -eq 1 ] || fail_test "source history must not be snapshot ancestry"

printf 'dirty\n' >>"$source_repo/README.md"
set +e
SHUNDA_PUBLIC_SENSITIVE_ANCHORS_FILE="$anchors" \
  "$source_repo/scripts/create-public-snapshot.sh" "$suite_dir/dirty-target" \
  >"$suite_dir/dirty.stdout" 2>"$suite_dir/dirty.stderr"
dirty_status=$?
set -e
[ "$dirty_status" -ne 0 ] || fail_test "dirty tracked source must fail"
git -C "$source_repo" restore README.md

printf 'missing-file\n' >>"$source_repo/scripts/public-snapshot-manifest.txt"
git -C "$source_repo" add scripts/public-snapshot-manifest.txt
git -C "$source_repo" -c user.name=Fixture -c user.email=fixture@example.invalid \
  commit -qm 'unknown manifest entry'
set +e
SHUNDA_PUBLIC_SENSITIVE_ANCHORS_FILE="$anchors" \
  "$source_repo/scripts/create-public-snapshot.sh" "$suite_dir/unknown-target" \
  >"$suite_dir/unknown.stdout" 2>"$suite_dir/unknown.stderr"
unknown_status=$?
set -e
[ "$unknown_status" -ne 0 ] || fail_test "unknown manifest entry must fail"
git -C "$source_repo" reset -q --hard HEAD^

printf 'PRIVATE-COMPANY\n' >"$source_repo/apps/private.py"
git -C "$source_repo" add apps/private.py
git -C "$source_repo" -c user.name=Fixture -c user.email=fixture@example.invalid \
  commit -qm 'sensitive anchor fixture'
set +e
SHUNDA_PUBLIC_SENSITIVE_ANCHORS_FILE="$anchors" \
  "$source_repo/scripts/create-public-snapshot.sh" "$suite_dir/sensitive-target" \
  >"$suite_dir/sensitive.stdout" 2>"$suite_dir/sensitive.stderr"
sensitive_status=$?
set -e
[ "$sensitive_status" -ne 0 ] || fail_test "sensitive anchor must fail"
git -C "$source_repo" reset -q --hard HEAD^

printf 'local secret\n' >"$source_repo/.env"
printf '.env\n' >>"$source_repo/scripts/public-snapshot-manifest.txt"
git -C "$source_repo" add .env scripts/public-snapshot-manifest.txt
git -C "$source_repo" -c user.name=Fixture -c user.email=fixture@example.invalid \
  commit -qm 'forbidden file fixture'
set +e
SHUNDA_PUBLIC_SENSITIVE_ANCHORS_FILE="$anchors" \
  "$source_repo/scripts/create-public-snapshot.sh" "$suite_dir/forbidden-target" \
  >"$suite_dir/forbidden.stdout" 2>"$suite_dir/forbidden.stderr"
forbidden_status=$?
set -e
[ "$forbidden_status" -ne 0 ] || fail_test "forbidden manifest path must fail"
git -C "$source_repo" reset -q --hard HEAD^

printf 'nested private key fixture\n' >"$source_repo/apps/private.pem"
git -C "$source_repo" add apps/private.pem
git -C "$source_repo" -c user.name=Fixture -c user.email=fixture@example.invalid \
  commit -qm 'nested forbidden file fixture'
set +e
SHUNDA_PUBLIC_SENSITIVE_ANCHORS_FILE="$anchors" \
  "$source_repo/scripts/create-public-snapshot.sh" "$suite_dir/nested-forbidden-target" \
  >"$suite_dir/nested-forbidden.stdout" 2>"$suite_dir/nested-forbidden.stderr"
nested_forbidden_status=$?
set -e
[ "$nested_forbidden_status" -ne 0 ] || fail_test "nested forbidden file must fail"
git -C "$source_repo" reset -q --hard HEAD^

mkdir "$suite_dir/nonempty"
printf 'keep\n' >"$suite_dir/nonempty/existing"
set +e
SHUNDA_PUBLIC_SENSITIVE_ANCHORS_FILE="$anchors" \
  "$source_repo/scripts/create-public-snapshot.sh" "$suite_dir/nonempty" \
  >"$suite_dir/nonempty.stdout" 2>"$suite_dir/nonempty.stderr"
nonempty_status=$?
set -e
[ "$nonempty_status" -ne 0 ] || fail_test "nonempty destination must fail"
[ "$(<"$suite_dir/nonempty/existing")" = "keep" ] || \
  fail_test "nonempty destination must remain untouched"

for output in "$suite_dir"/*.stdout "$suite_dir"/*.stderr; do
  [ -e "$output" ] || continue
  ! grep -Fq 'PRIVATE-COMPANY' "$output" || fail_test "anchor leaked in output"
  ! grep -Fq "$source_repo" "$output" || fail_test "source path leaked in output"
done

printf 'public snapshot contract passed\n'
