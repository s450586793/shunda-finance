import io
import json
import socket
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from uuid import UUID

import pytest
from django.test import override_settings

import apps.system_update.client as updater_client
from apps.system_update.client import (
    UpdaterClient,
    UpdaterStatusView,
    UpdaterTaskView,
    UpdaterUnavailable,
)

TOKEN = "t" * 32
NOW = "2026-08-07T12:00:00+00:00"
TASK = {
    "id": "00000000-0000-0000-0000-000000000001",
    "from_version": "v0.2.0",
    "to_version": "v0.2.1",
    "stage": "checking",
    "created_at": NOW,
    "started_at": None,
    "finished_at": None,
    "backup_complete": False,
    "rolled_back": False,
    "cleanup": "not_run",
    "error_code": "",
    "error_message": "",
}
STATUS = {
    "current_version": "v0.2.0",
    "current_published_at": "2026-08-06T12:00:00+00:00",
    "latest_version": "v0.2.1",
    "latest_published_at": NOW,
    "update_available": True,
    "checked_at": NOW,
    "task": None,
}
TASK_ID = UUID(TASK["id"])


class FakeResponse:
    def __init__(self, payload, *, status=200, headers=None):
        self._stream = io.BytesIO(payload)
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def read(self, amount=-1):
        return self._stream.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.last_request = None
        self.timeout = None
        self.calls = 0

    def __call__(self, request, timeout):
        self.calls += 1
        self.last_request = request
        self.timeout = timeout
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _payload(value):
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


@contextmanager
def _http_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
        assert not thread.is_alive()


def test_client_check_uses_fixed_authenticated_json_contract_and_parses_status():
    fake = FakeTransport(FakeResponse(_payload(STATUS)))
    client = UpdaterClient("http://updater:8090", TOKEN, transport=fake)

    status = client.check()

    assert status == UpdaterStatusView(
        current_version="v0.2.0",
        current_published_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
        latest_version="v0.2.1",
        latest_published_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
        update_available=True,
        checked_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
        task=None,
    )
    assert fake.last_request.full_url == "http://updater:8090/v1/check"
    assert fake.last_request.get_method() == "POST"
    assert fake.last_request.data == b"{}"
    assert fake.last_request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert fake.last_request.get_header("Content-type") == "application/json"
    assert 0 < fake.timeout < 10


def test_client_status_and_start_return_strict_public_views():
    status_transport = FakeTransport(FakeResponse(_payload({**STATUS, "task": TASK})))
    client = UpdaterClient("http://updater:8090", TOKEN, transport=status_transport)

    status = client.status()
    assert isinstance(status.task, UpdaterTaskView)
    assert status.task.id == UUID(TASK["id"])
    assert status_transport.last_request.full_url == "http://updater:8090/v1/status"
    assert status_transport.last_request.data is None

    start_transport = FakeTransport(FakeResponse(_payload(TASK), status=202))
    started = UpdaterClient(
        "http://updater:8090", TOKEN, transport=start_transport
    ).start("v0.2.1", TASK_ID)
    assert started.to_version == "v0.2.1"
    assert start_transport.last_request.full_url == "http://updater:8090/v1/update"
    assert start_transport.last_request.data == (
        b'{"target_version":"v0.2.1",'
        b'"task_id":"00000000-0000-0000-0000-000000000001"}'
    )


def test_client_accepts_nullable_current_publication_without_private_fields():
    payload = {**STATUS, "current_published_at": None}
    client = UpdaterClient(
        "http://updater:8090",
        TOKEN,
        transport=FakeTransport(FakeResponse(_payload(payload))),
    )

    status = client.status()

    assert status.current_published_at is None
    assert not hasattr(status, "repository")
    assert not hasattr(status, "digest")
    assert not hasattr(status, "image_id")


