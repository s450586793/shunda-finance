from __future__ import annotations

import http.client
import json
import socket
import struct
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread
from uuid import UUID

import pytest

import updater.http_server as http_server  # noqa: PLR0402
from updater.config import UpdaterConfig
from updater.manager import UpdateConflict
from updater.platform import PlatformConfig, SafeOperationError
from updater.types import CleanupStatus, Stage, StatusView, TaskView

TOKEN = "t" * 32
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
TASK = TaskView(
    id=UUID("00000000-0000-0000-0000-000000000001"),
    from_version="v0.2.0",
    to_version="v0.2.1",
    stage=Stage.CHECKING,
    created_at=NOW,
    started_at=None,
    finished_at=None,
    backup_complete=False,
    rolled_back=False,
    cleanup=CleanupStatus.NOT_RUN,
    error_code="",
    error_message="",
)
STATUS = StatusView(
    current_version="v0.2.0",
    current_published_at=NOW,
    latest_version="v0.2.1",
    latest_published_at=NOW,
    update_available=True,
    checked_at=NOW,
    task=None,
)


class FakeManager:
    def __init__(self):
        self.check_error: Exception | None = None
        self.status_error: Exception | None = None
        self.start_error: Exception | None = None
        self.check_calls = 0
        self.start_calls = 0
        self.execute_calls = 0
        self.start_task_ids: list[UUID] = []
        self.started = Event()
        self.release = Event()
        self.current_task: TaskView | None = None
        self._active = False
        self._lock = Lock()

    def check(self) -> StatusView:
        self.check_calls += 1
        if self.check_error is not None:
            raise self.check_error
        return STATUS

    def status(self) -> StatusView:
        if self.status_error is not None:
            raise self.status_error
        return replace(STATUS, task=self.current_task)

    def start(
        self, target_version: str, *, task_id: UUID | None = None
    ) -> tuple[TaskView, object]:
        with self._lock:
            self.start_calls += 1
            if task_id is None:
                raise AssertionError("HTTP start must provide a task ID")
            self.start_task_ids.append(task_id)
            if self.start_error is not None:
                raise self.start_error
            if self._active:
                raise UpdateConflict("task_active")
            self._active = True
            self.current_task = replace(TASK, id=task_id, to_version=target_version)

        def execute() -> None:
            self.execute_calls += 1
            self.started.set()
            assert self.release.wait(timeout=1)

        return self.current_task, execute


@pytest.fixture
def manager() -> FakeManager:
    return FakeManager()


@pytest.fixture
def server(manager: FakeManager):
    config = UpdaterConfig(
        token=TOKEN,
        listen=("127.0.0.1", 0),
        state_file=Path("/state/update-state.json"),
        platform=PlatformConfig(),
    )
    instance = http_server.build_server(config, manager)
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    manager.release.set()
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=1)
    assert not thread.is_alive()


def request(
    server,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: list[tuple[str, str]] | None = None,
    content_length: int | None = None,
) -> tuple[int, dict[str, str], dict[str, object]]:
    host, port = server.server_address[:2]
    request_headers = list(headers or [])
    if content_length is not None:
        request_headers.append(("Content-Length", str(content_length)))
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}:{port}", "Connection: close"]
    lines.extend(f"{name}: {value}" for name, value in request_headers)
    payload = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
    return raw_request(server, payload)


def raw_request(
    server, payload: bytes
) -> tuple[int, dict[str, str], dict[str, object]]:
    host, port = server.server_address[:2]
    with socket.create_connection((host, port), timeout=1) as connection:
        connection.sendall(payload)
        connection.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(connection)
        response.begin()
        response_body = response.read()
        return response.status, dict(response.getheaders()), json.loads(response_body)


def bearer_headers() -> list[tuple[str, str]]:
    return [("Authorization", f"Bearer {TOKEN}")]


def json_headers() -> list[tuple[str, str]]:
    return [*bearer_headers(), ("Content-Type", "application/json")]


def update_body(
    task_id: UUID = TASK.id, target_version: str = "v0.2.1"
) -> bytes:
    return json.dumps(
        {"target_version": target_version, "task_id": str(task_id)},
        separators=(",", ":"),
    ).encode()


