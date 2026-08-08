import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.accounts.roles import Role, assign_role
from apps.core.models import AuditLog
from apps.system_update.client import (
    UpdaterStatusView,
    UpdaterTaskView,
    UpdaterUnavailable,
)
from apps.system_update.models import SystemUpdateRequest

TASK_ID = UUID("00000000-0000-0000-0000-000000000001")
VALID_START_PAYLOAD = {
    "target_version": "v0.2.1",
    "task_id": str(TASK_ID),
}
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
TOKEN = "t" * 32
SENSITIVE_VALUES = (
    TOKEN,
    "sha256:private-digest",
    "private-image-id",
    "rollback-alias",
    "/private/path",
    "raw failure body",
)


def _task(
    *,
    stage="checking",
    error_code="",
    error_message="",
    rolled_back=False,
    cleanup="not_run",
):
    return UpdaterTaskView(
        id=TASK_ID,
        from_version="v0.2.0",
        to_version="v0.2.1",
        stage=stage,
        created_at=NOW,
        started_at=NOW,
        finished_at=NOW
        if stage in {"succeeded", "failed", "manual_intervention"}
        else None,
        backup_complete=True,
        rolled_back=rolled_back,
        cleanup=cleanup,
        error_code=error_code,
        error_message=error_message,
    )


def _status(*, task=None):
    return UpdaterStatusView(
        current_version="v0.2.0",
        current_published_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
        latest_version="v0.2.1",
        latest_published_at=NOW,
        update_available=True,
        checked_at=NOW,
        task=task,
    )