def test_client_start_rejects_a_task_response_with_different_correlation_id():
    mismatched = {**TASK, "id": "00000000-0000-0000-0000-000000000002"}
    client = UpdaterClient(
        "http://updater:8090",
        TOKEN,
        transport=FakeTransport(FakeResponse(_payload(mismatched), status=202)),
    )

    with pytest.raises(UpdaterUnavailable, match="invalid_response"):
        client.start("v0.2.1", TASK_ID)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"current_version":"v0.2.0","current_version":"v0.2.0"}',
        _payload({**STATUS, "unexpected": True}),
        _payload({**STATUS, "update_available": 1}),
        _payload({**STATUS, "latest_version": "latest"}),
        _payload({key: value for key, value in STATUS.items() if key != "current_published_at"}),
        _payload({**STATUS, "current_published_at": "2026-08-06T12:00:00+08:00"}),
        _payload({**STATUS, "checked_at": "2026-08-07T12:00:00+08:00"}),
        _payload({**STATUS, "task": {**TASK, "stage": "secret_stage"}}),
        _payload({**STATUS, "task": {**TASK, "error_code": "private_error"}}),
        b'{"current_version":"v0.2.0"} trailing',
        b'{"current_version":NaN}',
    ],
)
def test_client_rejects_non_exact_or_non_public_json_schema(payload):
    client = UpdaterClient("http://updater:8090", TOKEN, transport=FakeTransport(FakeResponse(payload)))

    with pytest.raises(UpdaterUnavailable, match="invalid_response"):
        client.status()


def test_client_rejects_oversized_or_truncated_response():
    oversized = b"{" + b"x" * (64 * 1024) + b"}"
    for payload, headers in (
        (oversized, {"Content-Type": "application/json"}),
        (_payload(STATUS), {"Content-Type": "application/json", "Content-Length": "999"}),
    ):
        client = UpdaterClient(
            "http://updater:8090", TOKEN, transport=FakeTransport(FakeResponse(payload, headers=headers))
        )
        with pytest.raises(UpdaterUnavailable, match="invalid_response"):
            client.status()


def test_client_rejects_non_json_response_without_exposing_body():
    secret = b"internal response secret"
    client = UpdaterClient(
        "http://updater:8090",
        TOKEN,
        transport=FakeTransport(FakeResponse(secret, headers={"Content-Type": "text/plain"})),
    )

    with pytest.raises(UpdaterUnavailable, match="invalid_response") as exc_info:
        client.status()

    assert secret.decode() not in str(exc_info.value)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (TimeoutError("private timeout"), "network_error"),
        (ConnectionRefusedError("private refusal"), "network_error"),
        (HTTPError("http://private", 401, "Unauthorized", {}, io.BytesIO(b"secret")), "unauthorized"),
        (HTTPError("http://private", 409, "Conflict", {}, io.BytesIO(b"secret")), "update_conflict"),
        (HTTPError("http://private", 503, "Down", {}, io.BytesIO(b"secret")), "service_unavailable"),
    ],
)
def test_client_maps_transport_failures_to_safe_codes_without_secrets(error, code):
    client = UpdaterClient("http://updater:8090", TOKEN, transport=FakeTransport(error))

    with pytest.raises(UpdaterUnavailable) as exc_info:
        client.check()

    assert exc_info.value.code == code
    assert "private" not in str(exc_info.value)
    assert "secret" not in str(exc_info.value)
    assert TOKEN not in str(exc_info.value)


def test_client_maps_malformed_http_stream_errors_to_safe_code():
    client = UpdaterClient(
        "http://updater:8090",
        TOKEN,
        transport=FakeTransport(IncompleteRead(b"private body", 10)),
    )

    with pytest.raises(UpdaterUnavailable, match="network_error") as exc_info:
        client.status()

    assert "private body" not in str(exc_info.value)