def test_health_is_the_only_public_get_route(server):
    status, headers, payload = request(server, "GET", "/health")

    assert status == 200
    assert payload == {"status": "ok"}
    assert headers["Content-Type"] == "application/json"
    assert headers["Cache-Control"] == "no-store"
    assert "Server" not in headers

    status, _headers, _payload = request(server, "GET", "/v1/status")

    assert status == 401


def test_health_rejects_wrong_method_and_non_health_routes_require_exact_bearer(server):
    status, _headers, _payload = request(server, "POST", "/health")
    assert status == 405

    status, _headers, _payload = request(
        server,
        "POST",
        "/health",
        headers=bearer_headers(),
        content_length=0,
    )
    assert status == 405

    status, _headers, _payload = request(
        server,
        "GET",
        "/v1/status",
        headers=[("Authorization", f"Bearer  {TOKEN}")],
    )
    assert status == 401


@pytest.mark.parametrize(
    ("method", "path", "headers", "expected_status"),
    [
        ("OPTIONS", "/v1/status", [], 401),
        ("OPTIONS", "/v1/status", [("Authorization", "Bearer wrong")], 401),
        ("FROB", "/v1/status", [], 401),
        ("OPTIONS", "/missing", [], 401),
        ("FROB", "/v1/status", bearer_headers(), 405),
        ("OPTIONS", "/missing", bearer_headers(), 404),
        ("POST", "/health", [], 405),
    ],
)
def test_unknown_verbs_and_routes_are_authenticated_before_fixed_error_response(
    server, method, path, headers, expected_status
):
    status, response_headers, payload = request(server, method, path, headers=headers)

    assert status == expected_status
    assert payload == http_server.ERROR_DOCUMENTS[expected_status]
    assert response_headers["Content-Type"] == "application/json"
    assert response_headers["Cache-Control"] == "no-store"
    assert "Server" not in response_headers


@pytest.mark.parametrize(
    "payload",
    [
        b"INVALID\r\n",
        b"GET /v1/status HTTP/9.9\r\nHost: local\r\n\r\n",
        b"GET /" + b"x" * 65_537 + b" HTTP/1.1\r\n\r\n",
    ],
)
def test_request_line_parse_errors_return_fixed_bounded_json_without_server_banner(
    server, payload
):
    status, headers, response = raw_request(server, payload)

    assert status == 400
    assert response == http_server.ERROR_DOCUMENTS[400]
    assert headers["Content-Type"] == "application/json"
    assert headers["Cache-Control"] == "no-store"
    assert "Server" not in headers


def test_authorization_uses_constant_time_comparator(monkeypatch, server):
    calls: list[tuple[bytes, bytes]] = []
    real_compare = http_server.hmac.compare_digest

    def compare(actual: bytes, expected: bytes) -> bool:
        calls.append((actual, expected))
        return real_compare(actual, expected)

    monkeypatch.setattr(http_server.hmac, "compare_digest", compare)

    status, _headers, payload = request(
        server, "GET", "/v1/status", headers=bearer_headers()
    )

    assert status == 200
    assert payload == STATUS.to_dict()
    assert calls == [(TOKEN.encode(), TOKEN.encode())]


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"null",
        b'{"unknown": true}',
        b"{} {}",
        b'{"target_version":"v0.2.1","target_version":"v0.2.1"}',
    ],
)
def test_mutating_routes_reject_non_exact_json_objects(server, body):
    status, _headers, payload = request(
        server,
        "POST",
        "/v1/check",
        body=body,
        headers=json_headers(),
        content_length=len(body),
    )

    assert status == 400
    assert payload == http_server.ERROR_DOCUMENTS[400]


