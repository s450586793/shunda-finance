from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

MAX_ENV_BYTES = 1_048_576
VERSION_PATTERN = re.compile(r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
LINE_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
LOCK_PATH = Path("/run/shunda-system-update-updater.lock")
LOCK_FAILURE_MESSAGE = b"updater update requires manual intervention\n"


def fail() -> NoReturn:
    raise SystemExit(1)


def private_write(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def safe_root_directory(path: Path) -> None:
    value = path.lstat()
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != 0
        or stat.S_IMODE(value.st_mode) & 0o022
    ):
        raise ValueError("unsafe lock parent")


def close_untrusted_descriptors(lock_descriptor: int) -> None:
    descriptors = []
    for name in os.listdir("/proc/self/fd"):
        try:
            descriptor = int(name)
        except ValueError:
            continue
        if descriptor >= 3 and descriptor != lock_descriptor:
            descriptors.append(descriptor)
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise


def lock_exec(arguments: list[str]) -> NoReturn:
    lock_descriptor = -1
    try:
        if len(arguments) < 2:
            raise ValueError("missing trusted Bash command")
        bash_binary = Path(arguments[0])
        if not bash_binary.is_absolute():
            raise ValueError("trusted Bash command is not absolute")

        safe_root_directory(Path("/"))
        safe_root_directory(LOCK_PATH.parent)
        try:
            lock_descriptor = os.open(
                LOCK_PATH,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC
                | os.O_NONBLOCK,
                0o600,
            )
            os.fchmod(lock_descriptor, 0o600)
        except FileExistsError:
            lock_descriptor = os.open(
                LOCK_PATH,
                os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            )

        linked = LOCK_PATH.lstat()
        opened = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != 0
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != 0
            or linked.st_dev != opened.st_dev
            or linked.st_ino != opened.st_ino
        ):
            raise ValueError("unsafe updater lock")
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        close_untrusted_descriptors(lock_descriptor)
        os.set_inheritable(lock_descriptor, True)
        os.execve(
            bash_binary,
            [str(bash_binary), "-s", "--", *arguments[1:]],
            dict(os.environ),
        )
    except (OSError, ValueError):
        if lock_descriptor >= 0:
            os.close(lock_descriptor)
        try:
            os.write(2, LOCK_FAILURE_MESSAGE)
        except OSError:
            pass
        fail()


