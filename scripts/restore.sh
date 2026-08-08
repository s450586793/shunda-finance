#!/usr/bin/env sh
set -eu

dump_path="${1:-}"
uploads_path="${2:-}"
target_database="${RESTORE_DATABASE_URL:-}"
data_dir="/data"
uploads_dir="$data_dir/uploads"
backup_dir="$data_dir/backups"

if [ -z "$dump_path" ] || [ -z "$uploads_path" ] || [ -z "$target_database" ]; then
  echo "用法: RESTORE_DATABASE_URL=... restore.sh <dump> <uploads.tar.gz>" >&2
  exit 2
fi
if [ "${CONFIRM_RESTORE:-NO}" != "YES" ]; then
  echo "必须设置 CONFIRM_RESTORE=YES" >&2
  exit 3
fi
if [ ! -f "$dump_path" ] || [ ! -f "$uploads_path" ]; then
  echo "备份文件不存在" >&2
  exit 2
fi
if [ ! -d "$backup_dir" ]; then
  echo "备份目录不存在：$backup_dir" >&2
  exit 2
fi
canonical_backup_dir="$(realpath -e "$backup_dir")"
canonical_dump_path="$(realpath -e "$dump_path")"
canonical_uploads_path="$(realpath -e "$uploads_path")"
case "$canonical_dump_path" in
  "$canonical_backup_dir"/*) ;;
  *)
    echo "数据库备份必须位于 $backup_dir" >&2
    exit 2
    ;;
esac
case "$canonical_uploads_path" in
  "$canonical_backup_dir"/*) ;;
  *)
    echo "上传备份必须位于 $backup_dir" >&2
    exit 2
    ;;
esac
if ! python /app/scripts/restore_safety.py validate-target; then
  echo "RESTORE_DATABASE_URL 必须是独立且安全的测试或恢复数据库" >&2
  exit 2
fi

restore_dir="$(mktemp -d "$backup_dir/.uploads-restore.XXXXXX")"
trap 'rm -rf "$restore_dir"' EXIT HUP INT TERM
if ! python /app/scripts/restore_safety.py extract-uploads "$canonical_uploads_path" "$restore_dir"; then
  echo "上传备份包含不安全成员" >&2
  exit 2
fi
if [ ! -d "$restore_dir/uploads" ]; then
  echo "上传备份不包含 uploads 目录" >&2
  exit 2
fi

sh /app/scripts/pg_restore_clean.sh "$target_database" "$canonical_dump_path"
stamp="$(date +%Y%m%d-%H%M%S)"
preserved_archive="$backup_dir/uploads-before-restore-$stamp.tar.gz"
if [ -d "$uploads_dir" ]; then
  tar -C "$data_dir" -czf "$preserved_archive.tmp" -- uploads
  mv "$preserved_archive.tmp" "$preserved_archive"
fi
mkdir -p "$uploads_dir"
find "$uploads_dir" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
tar -C "$restore_dir/uploads" -cf - . | tar -C "$uploads_dir" -xpf -
