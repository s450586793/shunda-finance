from __future__ import annotations

import hmac
import json
import re
import sys
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Any
from uuid import UUID

from updater.config import UpdaterConfig
from updater.manager import UpdateConflict, UpdateManager
from updater.platform import SafeOperationError
from updater.store import StateStoreError
from updater.types import StatusView, TaskView, validate_version

MAX_BODY_BYTES = 4 * 1024
MAX_RESPONSE_BYTES = 16 * 1024
ERROR_DOCUMENTS: dict[int, dict[str, dict[str, str]]] = {
    400: {"error": {"code": "invalid_request", "message": "Invalid request."}},
    401: {"error": {"code": "unauthorized", "message": "Unauthorized."}},
    404: {"error": {"code": "not_found", "message": "Not found."}},
    405: {"error": {"code": "method_not_allowed", "message": "Method not allowed."}},
    409: {"error": {"code": "update_conflict", "message": "Update unavailable."}},
    503: {"error": {"code": "service_unavailable", "message": "Service unavailable."}},
}
_CONTENT_LENGTH_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_EXPECTED_CLIENT_DISCONNECTS = (
    BrokenPipeError,
    ConnectionResetError,
    TimeoutError,
)


class _InvalidRequest(ValueError):
    pass


class _UpdaterHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        if isinstance(sys.exception(), _EXPECTED_CLIENT_DISCONNECTS):
            return
        super().handle_error(request, client_address)


class _OperationRunner:
    def __init__(self, manager: UpdateManager):
        self._manager = manager
        self._lock = Lock()
        self._worker: Thread | None = None
        self._task: TaskView | None = None

    def start(self, target_version: str, task_id: UUID) -> TaskView:
        with self._lock:
            persisted_task = self._manager.status().task
            if persisted_task is not None and persisted_task.id == task_id:
                if persisted_task.to_version != target_version:
                    raise UpdateConflict("task_id_conflict")
                self._task = persisted_task
                return persisted_task
            if self._worker is not None and self._worker.is_alive():
                if (
                    self._task is not None
                    and self._task.id == task_id
                    and self._task.to_version == target_version
                ):
                    return self._task
                raise UpdateConflict("task_active")
            task, execute = self._manager.start(target_version, task_id=task_id)
            worker = Thread(target=self._run, args=(execute,), daemon=True)
            self._task = task
            self._worker = worker
            worker.start()
            return task

    def _run(self, execute: Callable[[], None]) -> None:
        try:
            execute()
        except Exception:  # noqa: BLE001
            # Manager state captures safe task failures; no internal details leave this process.
            return
        finally:
            with self._lock:
                self._worker = None


def authorize(header: str | None, expected_token: str) -> bool:
    prefix = "Bearer "
    if not isinstance(header, str) or not header.startswith(prefix):
        return False
    try:
        return hmac.compare_digest(
            header[len(prefix) :].encode("utf-8"), expected_token.encode("utf-8")
        )
    except UnicodeError:
        return False


