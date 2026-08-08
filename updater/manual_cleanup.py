from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

STATE_FILE = Path("/state/update-state.json")
SUCCESS_MESSAGE = "cleanup completed"
FAILURE_MESSAGE = "cleanup requires manual intervention"


class ManualCleanupError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def build_store() -> Any:
    from updater.store import FileStateStore

    return FileStateStore(STATE_FILE)


def build_platform() -> Any:
    from updater.platform import DockerPlatform

    return DockerPlatform()


def require_pending_cleanup_task(state: Any) -> Any:
    from updater.types import CleanupStatus, Stage

    task = state.task
    if task is None:
        raise ManualCleanupError("cleanup_not_pending")
    if task.stage is not Stage.SUCCEEDED or task.cleanup is not CleanupStatus.PENDING:
        raise ManualCleanupError("cleanup_not_pending")
    return task


def run_pending_cleanup(
    store: Any,
    platform: Any,
) -> Any:
    from updater.manager import UpdateConflict, UpdateManager

    state = store.load()
    require_pending_cleanup_task(state)
    try:
        return UpdateManager(store, platform).complete_pending_cleanup()
    except UpdateConflict as error:
        raise ManualCleanupError(error.code) from error


def main() -> int:
    try:
        run_pending_cleanup(build_store(), build_platform())
    except Exception:  # noqa: BLE001
        print(FAILURE_MESSAGE, file=sys.stderr)
        return 1
    print(SUCCESS_MESSAGE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
