import json
import os
import stat
import tempfile
from pathlib import Path

from updater.types import PersistentState, persistent_state_from_dict


class StateStoreError(ValueError):
    pass


class FileStateStore:
    MAX_BYTES = 1_048_576

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> PersistentState:
        try:
            parent_stat = self.path.parent.lstat()
        except FileNotFoundError:
            return PersistentState()
        except OSError as error:
            raise StateStoreError("unsafe_state_path") from error
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or stat.S_IMODE(parent_stat.st_mode) != 0o700
        ):
            raise StateStoreError("unsafe_state_path")

        try:
            file_stat = self.path.lstat()
        except FileNotFoundError:
            return PersistentState()
        except OSError as error:
            raise StateStoreError("unsafe_state_path") from error
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or stat.S_IMODE(file_stat.st_mode) != 0o600
        ):
            raise StateStoreError("unsafe_state_path")
        if file_stat.st_size > self.MAX_BYTES:
            raise StateStoreError("state_too_large")

        payload = self._read_validated_file(file_stat)
        if len(payload) > self.MAX_BYTES:
            raise StateStoreError("state_too_large")
        try:
            return persistent_state_from_dict(json.loads(payload))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise StateStoreError("invalid_state") from error

    def _read_validated_file(self, expected_stat: os.stat_result) -> bytes:
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
        except OSError as error:
            raise StateStoreError("unsafe_state_path") from error
        try:
            opened_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or stat.S_IMODE(opened_stat.st_mode) != 0o600
                or opened_stat.st_dev != expected_stat.st_dev
                or opened_stat.st_ino != expected_stat.st_ino
            ):
                raise StateStoreError("unsafe_state_path")
            with os.fdopen(descriptor, "rb", closefd=True) as state_file:
                descriptor = -1
                return state_file.read(self.MAX_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def save(self, state: PersistentState) -> None:
        payload = json.dumps(state.to_dict(), separators=(",", ":")).encode()
        if len(payload) > self.MAX_BYTES:
            raise StateStoreError("state_too_large")
        atomic_private_replace(self.path, payload)


def atomic_private_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, delete=False
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            os.chmod(temporary_path, 0o600)
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