def _json(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


@pytest.mark.django_db
def test_update_routes_redirect_anonymous_users_to_login(client):
    for url in (
        reverse("system-update:index"),
        reverse("system-update:status"),
        reverse("system-update:check"),
        reverse("system-update:start"),
    ):
        response = client.get(url)

        assert response.status_code == 302
        assert response.headers["Location"] == f"/accounts/login/?next={url}"


@pytest.mark.django_db
def test_update_routes_forbid_finance_and_superuser_without_owner_role(finance_user):
    superuser = User.objects.create_superuser("root", "root@example.test", "secret")
    finance_client = Client()
    finance_client.force_login(finance_user)
    superuser_client = Client()
    superuser_client.force_login(superuser)
    urls = (
        reverse("system-update:index"),
        reverse("system-update:status"),
        reverse("system-update:check"),
        reverse("system-update:start"),
    )

    for authenticated_client in (finance_client, superuser_client):
        for url in urls:
            assert authenticated_client.get(url).status_code == 403


@pytest.mark.django_db
def test_owner_can_resolve_the_update_page_view_without_a_template(owner_user):
    from apps.system_update.views import index

    request = RequestFactory().get(reverse("system-update:index"))
    request.user = owner_user

    assert index(request).status_code == 200


@pytest.mark.django_db
def test_owner_update_index_renders_a_controlled_upgrade_workspace(owner_client):
    response = owner_client.get(reverse("system-update:index"))

    assert response.status_code == 200
    page = response.content.decode()
    for marker in (
        'data-system-update',
        'data-status-url="/system/update/status/"',
        'data-check-url="/system/update/check/"',
        'data-start-url="/system/update/start/"',
        'data-csrf',
        'data-current-version',
        'data-current-published-at',
        'data-latest-version',
        'data-latest-published-at',
        'data-task-id',
        'data-task-created-at',
        'data-task-started-at',
        'data-task-finished-at',
        'data-operation-guidance',
        'data-progress',
        'data-backup-state',
        'data-rollback-state',
        'data-cleanup-state',
        'data-confirm-dialog',
        'data-confirm-start',
        'data-check',
        'data-start',
        'data-status-refresh',
    ):
        assert marker in page
    assert '<dialog' in page
    for icon in (
        'refresh-cw',
        'download',
        'shield-check',
        'rotate-ccw',
        'trash-2',
        'alert-triangle',
    ):
        assert f'data-lucide="{icon}"' in page
    assert "Docker" not in page
    assert "updater:" not in page
    assert "innerHTML" not in (
        Path(__file__).parents[2] / "static/js/system-update.js"
    ).read_text()


@pytest.mark.django_db
def test_navigation_shows_real_system_settings_only_to_owner(owner_user, finance_user):
    owner_client = Client()
    owner_client.force_login(owner_user)
    finance_client = Client()
    finance_client.force_login(finance_user)
    owner_response = owner_client.get("/reporting/")
    finance_response = finance_client.get("/imports/")
    owner_items = {
        item["label"]: item for item in owner_response.context["navigation_items"]
    }
    finance_labels = [
        item["label"] for item in finance_response.context["navigation_items"]
    ]

    assert owner_items["系统设置"] == {
        "label": "系统设置",
        "href": reverse("system-update:index"),
        "icon": "settings",
    }
    assert "系统设置" not in finance_labels


@pytest.mark.django_db
def test_status_is_owner_only_get_and_proxies_a_public_status(owner_client):
    updater = Mock(status=Mock(return_value=_status(task=_task())))

    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        response = owner_client.get(reverse("system-update:status"))

    assert response.status_code == 200
    assert response.json() == {
        "current_version": "v0.2.0",
        "current_published_at": "2026-08-06T12:00:00+00:00",
        "latest_version": "v0.2.1",
        "latest_published_at": "2026-08-07T12:00:00+00:00",
        "update_available": True,
        "checked_at": "2026-08-07T12:00:00+00:00",
        "task": {
            "id": str(TASK_ID),
            "from_version": "v0.2.0",
            "to_version": "v0.2.1",
            "stage": "checking",
            "created_at": "2026-08-07T12:00:00+00:00",
            "started_at": "2026-08-07T12:00:00+00:00",
            "finished_at": None,
            "backup_complete": True,
            "rolled_back": False,
            "cleanup": "not_run",
            "error_code": "",
            "error_message": "",
        },
    }
    encoded = response.content.decode()
    assert all(value not in encoded for value in SENSITIVE_VALUES)
    updater.status.assert_called_once_with()
    assert (
        owner_client.post(
            reverse("system-update:status"), data=b"{}", content_type="application/json"
        ).status_code
        == 405
    )


@pytest.mark.django_db
def test_check_requires_csrf_and_exact_empty_json(owner_user):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(owner_user)
    url = reverse("system-update:check")
    token = csrf_client.get("/reporting/").cookies["csrftoken"].value
    updater = Mock(check=Mock(return_value=_status()))

    assert csrf_client.get(url).status_code == 405
    assert _json(csrf_client, url, {}).status_code == 403
    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        response = csrf_client.post(
            url,
            data=b"{}",
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )

    assert response.status_code == 200
    assert response.json()["latest_version"] == "v0.2.1"
    updater.check.assert_called_once_with()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("content_type", "payload"),
    [
        ("text/plain", b"{}"),
        ("application/json; charset=utf-8", b"{}"),
        ("application/json", b"[]"),
        ("application/json", b'{"unexpected": true}'),
        ("application/json", b'{"target_version":"v0.2.1","target_version":"v0.2.1"}'),
        ("application/json", b'{"target_version":NaN}'),
    ],
)
def test_check_rejects_nonexact_json_without_calling_updater(
    owner_client, content_type, payload
):
    updater = Mock()

    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        response = owner_client.post(
            reverse("system-update:check"), data=payload, content_type=content_type
        )

    assert response.status_code == 400
    assert json.loads(response.content) == {"error": "invalid_request"}
    updater.check.assert_not_called()


@pytest.mark.django_db
def test_check_rejects_a_missing_content_length_without_calling_updater(owner_user):
    from apps.system_update.views import check

    request = RequestFactory().post(
        reverse("system-update:check"),
        data=b"{}",
        content_type="application/json",
    )
    request.user = owner_user
    request.META["CONTENT_LENGTH"] = ""
    updater = Mock(check=Mock(return_value=_status()))

    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        response = check(request)

    assert response.status_code == 400
    assert json.loads(response.content) == {"error": "invalid_request"}
    updater.check.assert_not_called()


