from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from django.conf import settings

UPDATER_URL = "http://updater:8090"
REQUEST_TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 64 * 1024
RESPONSE_READ_CHUNK_BYTES = 4 * 1024
_VERSION_PATTERN = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?\+00:00$"
)
_TASK_FIELDS = frozenset(
    {
        "id",
        "from_version",
        "to_version",
        "stage",
        "created_at",
        "started_at",
        "finished_at",
        "backup_complete",
        "rolled_back",
        "cleanup",
        "error_code",
        "error_message",
    }
)
_STATUS_FIELDS = frozenset(
    {
        "current_version",
        "current_published_at",
        "latest_version",
        "latest_published_at",
        "update_available",
        "checked_at",
        "task",
    }
)
_STAGES = frozenset(
    {
        "checking",
        "backing_up",
        "pulling",
        "stopping_web",
        "migrating",
        "starting_web",
        "checking_health",
        "stabilizing",
        "persisting_version",
        "cleaning",
        "rolling_back",
        "succeeded",
        "failed",
        "manual_intervention",
    }
)
_CLEANUP_STATUSES = frozenset({"not_run", "complete", "pending"})
_PUBLIC_ERROR_MESSAGES = {
    "": "",
    "backup_failed": "备份失败，请联系管理员。",
    "pull_failed": "下载升级版本失败，请联系管理员。",
    "migration_failed": "升级失败，请联系管理员。",
    "health_check_failed": "升级后检查失败，请联系管理员。",
    "rollback_failed": "升级失败，需要人工处理。",
    "update_failed": "升级失败，请联系管理员。",
}


