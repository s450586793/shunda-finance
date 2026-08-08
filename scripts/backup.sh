#!/usr/bin/env sh
set -eu

umask 077
backup_dir="${BACKUP_DIR:-/data/backups}"
uploads_dir="${UPLOADS_DIR:-/data/uploads}"
stamp="$(date +%Y%m%d-%H%M%S)"

if [ -z "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL is required" >&2
  exit 2
fi
if [ ! -d "$uploads_dir" ]; then
  echo "上传目录不存在：$uploads_dir" >&2
  exit 2
fi

mkdir -p "$backup_dir"
backup_dir="$(cd "$backup_dir" && pwd -P)"
uploads_dir="$(cd "$uploads_dir" && pwd -P)"
uploads_parent_dir="$(dirname "$uploads_dir")"
uploads_name="$(basename "$uploads_dir")"
dump_path="$backup_dir/db-$stamp.dump"
archive_path="$backup_dir/uploads-$stamp.tar.gz"
trap 'rm -f "$dump_path.tmp" "$archive_path.tmp"' EXIT HUP INT TERM
pg_dump "$DATABASE_URL" --format=custom --file="$dump_path.tmp"
mv "$dump_path.tmp" "$dump_path"
tar -C "$uploads_parent_dir" -czf "$archive_path.tmp" -- "$uploads_name"
mv "$archive_path.tmp" "$archive_path"
test -s "$dump_path"
test -s "$archive_path"
printf 'DB_BACKUP=%s\n' "$dump_path"
printf 'UPLOADS_BACKUP=%s\n' "$archive_path"
find "$backup_dir" -type f \( -name 'db-*.dump' -o -name 'uploads-*.tar.gz' \) -mtime +30 -delete