@pytest.mark.django_db
def test_check_rejects_a_body_larger_than_the_fixed_limit(owner_client, settings):
    settings.SYSTEM_UPDATE_MAX_REQUEST_BYTES = 8
    updater = Mock()

    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        response = owner_client.post(
            reverse("system-update:check"),
            data=b"{" + b" " * 9 + b"}",
            content_type="application/json",
        )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    updater.check.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "target", ["0.2.1", "v00.2.1", "v0.02.1", "v0.2.01", "v0.2.1 "]
)
def test_start_rejects_noncanonical_versions_without_calling_updater(
    owner_client, target
):
    updater = Mock()

    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        response = _json(
            owner_client,
            reverse("system-update:start"),
            {**VALID_START_PAYLOAD, "target_version": target},
        )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    updater.start.assert_not_called()


@pytest.mark.django_db
def test_start_requires_csrf_exact_schema_and_records_one_safe_audit(owner_user):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(owner_user)
    url = reverse("system-update:start")
    token = csrf_client.get("/reporting/").cookies["csrftoken"].value
    updater = Mock(start=Mock(return_value=_task()))

    assert csrf_client.get(url).status_code == 405
    assert _json(csrf_client, url, VALID_START_PAYLOAD).status_code == 403
    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        response = csrf_client.post(
            url,
            data=(
                b'{"target_version":"v0.2.1",'
                b'"task_id":"00000000-0000-0000-0000-000000000001"}'
            ),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )

    request = SystemUpdateRequest.objects.get(task_id=TASK_ID)
    audit = AuditLog.objects.get(action="system_update.started")
    assert response.status_code == 202
    assert response.json()["id"] == str(TASK_ID)
    assert request.requested_by == owner_user
    assert request.target_version == "v0.2.1"
    assert request.result == "active"
    assert audit.actor == owner_user
    assert audit.target_id == str(request.pk)
    assert audit.changes == {"task_id": str(TASK_ID), "target_version": "v0.2.1"}
    updater.start.assert_called_once_with("v0.2.1", TASK_ID)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"target_version": "v0.2.1"},
        {"task_id": str(TASK_ID)},
        {**VALID_START_PAYLOAD, "extra": True},
    ],
)
def test_start_requires_exact_target_and_task_id_schema(owner_client, payload):
    updater = Mock()

    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        response = _json(owner_client, reverse("system-update:start"), payload)

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    updater.start.assert_not_called()
    assert not SystemUpdateRequest.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "task_id",
    [
        None,
        1,
        "not-a-uuid",
        "00000000000000000000000000000001",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    ],
)
def test_start_rejects_noncanonical_task_ids_without_durable_side_effects(
    owner_client, task_id
):
    updater = Mock()

    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        response = _json(
            owner_client,
            reverse("system-update:start"),
            {**VALID_START_PAYLOAD, "task_id": task_id},
        )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    updater.start.assert_not_called()
    assert not SystemUpdateRequest.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("code", "status", "error"),
    [
        ("update_conflict", 409, "update_conflict"),
        ("invalid_request", 400, "invalid_request"),
        ("network_error", 503, "updater_unavailable"),
        ("unexpected_private_code", 503, "updater_unavailable"),
    ],
)
def test_updater_errors_have_a_fixed_safe_http_mapping(
    owner_client, code, status, error
):
    updater = Mock(start=Mock(side_effect=UpdaterUnavailable(code)))

    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        response = _json(
            owner_client, reverse("system-update:start"), VALID_START_PAYLOAD
        )

    assert response.status_code == status
    assert response.json() == {"error": error}
    request = SystemUpdateRequest.objects.get(task_id=TASK_ID)
    assert request.result == "active"
    assert AuditLog.objects.filter(action="system_update.started").count() == 1


@pytest.mark.django_db
def test_start_handles_unavailable_client_factory_after_durable_reservation(
    owner_client,
):
    with (
        patch(
            "apps.system_update.views.get_updater_client",
            side_effect=UpdaterUnavailable("network_error"),
        ),
    ):
        response = _json(
            owner_client,
            reverse("system-update:start"),
            VALID_START_PAYLOAD,
        )

    assert response.status_code == 503
    assert response.json() == {"error": "updater_unavailable"}
    assert SystemUpdateRequest.objects.filter(task_id=TASK_ID, result="active").exists()
    assert AuditLog.objects.filter(action="system_update.started").count() == 1


