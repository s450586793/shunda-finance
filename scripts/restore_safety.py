import argparse
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from psycopg.conninfo import conninfo_to_dict

DISALLOWED_CONNECTION_OPTIONS = {"service", "servicefile", "hostaddr"}
RESTORE_DATABASE_SUFFIXES = ("_restore", "_test")


class RestoreSafetyError(ValueError):
    pass


@dataclass(frozen=True)
class ConnectionTarget:
    host: str
    port: str
    dbname: str


def validate_restore_target(
    production_url: str | None,
    restore_url: str | None,
    *,
    environment: dict[str, str] | None = None,
) -> None:
    _reject_libpq_environment(os.environ if environment is None else environment)
    production = _connection_target(production_url, "生产数据库")
    restore = _connection_target(restore_url, "恢复数据库")
    if restore.dbname == production.dbname:
        raise RestoreSafetyError("恢复数据库不能与生产数据库相同")
    if restore == production:
        raise RestoreSafetyError("恢复目标不能与生产目标相同")
    if not restore.dbname.casefold().endswith(RESTORE_DATABASE_SUFFIXES):
        raise RestoreSafetyError("恢复数据库名称必须以 _restore 或 _test 结尾")


def _reject_libpq_environment(environment: dict[str, str]) -> None:
    if any(name.startswith("PG") and value for name, value in environment.items()):
        raise RestoreSafetyError("恢复环境不能包含 libpq 连接配置")


def extract_uploads_archive(archive_path: str | Path, destination: str | Path) -> None:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                _validate_upload_member(member)
            archive.extractall(destination, members=members, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise RestoreSafetyError("上传备份不安全或无法读取") from exc


def _connection_target(url: str | None, label: str) -> ConnectionTarget:
    if not url:
        raise RestoreSafetyError(f"{label}连接配置缺失")
    try:
        options = conninfo_to_dict(url)
    except Exception as exc:
        raise RestoreSafetyError(f"{label}连接配置无效") from exc
    if any(options.get(name) for name in DISALLOWED_CONNECTION_OPTIONS):
        raise RestoreSafetyError(f"{label}不能使用服务或 hostaddr 配置")
    host = str(options.get("host", "")).strip().casefold().rstrip(".")
    port = str(options.get("port") or "5432").strip()
    dbname = str(options.get("dbname", "")).strip()
    if not host or not port or not dbname or "=" in dbname:
        raise RestoreSafetyError(f"{label}必须显式指定 host、port 和 dbname")
    return ConnectionTarget(host, port, dbname.casefold())


def _validate_upload_member(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or path.parts[0] != "uploads"
        or not (member.isdir() or member.isreg())
    ):
        raise RestoreSafetyError("上传备份包含不安全成员")


def main() -> int:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("validate-target")
    extract = subcommands.add_parser("extract-uploads")
    extract.add_argument("archive")
    extract.add_argument("destination")
    arguments = parser.parse_args()
    try:
        if arguments.command == "validate-target":
            validate_restore_target(
                os.environ.get("DATABASE_URL"), os.environ.get("RESTORE_DATABASE_URL")
            )
        else:
            extract_uploads_archive(arguments.archive, arguments.destination)
    except RestoreSafetyError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