def test_client_disables_environment_proxy_before_sending_bearer_token(monkeypatch):
    target_requests = []
    proxy_requests = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            target_requests.append(dict(self.headers))
            body = _payload(STATUS)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            proxy_requests.append(dict(self.headers))
            self.send_error(502)

        def log_message(self, format, *args):
            return

    with _http_server(TargetHandler) as target, _http_server(ProxyHandler) as proxy:
        target_port = target.server_address[1]
        proxy_port = proxy.server_address[1]
        original_getaddrinfo = socket.getaddrinfo

        def resolve(host, port, *args, **kwargs):
            if host == "updater.test":
                return original_getaddrinfo("127.0.0.1", target_port, *args, **kwargs)
            return original_getaddrinfo(host, port, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        monkeypatch.setattr(updater_client, "UPDATER_URL", f"http://updater.test:{target_port}")
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
        monkeypatch.setenv("no_proxy", "")
        monkeypatch.setenv("NO_PROXY", "")

        status = UpdaterClient(updater_client.UPDATER_URL, TOKEN).status()

    assert status.current_version == "v0.2.0"
    assert len(target_requests) == 1
    assert target_requests[0]["Authorization"] == f"Bearer {TOKEN}"
    assert proxy_requests == []


def test_client_enforces_one_total_deadline_across_slow_response_chunks(monkeypatch):
    class SlowResponse(FakeResponse):
        def __init__(self, payload):
            super().__init__(payload)
            self.timeouts = []

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def read(self, amount=-1):
            time.sleep(0.015)
            return super().read(min(amount, 40))

    monkeypatch.setattr(updater_client, "REQUEST_TIMEOUT_SECONDS", 0.02)
    response = SlowResponse(_payload(STATUS))
    client = UpdaterClient("http://updater:8090", TOKEN, transport=FakeTransport(response))

    with pytest.raises(UpdaterUnavailable, match="network_error"):
        client.status()

    assert response.timeouts
    assert all(0 < timeout <= 0.02 for timeout in response.timeouts)


def test_client_fails_closed_when_response_timeout_cannot_be_set():
    class NoTimeoutResponse:
        status = 200

        def __init__(self):
            self.headers = {"Content-Type": "application/json"}
            self.stream = io.BytesIO(_payload(STATUS))

        def read(self, amount=-1):
            return self.stream.read(amount)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    client = UpdaterClient(
        "http://updater:8090", TOKEN, transport=FakeTransport(NoTimeoutResponse())
    )

    with pytest.raises(UpdaterUnavailable, match="network_error"):
        client.status()


@pytest.mark.parametrize(
    ("url", "token"),
    [
        ("http://updater:8090/path", TOKEN),
        ("http://token@updater:8090", TOKEN),
        ("http://updater:8090#fragment", TOKEN),
        ("http://updater:8090", " " * 32),
        ("http://updater:8090", "short"),
    ],
)
def test_client_rejects_non_fixed_endpoint_or_weak_token(url, token):
    with pytest.raises(ValueError, match="updater configuration"):
        UpdaterClient(url, token)


def test_client_rejects_invalid_target_without_sending_a_post():
    fake = FakeTransport(FakeResponse(_payload(TASK), status=202))
    client = UpdaterClient("http://updater:8090", TOKEN, transport=fake)

    with pytest.raises(UpdaterUnavailable, match="invalid_request"):
        client.start("latest", TASK_ID)

    assert fake.last_request is None


def test_client_never_retries_start_after_a_network_failure():
    fake = FakeTransport(ConnectionRefusedError("private refusal"))
    client = UpdaterClient("http://updater:8090", TOKEN, transport=fake)

    with pytest.raises(UpdaterUnavailable, match="network_error"):
        client.start("v0.2.1", TASK_ID)

    assert fake.calls == 1


@override_settings(SHUNDA_UPDATER_URL="http://updater:8090", SHUNDA_UPDATER_TOKEN=TOKEN)
def test_client_from_settings_uses_only_fixed_django_settings():
    fake = FakeTransport(FakeResponse(_payload(STATUS)))

    status = UpdaterClient.from_settings(transport=fake).status()

    assert status.current_version == "v0.2.0"
    assert fake.last_request.full_url == "http://updater:8090/v1/status"