def test_mutating_routes_reject_missing_invalid_or_oversized_bodies(server):
    status, _headers, _payload = request(
        server, "POST", "/v1/check", headers=json_headers()
    )
    assert status == 400

    status, _headers, _payload = request(
        server,
        "POST",
        "/v1/check",
        body=b"{}",
        headers=[
            *bearer_headers(),
            ("Content-Type", "application/json; charset=utf-8"),
        ],
        content_length=2,
    )
    assert status == 400

    body = b"{" + b"x" * (http_server.MAX_BODY_BYTES + 1) + b"}"
    status, _headers, _payload = request(
        server,
        "POST",
        "/v1/check",
        body=body,
        headers=json_headers(),
        content_length=len(body),
    )
    assert status == 400


def test_mutating_routes_reject_truncated_body(server):
    body = b"{}"
    status, _headers, payload = request(
        server,
        "POST",
        "/v1/check",
        body=body,
        headers=json_headers(),
        content_length=len(body) + 1,
    )

    assert status == 400
    assert payload == http_server.ERROR_DOCUMENTS[400]


def test_check_and_status_serialize_only_public_views(server, manager):
    status, _headers, payload = request(
        server,
        "POST",
        "/v1/check",
        body=b"{}",
        headers=json_headers(),
        content_length=2,
    )

    assert status == 200
    assert payload == STATUS.to_dict()
    assert manager.check_calls == 1

    status, _headers, payload = request(
        server, "GET", "/v1/status", headers=bearer_headers()
    )
    assert status == 200
    assert payload == STATUS.to_dict()


def test_operational_and_conflict_errors_have_fixed_safe_responses(server, manager):
    manager.check_error = SafeOperationError("pull_failed")
    status, _headers, payload = request(
        server,
        "POST",
        "/v1/check",
        body=b"{}",
        headers=json_headers(),
        content_length=2,
    )
    assert status == 503
    assert payload == http_server.ERROR_DOCUMENTS[503]

    manager.check_error = None
    manager.start_error = UpdateConflict("task_active")
    body = update_body()
    status, _headers, payload = request(
        server,
        "POST",
        "/v1/update",
        body=body,
        headers=json_headers(),
        content_length=len(body),
    )
    assert status == 409
    assert payload == http_server.ERROR_DOCUMENTS[409]


@pytest.mark.parametrize(
    "body",
    [
        b'{"target_version":"v0.2.1"}',
        b'{"task_id":"00000000-0000-0000-0000-000000000001"}',
        b'{"target_version":"v0.2.1","task_id":1}',
        b'{"target_version":"v0.2.1","task_id":"not-a-uuid"}',
        b'{"target_version":"v0.2.1","task_id":"00000000000000000000000000000001"}',
        b'{"target_version":"v0.2.1","task_id":"AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"}',
        b'{"target_version":"v0.2.1","task_id":"00000000-0000-0000-0000-000000000001","extra":true}',
    ],
)
def test_start_requires_exact_schema_and_canonical_task_id(server, manager, body):
    status, _headers, payload = request(
        server,
        "POST",
        "/v1/update",
        body=body,
        headers=json_headers(),
        content_length=len(body),
    )

    assert status == 400
    assert payload == http_server.ERROR_DOCUMENTS[400]
    assert manager.start_calls == 0


def test_start_retry_with_same_task_id_returns_task_without_a_second_worker(
    server, manager
):
    body = update_body()
    first: list[tuple[int, dict[str, str], dict[str, object]]] = []
    thread = Thread(
        target=lambda: first.append(
            request(
                server,
                "POST",
                "/v1/update",
                body=body,
                headers=json_headers(),
                content_length=len(body),
            )
        )
    )
    thread.start()
    assert manager.started.wait(timeout=1)

    retry = request(
        server,
        "POST",
        "/v1/update",
        body=body,
        headers=json_headers(),
        content_length=len(body),
    )
    manager.release.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert first[0][0] == 202
    assert first[0][2] == TASK.to_dict()
    assert retry[0] == 202
    assert retry[2] == TASK.to_dict()
    assert manager.start_calls == 1
    assert manager.start_task_ids == [TASK.id]
    assert manager.execute_calls == 1


def test_start_retry_reuses_persisted_task_after_operation_runner_restarts(manager):
    first_runner = http_server._OperationRunner(manager)
    first = first_runner.start("v0.2.1", TASK.id)
    assert manager.started.wait(timeout=1)

    restarted_runner = http_server._OperationRunner(manager)
    retry = restarted_runner.start("v0.2.1", TASK.id)
    manager.release.set()

    assert retry == first
    assert manager.start_calls == 1
    assert manager.start_task_ids == [TASK.id]
    assert manager.execute_calls == 1