@pytest.mark.django_db
def test_start_duplicate_task_id_reuses_the_durable_request_without_another_audit(
    owner_client, owner_user
):
    SystemUpdateRequest.objects.create(
        task_id=TASK_ID,
        requested_by=owner_user,
        target_version="v0.2.1",
        result="active",
    )
    updater = Mock(start=Mock(return_value=_task()))

    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        response = _json(
            owner_client, reverse("system-update:start"), VALID_START_PAYLOAD
        )

    assert response.status_code == 202
    assert response.json()["id"] == str(TASK_ID)
    assert SystemUpdateRequest.objects.count() == 1
    assert not AuditLog.objects.exists()
    updater.start.assert_called_once_with("v0.2.1", TASK_ID)


@pytest.mark.django_db
@pytest.mark.parametrize("conflict", ["owner", "target", "terminal"])
def test_start_duplicate_task_id_conflicts_without_exposing_integrity_errors(
    owner_client, owner_user, conflict
):
    requested_by = owner_user
    target_version = "v0.2.1"
    result = "active"
    if conflict == "owner":
        requested_by = User.objects.create_user("other-owner", password="secret")
    elif conflict == "target":
        target_version = "v0.2.2"
    else:
        result = "failed"
    SystemUpdateRequest.objects.create(
        task_id=TASK_ID,
        requested_by=requested_by,
        target_version=target_version,
        result=result,
    )
    updater_factory = Mock()

    with patch("apps.system_update.views.get_updater_client", updater_factory):
        response = _json(
            owner_client, reverse("system-update:start"), VALID_START_PAYLOAD
        )

    assert response.status_code == 409
    assert response.json() == {"error": "update_conflict"}
    updater_factory.assert_not_called()
    assert SystemUpdateRequest.objects.count() == 1
    assert not AuditLog.objects.exists()


@pytest.mark.django_db
def test_start_rolls_back_when_started_audit_cannot_be_written(owner_user):
    from apps.system_update.views import start

    request = RequestFactory().post(
        reverse("system-update:start"),
        data=(
            b'{"target_version":"v0.2.1",'
            b'"task_id":"00000000-0000-0000-0000-000000000001"}'
        ),
        content_type="application/json",
    )
    request.user = owner_user
    updater = Mock(start=Mock(return_value=_task()))

    with (
        patch("apps.system_update.views.get_updater_client", return_value=updater),
        patch("apps.system_update.views.record_audit", side_effect=IntegrityError("audit")),
        pytest.raises(IntegrityError, match="audit"),
    ):
        start(request)

    assert not SystemUpdateRequest.objects.exists()
    updater.start.assert_not_called()


@pytest.mark.django_db
def test_start_does_not_call_updater_when_request_reservation_fails(owner_client):
    updater = Mock(start=Mock(return_value=_task()))

    with (
        patch("apps.system_update.views.get_updater_client", return_value=updater),
        patch(
            "apps.system_update.views.SystemUpdateRequest.objects.create",
            side_effect=IntegrityError("database"),
        ),
        pytest.raises(IntegrityError, match="database"),
    ):
        _json(
            owner_client,
            reverse("system-update:start"),
            VALID_START_PAYLOAD,
        )

    updater.start.assert_not_called()


@pytest.mark.django_db
def test_start_reconciles_an_accepted_task_after_ambiguous_response(owner_client):
    updater = Mock(
        start=Mock(side_effect=UpdaterUnavailable("invalid_response")),
        status=Mock(return_value=_status(task=_task())),
    )

    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        response = _json(
            owner_client,
            reverse("system-update:start"),
            VALID_START_PAYLOAD,
        )

    assert response.status_code == 202
    assert response.json()["id"] == str(TASK_ID)
    assert SystemUpdateRequest.objects.filter(task_id=TASK_ID, result="active").exists()
    updater.start.assert_called_once_with("v0.2.1", TASK_ID)
    updater.status.assert_called_once_with()


