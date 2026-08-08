#!/usr/bin/env sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "用法: pg_restore_clean.sh <database-url> <dump>" >&2
  exit 2
fi

target_database="$1"
dump_path="$2"
pg_restore_path="$(command -v pg_restore)"

exec env -i \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  HOME=/tmp \
  LANG=C.UTF-8 \
  "$pg_restore_path" --clean --if-exists --no-owner \
  --dbname="$target_database" "$dump_path"