def read_environment(path: Path, expected_stat: os.stat_result | None = None) -> tuple[bytes, os.stat_result]:
    linked = path.lstat()
    if (
        not stat.S_ISREG(linked.st_mode)
        or stat.S_IMODE(linked.st_mode) != 0o600
        or linked.st_uid != 0
        or linked.st_size > MAX_ENV_BYTES
    ):
        fail()
    if expected_stat is not None and (
        linked.st_dev != expected_stat.st_dev or linked.st_ino != expected_stat.st_ino
    ):
        fail()

    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != 0
            or opened.st_dev != linked.st_dev
            or opened.st_ino != linked.st_ino
        ):
            fail()
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            payload = handle.read(MAX_ENV_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > MAX_ENV_BYTES:
        fail()
    return payload, opened


def stat_payload(value: os.stat_result) -> bytes:
    return json.dumps(
        {
            "device": value.st_dev,
            "inode": value.st_ino,
            "mode": stat.S_IMODE(value.st_mode),
            "uid": value.st_uid,
        },
        separators=(",", ":"),
    ).encode("ascii")


def capture_environment(arguments: list[str]) -> None:
    if len(arguments) != 8:
        fail()
    app_dir = Path(arguments[0])
    env_path = Path(arguments[1])
    compose_path = Path(arguments[2])
    target_tag = arguments[3]
    original_path = Path(arguments[4])
    target_path = Path(arguments[5])
    old_tag_path = Path(arguments[6])
    stat_path = Path(arguments[7])

    resolved_app = app_dir.resolve(strict=True)
    app_stat = app_dir.lstat()
    if (
        resolved_app != app_dir
        or not stat.S_ISDIR(app_stat.st_mode)
        or app_stat.st_uid != 0
        or stat.S_IMODE(app_stat.st_mode) & 0o022
    ):
        fail()

    compose_stat = compose_path.lstat()
    if (
        not stat.S_ISREG(compose_stat.st_mode)
        or compose_stat.st_uid != 0
        or stat.S_IMODE(compose_stat.st_mode) & 0o022
    ):
        fail()

    payload, env_stat = read_environment(env_path)
    text = payload.decode("utf-8", errors="strict")
    seen: set[str] = set()
    updater_index = None
    old_tag = None
    lines = text.splitlines(keepends=True)
    for index, complete_line in enumerate(lines):
        line = complete_line.rstrip("\r\n")
        if not line or line.startswith("#"):
            continue
        match = LINE_PATTERN.fullmatch(line)
        if match is None or match.group(1) in seen:
            fail()
        key, value = match.groups()
        seen.add(key)
        if key == "SHUNDA_UPDATER_IMAGE_TAG":
            updater_index = index
            old_tag = value

    if updater_index is None or old_tag is None or VERSION_PATTERN.fullmatch(old_tag) is None:
        fail()
    if old_tag == target_tag:
        fail()

    complete_line = lines[updater_index]
    ending = complete_line[len(complete_line.rstrip("\r\n")) :]
    lines[updater_index] = f"SHUNDA_UPDATER_IMAGE_TAG={target_tag}{ending}"
    target_payload = "".join(lines).encode("utf-8")
    if target_payload == payload:
        fail()

    private_write(original_path, payload)
    private_write(target_path, target_payload)
    private_write(old_tag_path, old_tag.encode("ascii"))
    private_write(stat_path, stat_payload(env_stat))


def expected_environment_stat(path: Path) -> dict[str, int]:
    value = json.loads(path.read_text(encoding="ascii"))
    if (
        not isinstance(value, dict)
        or set(value) != {"device", "inode", "mode", "uid"}
        or any(not isinstance(item, int) for item in value.values())
        or value["mode"] != 0o600
        or value["uid"] != 0
    ):
        fail()
    return value


def validate_current_environment(
    path: Path,
    expected: bytes,
    expected_stat: dict[str, int],
) -> os.stat_result:
    linked = path.lstat()
    if (
        not stat.S_ISREG(linked.st_mode)
        or stat.S_IMODE(linked.st_mode) != 0o600
        or linked.st_uid != 0
        or linked.st_dev != expected_stat["device"]
        or linked.st_ino != expected_stat["inode"]
    ):
        raise ValueError("unsafe environment path")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if opened.st_dev != linked.st_dev or opened.st_ino != linked.st_ino:
            raise ValueError("environment race")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            current = handle.read(len(expected) + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if current != expected:
        raise ValueError("environment changed")
    return linked


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_replaced_parent(path: Path) -> None:
    fsync_directory(path.parent)


def validate_replaced_environment(
    path: Path,
    source: bytes,
    expected_stat: dict[str, int],
    temporary_stat: os.stat_result,
) -> os.stat_result:
    linked = path.lstat()
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != 0
            or linked.st_dev != opened.st_dev
            or linked.st_ino != opened.st_ino
            or opened.st_dev != temporary_stat.st_dev
            or opened.st_ino != temporary_stat.st_ino
            or (
                opened.st_dev == expected_stat["device"]
                and opened.st_ino == expected_stat["inode"]
            )
        ):
            raise ValueError("environment postcondition failed")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            replaced_payload = handle.read(len(source) + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if replaced_payload != source:
        raise ValueError("environment postcondition failed")
    return opened


def write_next_stat(path: Path, value: os.stat_result) -> None:
    private_write(path, stat_payload(value))


def replace_environment(arguments: list[str]) -> None:
    if len(arguments) not in {5, 6}:
        fail()
    path = Path(arguments[0])
    source_path = Path(arguments[1])
    expected_path = Path(arguments[2])
    expected_stat_path = Path(arguments[3])
    next_stat_path = Path(arguments[4])
    marker_path = Path(arguments[5]) if len(arguments) == 6 and arguments[5] else None
    source = source_path.read_bytes()
    expected = expected_path.read_bytes()
    expected_stat = expected_environment_stat(expected_stat_path)
    temporary_path = None

    try:
        if marker_path is not None:
            private_write(marker_path, b"recovery-required")
            fsync_directory(marker_path.parent)
        validate_current_environment(path, expected, expected_stat)
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=".system-update-updater.",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            os.chmod(temporary_path, 0o600)
            temporary_file.write(source)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_stat = os.fstat(temporary_file.fileno())
            if (
                not stat.S_ISREG(temporary_stat.st_mode)
                or stat.S_IMODE(temporary_stat.st_mode) != 0o600
                or temporary_stat.st_uid != 0
                or (
                    temporary_stat.st_dev == expected_stat["device"]
                    and temporary_stat.st_ino == expected_stat["inode"]
                )
            ):
                raise ValueError("unsafe temporary environment")
        validate_current_environment(path, expected, expected_stat)
        os.replace(temporary_path, path)
        temporary_path = None
        fsync_replaced_parent(path)
        replaced_stat = validate_replaced_environment(path, source, expected_stat, temporary_stat)
        write_next_stat(next_stat_path, replaced_stat)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def classify_environment(arguments: list[str]) -> None:
    if len(arguments) != 6:
        fail()
    path = Path(arguments[0])
    original = Path(arguments[1]).read_bytes()
    target = Path(arguments[2]).read_bytes()
    original_stat = expected_environment_stat(Path(arguments[3]))
    stat_output = Path(arguments[4])
    state_output = Path(arguments[5])

    payload, opened = read_environment(path)
    if payload == target:
        state = b"target"
        private_write(stat_output, stat_payload(opened))
    elif (
        payload == original
        and opened.st_dev == original_stat["device"]
        and opened.st_ino == original_stat["inode"]
    ):
        state = b"original"
    else:
        fail()
    private_write(state_output, state)


def main() -> None:
    if len(sys.argv) < 2:
        fail()
    command = sys.argv[1]
    arguments = sys.argv[2:]
    if command == "capture":
        capture_environment(arguments)
    elif command == "replace":
        replace_environment(arguments)
    elif command == "classify":
        classify_environment(arguments)
    elif command == "lock-exec":
        lock_exec(arguments)
    else:
        fail()


if __name__ == "__main__":
    main()