@pytest.mark.django_db
def test_terminal_status_is_recorded_once_with_safe_audit_fields(
    owner_client, owner_user
):
    update_request = SystemUpdateRequest.objects.create(
        task_id=TASK_ID,
        requested_by=owner_user,
        target_version="v0.2.1",
        result="active",
    )
    updater = Mock(
        status=Mock(
            return_value=_status(
                task=_task(
                    stage="failed",
                    error_code="update_failed",
                    error_message="升级失败，请联系管理员。",
                    rolled_back=True,
                    cleanup="pending",
                )
            )
        )
    )

    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        first = owner_client.get(reverse("system-update:status"))
        second = owner_client.get(reverse("system-update:status"))

    update_request.refresh_from_db()
    audits = AuditLog.objects.filter(action="system_update.failed")
    assert first.status_code == 200
    assert second.status_code == 200
    assert update_request.result == "failed"
    assert update_request.terminal_recorded_at is not None
    assert audits.count() == 1
    assert audits.get().changes == {
        "task_id": str(TASK_ID),
        "target_version": "v0.2.1",
        "stage": "failed",
        "rolled_back": True,
        "cleanup": "pending",
        "error_code": "update_failed",
    }


@pytest.mark.django_db
def test_terminal_audit_uses_the_request_owner_not_the_polling_owner(owner_user):
    polling_owner = User.objects.create_user("polling-owner", password="secret")
    assign_role(polling_owner, Role.OWNER)
    polling_client = Client()
    polling_client.force_login(polling_owner)
    SystemUpdateRequest.objects.create(
        task_id=TASK_ID,
        requested_by=owner_user,
        target_version="v0.2.1",
        result="active",
    )
    updater = Mock(
        status=Mock(
            return_value=_status(task=_task(stage="succeeded", cleanup="complete"))
        )
    )

    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        response = polling_client.get(reverse("system-update:status"))

    assert response.status_code == 200
    audit = AuditLog.objects.get(action="system_update.succeeded")
    assert audit.actor == owner_user
    assert audit.actor != polling_owner


@pytest.mark.django_db
def test_terminal_status_locks_the_matching_request_before_one_audit(
    owner_client, owner_user, monkeypatch
):
    SystemUpdateRequest.objects.create(
        task_id=TASK_ID,
        requested_by=owner_user,
        target_version="v0.2.1",
        result="active",
    )
    updater = Mock(
        status=Mock(
            return_value=_status(task=_task(stage="succeeded", cleanup="complete"))
        )
    )
    select_for_update = SystemUpdateRequest.objects.select_for_update
    lock_calls = []

    def record_lock():
        lock_calls.append(TASK_ID)
        return select_for_update()

    monkeypatch.setattr(SystemUpdateRequest.objects, "select_for_update", record_lock)
    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        owner_client.get(reverse("system-update:status"))
        owner_client.get(reverse("system-update:status"))

    assert lock_calls == [TASK_ID, TASK_ID]
    assert AuditLog.objects.filter(action="system_update.succeeded").count() == 1


@pytest.mark.django_db
def test_unrelated_terminal_status_does_not_create_an_audit(owner_client):
    unrelated_task = _task(
        stage="manual_intervention",
        error_code="rollback_failed",
        error_message="升级失败，需要人工处理。",
    )
    updater = Mock(status=Mock(return_value=_status(task=unrelated_task)))

    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        response = owner_client.get(reverse("system-update:status"))

    assert response.status_code == 200
    assert not AuditLog.objects.exists()


@pytest.mark.django_db
def test_status_does_not_persist_or_expose_sensitive_updater_values(
    owner_client, owner_user
):
    SystemUpdateRequest.objects.create(
        task_id=TASK_ID,
        requested_by=owner_user,
        target_version="v0.2.1",
        result="active",
    )
    updater = Mock(status=Mock(side_effect=UpdaterUnavailable("network_error")))

    with patch("apps.system_update.views.get_updater_client", return_value=updater):
        response = owner_client.get(reverse("system-update:status"))

    rendered = response.content.decode() + json.dumps(
        list(AuditLog.objects.values("changes"))
    )
    assert response.status_code == 503
    assert response.json() == {"error": "updater_unavailable"}
    assert not AuditLog.objects.exists()
    assert all(value not in rendered for value in SENSITIVE_VALUES)
