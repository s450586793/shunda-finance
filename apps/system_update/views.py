import json
from datetime import datetime
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.template.response import TemplateResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import owner_required
from apps.core.audit import record_audit

from .client import UpdaterClient, UpdaterUnavailable
from .models import SystemUpdateRequest, validate_release_version

_TERMINAL_STAGES = frozenset({"succeeded", "failed", "manual_intervention"})
_UPDATER_ERROR_RESPONSES = {
    "invalid_request": (400, "invalid_request"),
    "update_conflict": (409, "update_conflict"),
}
_RECONCILABLE_START_ERRORS = frozenset(
    {
        "network_error",
        "invalid_response",
        "unexpected_response",
        "update_conflict",
        "service_unavailable",
    }
)


class _TaskIdConflict(RuntimeError):
    pass


def get_updater_client():
    return UpdaterClient.from_settings()


def _json_error(status, error):
    return JsonResponse({"error": error}, status=status)


def _updater_error(error):
    status, code = _UPDATER_ERROR_RESPONSES.get(
        error.code, (503, "updater_unavailable")
    )
    return _json_error(status, code)


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _reject_non_json_number(_value):
    raise ValueError("non_json_number")


def _strict_json_object(request, allowed):
    if request.META.get("CONTENT_TYPE") != "application/json":
        raise ValueError("invalid_content_type")
    content_length = request.META.get("CONTENT_LENGTH", "")
    try:
        if int(content_length) > settings.SYSTEM_UPDATE_MAX_REQUEST_BYTES:
            raise ValueError("body_too_large")
    except (TypeError, ValueError):
        raise ValueError("invalid_content_length") from None
    body = request.body
    if len(body) > settings.SYSTEM_UPDATE_MAX_REQUEST_BYTES:
        raise ValueError("body_too_large")
    try:
        payload = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("invalid_json") from None
    if type(payload) is not dict or set(payload) != allowed:
        raise ValueError("invalid_schema")
    return payload


def _timestamp(value):
    return value.isoformat() if isinstance(value, datetime) else None


def _parse_task_id(value):
    if type(value) is not str:
        raise ValueError("invalid_task_id")
    try:
        task_id = UUID(value)
    except (ValueError, AttributeError) as error:
        raise ValueError("invalid_task_id") from error
    if str(task_id) != value:
        raise ValueError("invalid_task_id")
    return task_id


def _task_to_dict(task):
    return {
        "id": str(task.id),
        "from_version": task.from_version,
        "to_version": task.to_version,
        "stage": task.stage,
        "created_at": _timestamp(task.created_at),
        "started_at": _timestamp(task.started_at),
        "finished_at": _timestamp(task.finished_at),
        "backup_complete": task.backup_complete,
        "rolled_back": task.rolled_back,
        "cleanup": task.cleanup,
        "error_code": task.error_code,
        "error_message": task.error_message,
    }


def _status_to_dict(status):
    return {
        "current_version": status.current_version,
        "current_published_at": _timestamp(status.current_published_at),
        "latest_version": status.latest_version,
        "latest_published_at": _timestamp(status.latest_published_at),
        "update_available": status.update_available,
        "checked_at": _timestamp(status.checked_at),
        "task": _task_to_dict(status.task) if status.task else None,
    }


def _record_terminal_audit(task):
    if task.stage not in _TERMINAL_STAGES:
        return
    with transaction.atomic():
        update_request = (
            SystemUpdateRequest.objects.select_for_update()
            .filter(task_id=task.id)
            .first()
        )
        if update_request is None or update_request.terminal_recorded_at is not None:
            return
        update_request.result = task.stage
        update_request.terminal_recorded_at = timezone.now()
        update_request.save(update_fields=["result", "terminal_recorded_at"])
        record_audit(
            update_request.requested_by,
            f"system_update.{task.stage}",
            update_request,
            {
                "task_id": str(task.id),
                "target_version": update_request.target_version,
                "stage": task.stage,
                "rolled_back": task.rolled_back,
                "cleanup": task.cleanup,
                "error_code": task.error_code,
            },
        )


def _reserve_update_request(task_id, actor, target_version):
    try:
        with transaction.atomic():
            update_request = SystemUpdateRequest.objects.create(
                task_id=task_id,
                requested_by=actor,
                target_version=target_version,
                result=SystemUpdateRequest.Result.ACTIVE,
            )
            record_audit(
                actor,
                "system_update.started",
                update_request,
                {"task_id": str(task_id), "target_version": target_version},
            )
            return update_request
    except IntegrityError as error:
        existing = SystemUpdateRequest.objects.filter(task_id=task_id).first()
        if (
            existing is not None
            and existing.requested_by_id == actor.pk
            and existing.target_version == target_version
            and existing.result == SystemUpdateRequest.Result.ACTIVE
        ):
            return existing
        if existing is not None:
            raise _TaskIdConflict("task_id_conflict") from error
        raise


def _matching_task(status, task_id, target_version):
    task = getattr(status, "task", None)
    if (
        task is not None
        and task.id == task_id
        and task.to_version == target_version
    ):
        return task
    return None


def _reconcile_start(updater, task_id, target_version):
    try:
        return _matching_task(updater.status(), task_id, target_version)
    except (UpdaterUnavailable, ValueError):
        return None


@owner_required
@require_GET
def index(request):
    return TemplateResponse(request, "system_update/index.html")


@owner_required
@require_GET
def status(request):
    try:
        update_status = get_updater_client().status()
    except UpdaterUnavailable as error:
        return _updater_error(error)
    except ValueError:
        return _json_error(503, "updater_unavailable")
    if update_status.task is not None:
        _record_terminal_audit(update_status.task)
    return JsonResponse(_status_to_dict(update_status))


@owner_required
@require_POST
def check(request):
    try:
        _strict_json_object(request, set())
    except ValueError:
        return _json_error(400, "invalid_request")
    try:
        update_status = get_updater_client().check()
    except UpdaterUnavailable as error:
        return _updater_error(error)
    except ValueError:
        return _json_error(503, "updater_unavailable")
    return JsonResponse(_status_to_dict(update_status))


@owner_required
@require_POST
def start(request):
    try:
        payload = _strict_json_object(request, {"target_version", "task_id"})
        target_version = payload["target_version"]
        task_id = _parse_task_id(payload["task_id"])
        validate_release_version(target_version)
    except (KeyError, ValidationError, ValueError):
        return _json_error(400, "invalid_request")
    try:
        _reserve_update_request(task_id, request.user, target_version)
    except _TaskIdConflict:
        return _json_error(409, "update_conflict")
    try:
        updater = get_updater_client()
    except UpdaterUnavailable as error:
        return _updater_error(error)
    except ValueError:
        return _json_error(503, "updater_unavailable")
    try:
        task = updater.start(target_version, task_id)
    except UpdaterUnavailable as error:
        if error.code in _RECONCILABLE_START_ERRORS:
            task = _reconcile_start(updater, task_id, target_version)
            if task is not None:
                return JsonResponse(_task_to_dict(task), status=202)
        return _updater_error(error)
    except ValueError:
        return _json_error(503, "updater_unavailable")
    if task.id != task_id or task.to_version != target_version:
        reconciled = _reconcile_start(updater, task_id, target_version)
        if reconciled is None:
            return _json_error(503, "updater_unavailable")
        task = reconciled
    return JsonResponse(_task_to_dict(task), status=202)