def test_start_with_different_task_id_conflicts_while_worker_is_active(
    server, manager
):
    first_body = update_body()
    status, _headers, payload = request(
        server,
        "POST",
        "/v1/update",
        body=first_body,
        headers=json_headers(),
        content_length=len(first_body),
    )
    assert status == 202
    assert payload == TASK.to_dict()
    assert manager.started.wait(timeout=1)

    second_body = update_body(UUID("00000000-0000-0000-0000-000000000002"))
    second = request(
        server,
        "POST",
        "/v1/update",
        body=second_body,
        headers=json_headers(),
        content_length=len(second_body),
    )
    manager.release.set()

    assert second[0] == 409
    assert second[2] == http_server.ERROR_DOCUMENTS[409]
    assert manager.start_calls == 1
    assert manager.start_task_ids == [TASK.id]
    assert manager.execute_calls == 1


def test_background_exception_is_not_exposed_or_allowed_to_stop_server(server, manager):
    def failing_start(
        target_version: str, *, task_id: UUID | None = None
    ) -> tuple[TaskView, object]:
        def execute() -> None:
            manager.started.set()
            raise RuntimeError(f"private failure {TOKEN} /config/compose.yml")

        return replace(TASK, id=task_id, to_version=target_version), execute

    manager.start = failing_start  # type: ignore[method-assign]
    body = update_body()
    status, _headers, payload = request(
        server,
        "POST",
        "/v1/update",
        body=body,
        headers=json_headers(),
        content_length=len(body),
    )
    assert status == 202
    assert payload == TASK.to_dict()
    assert manager.started.wait(timeout=1)

    status, _headers, payload = request(
        server, "GET", "/v1/status", headers=bearer_headers()
    )
    assert status == 200
    assert payload == STATUS.to_dict()


@pytest.mark.parametrize(
    "failure",
    [
        BrokenPipeError("private write failure /state/update-state.json"),
        ConnectionResetError("private reset failure /var/run/docker.sock"),
        TimeoutError("private timeout /config/compose.yml"),
    ],
)
def test_server_error_handler_silences_expected_disconnects(server, failure, capfd):
    try:
        raise failure
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        server.handle_error(None, ("127.0.0.1", 1))

    assert capfd.readouterr().err == ""


def test_reset_client_does_not_log_or_stop_later_requests(
    server, manager, monkeypatch, capfd
):
    status_started = Event()
    release_status = Event()
    request_finished = Event()
    handled_errors: list[BaseException | None] = []

    def blocked_status() -> StatusView:
        status_started.set()
        assert release_status.wait(timeout=1)
        return STATUS

    original_finish_request = server.finish_request
    original_handle_error = server.handle_error

    def tracked_finish_request(request_socket, client_address):
        try:
            return original_finish_request(request_socket, client_address)
        finally:
            request_finished.set()

    def tracked_handle_error(request_socket, client_address):
        handled_errors.append(sys.exc_info()[1])
        return original_handle_error(request_socket, client_address)

    monkeypatch.setattr(manager, "status", blocked_status)
    monkeypatch.setattr(server, "finish_request", tracked_finish_request)
    monkeypatch.setattr(server, "handle_error", tracked_handle_error)

    host, port = server.server_address[:2]
    connection = socket.create_connection((host, port), timeout=1)
    connection.sendall(
        (
            f"GET /v1/status HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Authorization: Bearer {TOKEN}\r\nConnection: close\r\n\r\n"
        ).encode()
    )
    assert status_started.wait(timeout=1)
    connection.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_LINGER,
        struct.pack("ii", 1, 0),
    )
    connection.close()
    release_status.set()
    assert request_finished.wait(timeout=1)

    assert handled_errors == []
    assert capfd.readouterr().err == ""
    status, _headers, payload = request(server, "GET", "/health")
    assert status == 200
    assert payload == {"status": "ok"}