def build_server(config: UpdaterConfig, manager: UpdateManager) -> ThreadingHTTPServer:
    operation_runner = _OperationRunner(manager)

    class UpdaterRequestHandler(BaseHTTPRequestHandler):
        def handle_one_request(self) -> None:
            try:
                self.raw_requestline = self.rfile.readline(65_537)
                if len(self.raw_requestline) > 65_536:
                    self.requestline = ""
                    self.request_version = "HTTP/1.0"
                    self.command = ""
                    self._error(400)
                    return
                if not self.raw_requestline:
                    self.close_connection = True
                    return
                if not self.parse_request():
                    return
                self._dispatch()
                self.wfile.flush()
            except _EXPECTED_CLIENT_DISCONNECTS:
                self.close_connection = True

        def send_error(
            self, _code: int, _message: str | None = None, _explain: str | None = None
        ) -> None:
            self.request_version = "HTTP/1.0"
            self._error(400)

        def _dispatch(self) -> None:
            if self.path == "/health":
                if self.command == "GET":
                    self._respond(200, {"status": "ok"})
                else:
                    self._error(405)
                return
            if not self._is_authorized():
                self._error(401)
                return
            routes: dict[tuple[str, str], Callable[[], None]] = {
                ("GET", "/v1/status"): self._handle_status,
                ("POST", "/v1/check"): self._handle_check,
                ("POST", "/v1/update"): self._handle_start,
            }
            route = routes.get((self.command, self.path))
            if route is None:
                self._error(
                    405
                    if self.path in {"/health", "/v1/status", "/v1/check", "/v1/update"}
                    else 404
                )
                return
            try:
                route()
            except _InvalidRequest:
                self._error(400)
            except UpdateConflict:
                self._error(409)
            except _EXPECTED_CLIENT_DISCONNECTS:
                raise
            except (
                SafeOperationError,
                StateStoreError,
                OSError,
                RuntimeError,
                ValueError,
            ):
                self._error(503)
            except Exception:  # noqa: BLE001
                self._error(503)

        def _is_authorized(self) -> bool:
            headers = self.headers.get_all("Authorization")
            return (
                headers is not None
                and len(headers) == 1
                and authorize(headers[0], config.token)
            )

        def _handle_status(self) -> None:
            self._respond(200, _view_payload(manager.status()))

        def _handle_check(self) -> None:
            self._read_json(set())
            self._respond(200, _view_payload(manager.check()))

        def _handle_start(self) -> None:
            payload = self._read_json({"target_version", "task_id"})
            target_version = payload["target_version"]
            task_id_value = payload["task_id"]
            if type(target_version) is not str or type(task_id_value) is not str:
                raise _InvalidRequest("invalid_target_version")
            try:
                validate_version(target_version)
                task_id = UUID(task_id_value)
            except ValueError as error:
                raise _InvalidRequest("invalid_start_request") from error
            if str(task_id) != task_id_value:
                raise _InvalidRequest("invalid_task_id")
            self._respond(
                202, _view_payload(operation_runner.start(target_version, task_id))
            )

        def _read_json(self, expected_keys: set[str]) -> dict[str, Any]:
            if self.headers.get_all("Transfer-Encoding") is not None:
                raise _InvalidRequest("invalid_transfer_encoding")
            content_types = self.headers.get_all("Content-Type")
            if content_types != ["application/json"]:
                raise _InvalidRequest("invalid_content_type")
            lengths = self.headers.get_all("Content-Length")
            if (
                lengths is None
                or len(lengths) != 1
                or _CONTENT_LENGTH_PATTERN.fullmatch(lengths[0]) is None
            ):
                raise _InvalidRequest("invalid_content_length")
            length = int(lengths[0])
            if length > MAX_BODY_BYTES:
                raise _InvalidRequest("request_too_large")
            body = self.rfile.read(length)
            if len(body) != length:
                raise _InvalidRequest("truncated_request")
            return _exact_json_object(body, expected_keys)

        def _error(self, status: int) -> None:
            self._respond(status, ERROR_DOCUMENTS[status])

        def _respond(self, status: int, payload: dict[str, Any]) -> None:
            try:
                body = json.dumps(
                    payload, separators=(",", ":"), ensure_ascii=True
                ).encode("ascii")
            except (TypeError, ValueError):
                status = 503
                body = json.dumps(
                    ERROR_DOCUMENTS[status], separators=(",", ":")
                ).encode()
            if len(body) > MAX_RESPONSE_BYTES:
                status = 503
                body = json.dumps(
                    ERROR_DOCUMENTS[status], separators=(",", ":")
                ).encode()
            if self.request_version == "HTTP/0.9":
                self.request_version = "HTTP/1.0"
            self.send_response_only(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return _UpdaterHTTPServer(config.listen, UpdaterRequestHandler)


def _view_payload(view: StatusView | TaskView) -> dict[str, Any]:
    if not isinstance(view, (StatusView, TaskView)):
        raise TypeError("invalid_view")
    return view.to_dict()


def _exact_json_object(payload: bytes, expected_keys: set[str]) -> dict[str, Any]:
    try:
        decoded = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_no_duplicate_object,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
        raise _InvalidRequest("invalid_json") from error
    if type(decoded) is not dict or set(decoded) != expected_keys:
        raise _InvalidRequest("invalid_json")
    return decoded


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result
