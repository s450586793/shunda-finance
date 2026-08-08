from __future__ import annotations

import os
import signal
import sys
from http.server import ThreadingHTTPServer
from threading import Lock, Thread
from types import FrameType

from updater.config import ConfigError, UpdaterConfig
from updater.http_server import build_server
from updater.manager import UpdateManager
from updater.platform import DockerPlatform, SafeOperationError
from updater.store import FileStateStore, StateStoreError


def build_manager(config: UpdaterConfig) -> UpdateManager:
    return UpdateManager(FileStateStore(config.state_file), DockerPlatform())


class _ShutdownController:
    def __init__(
        self,
        server: ThreadingHTTPServer,
        previous_handlers: dict[int, signal.Handlers],
    ):
        self._server = server
        self._previous_handlers = previous_handlers
        self._lock = Lock()
        self._thread: Thread | None = None
        self._failed = False
        self._finalizing = False

    def request_shutdown(self) -> None:
        with self._lock:
            if self._finalizing:
                return
            if self._thread is not None and (
                self._thread.is_alive() or not self._failed
            ):
                return
            self._failed = False
            self._thread = Thread(target=self._shutdown, daemon=False)
            self._thread.start()

    def join(self) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join()

    def begin_finalization(self) -> Thread | None:
        with self._lock:
            self._finalizing = True
            return self._thread

    def restore_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._previous_handlers[signal.SIGINT])
        signal.signal(signal.SIGTERM, self._previous_handlers[signal.SIGTERM])

    def _shutdown(self) -> None:
        try:
            self._server.shutdown()
        except Exception:  # noqa: BLE001
            with self._lock:
                self._failed = True
            return


def install_shutdown_handlers(server: ThreadingHTTPServer) -> _ShutdownController:
    previous_handlers = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }
    controller = _ShutdownController(server, previous_handlers)

    def shutdown(_signum: int, _frame: FrameType | None) -> None:
        controller.request_shutdown()

    try:
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
    except Exception:
        controller.restore_handlers()
        raise
    return controller


def main() -> int:
    try:
        config = UpdaterConfig.from_env(os.environ)
    except (ConfigError, TypeError, ValueError):
        print("updater startup failed", file=sys.stderr)
        return 2
    server: ThreadingHTTPServer | None = None
    shutdown_controller: _ShutdownController | None = None
    result = 0
    try:
        manager = build_manager(config)
        manager.recover()
        server = build_server(config, manager)
        shutdown_controller = install_shutdown_handlers(server)
        server.serve_forever(poll_interval=0.5)
    except (SafeOperationError, StateStoreError, OSError, RuntimeError, ValueError):
        print("updater startup failed", file=sys.stderr)
        result = 1
    finally:
        try:
            if shutdown_controller is not None:
                shutdown_thread = shutdown_controller.begin_finalization()
                if shutdown_thread is not None:
                    shutdown_thread.join()
            if server is not None:
                server.server_close()
        finally:
            if shutdown_controller is not None:
                shutdown_controller.restore_handlers()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