class UpdaterUnavailable(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class UpdaterTaskView:
    id: UUID
    from_version: str
    to_version: str
    stage: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    backup_complete: bool
    rolled_back: bool
    cleanup: str
    error_code: str
    error_message: str


@dataclass(frozen=True)
class UpdaterStatusView:
    current_version: str
    current_published_at: datetime | None
    latest_version: str | None
    latest_published_at: datetime | None
    update_available: bool
    checked_at: datetime | None
    task: UpdaterTaskView | None


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _open_without_redirect(request: Request, timeout: float):
    return build_opener(ProxyHandler({}), _NoRedirectHandler()).open(
        request, timeout=timeout
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _reject_non_json_number(value: str):
    raise ValueError("non_json_number")


def _is_valid_configuration(base_url: object, token: object) -> bool:
    return (
        base_url == UPDATER_URL
        and isinstance(token, str)
        and bool(token.strip())
        and len(token.encode("utf-8")) >= 32
    )


def _require_exact_object(payload: Any, expected_fields: frozenset[str]) -> dict[str, Any]:
    if type(payload) is not dict or set(payload) != expected_fields:
        raise ValueError("invalid_schema")
    return payload


def _require_version(value: Any) -> str:
    if not isinstance(value, str) or _VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid_version")
    return value


def _require_timestamp(value: Any, *, nullable: bool) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or _UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid_timestamp")
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() != timedelta(0):
        raise ValueError("invalid_timestamp")
    return timestamp


def _require_string(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("invalid_string")
    return value


def _parse_task(payload: Any) -> UpdaterTaskView:
    data = _require_exact_object(payload, _TASK_FIELDS)
    try:
        task_id = UUID(_require_string(data["id"]))
    except (ValueError, AttributeError) as error:
        raise ValueError("invalid_task_id") from error
    if str(task_id) != data["id"]:
        raise ValueError("invalid_task_id")
    stage = _require_string(data["stage"])
    cleanup = _require_string(data["cleanup"])
    error_code = _require_string(data["error_code"])
    error_message = _require_string(data["error_message"])
    if (
        stage not in _STAGES
        or cleanup not in _CLEANUP_STATUSES
        or _PUBLIC_ERROR_MESSAGES.get(error_code) != error_message
    ):
        raise ValueError("invalid_task_value")
    return UpdaterTaskView(
        id=task_id,
        from_version=_require_version(data["from_version"]),
        to_version=_require_version(data["to_version"]),
        stage=stage,
        created_at=_require_timestamp(data["created_at"], nullable=False),
        started_at=_require_timestamp(data["started_at"], nullable=True),
        finished_at=_require_timestamp(data["finished_at"], nullable=True),
        backup_complete=data["backup_complete"] if type(data["backup_complete"]) is bool else _raise_invalid(),
        rolled_back=data["rolled_back"] if type(data["rolled_back"]) is bool else _raise_invalid(),
        cleanup=cleanup,
        error_code=error_code,
        error_message=error_message,
    )


def _raise_invalid():
    raise ValueError("invalid_boolean")


def _parse_status(payload: Any) -> UpdaterStatusView:
    data = _require_exact_object(payload, _STATUS_FIELDS)
    latest_version = data["latest_version"]
    if latest_version is not None:
        latest_version = _require_version(latest_version)
    task = data["task"]
    if task is not None:
        task = _parse_task(task)
    if type(data["update_available"]) is not bool:
        raise ValueError("invalid_boolean")
    return UpdaterStatusView(
        current_version=_require_version(data["current_version"]),
        current_published_at=_require_timestamp(
            data["current_published_at"], nullable=True
        ),
        latest_version=latest_version,
        latest_published_at=_require_timestamp(data["latest_published_at"], nullable=True),
        update_available=data["update_available"],
        checked_at=_require_timestamp(data["checked_at"], nullable=True),
        task=task,
    )


class UpdaterClient:
    def __init__(self, base_url: str, token: str, *, transport: Callable | None = None):
        if not _is_valid_configuration(base_url, token):
            raise ValueError("invalid updater configuration")
        self._base_url = base_url
        self._token = token
        self._transport = transport or _open_without_redirect

    @classmethod
    def from_settings(cls, *, transport: Callable | None = None) -> UpdaterClient:
        return cls(
            settings.SHUNDA_UPDATER_URL,
            settings.SHUNDA_UPDATER_TOKEN,
            transport=transport,
        )

    def status(self) -> UpdaterStatusView:
        return self._parse_response(
            _parse_status, self._request("GET", "/v1/status", None, 200)
        )

    def check(self) -> UpdaterStatusView:
        return self._parse_response(
            _parse_status, self._request("POST", "/v1/check", {}, 200)
        )

    def start(self, target_version: str, task_id: UUID) -> UpdaterTaskView:
        try:
            _require_version(target_version)
            if not isinstance(task_id, UUID):
                raise TypeError("invalid_task_id")
        except (TypeError, ValueError) as error:
            raise UpdaterUnavailable("invalid_request") from error
        task = self._parse_response(
            _parse_task,
            self._request(
                "POST",
                "/v1/update",
                {"target_version": target_version, "task_id": str(task_id)},
                202,
            ),
        )
        if task.id != task_id or task.to_version != target_version:
            raise UpdaterUnavailable("invalid_response")
        return task

    @staticmethod
    def _parse_response(parser, payload):
        try:
            return parser(payload)
        except (TypeError, ValueError, UnicodeError):
            raise UpdaterUnavailable("invalid_response") from None

    def _request(self, method: str, path: str, body: dict[str, str] | None, expected_status: int):
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("ascii")
        headers = {"Authorization": f"Bearer {self._token}", "Connection": "close"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self._base_url}{path}", data=data, headers=headers, method=method)
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        try:
            response = self._transport(request, _remaining_seconds(deadline))
            with response:
                _remaining_seconds(deadline)
                status = getattr(response, "status", None)
                if status != expected_status:
                    raise UpdaterUnavailable("unexpected_response")
                payload = self._read_response(response, deadline)
        except UpdaterUnavailable:
            raise
        except HTTPError as error:
            codes = {401: "unauthorized", 409: "update_conflict", 503: "service_unavailable"}
            raise UpdaterUnavailable(codes.get(error.code, "unexpected_response")) from None
        except (HTTPException, OSError, TimeoutError, URLError):
            raise UpdaterUnavailable("network_error") from None
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise UpdaterUnavailable("invalid_response") from None
        return payload

    @staticmethod
    def _read_response(response, deadline: float) -> Any:
        content_type = response.headers.get("Content-Type", "")
        if content_type != "application/json":
            raise ValueError("invalid_content_type")
        content_length = response.headers.get("Content-Length")
        if content_length is not None and (
            not content_length.isdecimal()
            or int(content_length) > MAX_RESPONSE_BYTES
        ):
            raise ValueError("invalid_content_length")
        payload = bytearray()
        while len(payload) <= MAX_RESPONSE_BYTES:
            _set_response_timeout(response, _remaining_seconds(deadline))
            chunk = response.read(
                min(RESPONSE_READ_CHUNK_BYTES, MAX_RESPONSE_BYTES + 1 - len(payload))
            )
            _remaining_seconds(deadline)
            if not chunk:
                break
            payload.extend(chunk)
            if content_length is not None and len(payload) == int(content_length):
                break
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("response_too_large")
        if content_length is not None and len(payload) != int(content_length):
            raise ValueError("truncated_response")
        return json.loads(
            bytes(payload).decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_number,
        )


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("updater request deadline exceeded")
    return remaining


def _set_response_timeout(response, timeout: float) -> None:
    candidates = [response]
    visited = {id(response)}
    while candidates:
        candidate = candidates.pop()
        settimeout = getattr(candidate, "settimeout", None)
        if callable(settimeout):
            settimeout(timeout)
            return
        for attribute in ("fp", "raw", "_sock", "sock"):
            nested = getattr(candidate, attribute, None)
            if nested is not None and id(nested) not in visited:
                visited.add(id(nested))
                candidates.append(nested)
    raise TimeoutError("updater response timeout is unavailable")
