import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from updater.types import (
    CheckResult,
    CleanupJournal,
    CleanupStatus,
    CleanupStepStatus,
    ImageIdentity,
    PersistentState,
    Stage,
    StatusView,
    UpdateTask,
    validate_version,
    version_key,
)


def test_validate_version_accepts_only_canonical_release_versions():
    assert validate_version("v0.2.1") == "v0.2.1"
    for value in ("0.2.1", "v0.2", "v0.2.1-rc.1", "latest", "v0.2.1 evil"):
        with pytest.raises(ValueError):
            validate_version(value)


def test_version_key_returns_release_components_after_validation():
    assert version_key("v12.34.56") == (12, 34, 56)


def test_task_public_view_excludes_private_docker_identity():
    task = update_task_fixture()

    encoded = json.dumps(task.public_view().to_dict())

    for private in ("sha256:", "rollback", "/data/backups", "ghcr.io"):
        assert private not in encoded


def test_task_public_view_replaces_internal_error_with_safe_unknown_code_message():
    internal_error = (
        "docker pull ghcr.io/example/web@sha256:deadbeef\n"
        "Traceback (most recent call last):\n"
        "  File '/srv/updater/commands.py', line 42\n"
        "/data/backups/update.sql\n"
        "Authorization: Bearer token-super-secret"
    )
    task = update_task_fixture(
        error_code="docker_command_failed",
        error_message=internal_error,
    )

    public_task = task.public_view().to_dict()
    encoded = json.dumps(public_task)

    assert public_task["error_code"] == "update_failed"
    assert public_task["error_message"] == "升级失败，请联系管理员。"
    for private in (
        "docker pull",
        "Traceback",
        "sha256:deadbeef",
        "/srv/updater/commands.py",
        "/data/backups/update.sql",
        "token-super-secret",
    ):
        assert private not in encoded


def test_task_public_view_keeps_no_error_empty():
    public_task = update_task_fixture().public_view().to_dict()

    assert public_task["error_code"] == ""
    assert public_task["error_message"] == ""


def test_persistent_state_round_trip_preserves_json_values():
    state = persistent_state_fixture()

    assert PersistentState.from_dict(state.to_dict()) == state


def test_cleanup_journal_round_trip_preserves_each_durable_boundary():
    task = update_task_fixture(
        cleanup_journal=CleanupJournal(
            version_tag=CleanupStepStatus.COMPLETED,
            rollback_alias=CleanupStepStatus.STARTED,
            image_id=CleanupStepStatus.NOT_STARTED,
        )
    )

    restored = UpdateTask.from_dict(task.to_dict())

    assert restored.cleanup_journal == task.cleanup_journal


def test_legacy_task_without_cleanup_journal_loads_as_unknown_progress():
    payload = update_task_fixture().to_dict()
    payload.pop("cleanup_journal")

    restored = UpdateTask.from_dict(payload)

    assert restored.cleanup_journal is None


@pytest.mark.parametrize(
    "journal",
    [
        {
            "version_tag": "not_started",
            "rollback_alias": "started",
            "image_id": "not_started",
        },
        {
            "version_tag": "completed",
            "rollback_alias": "not_started",
            "image_id": "started",
        },
        {
            "version_tag": "completed",
            "rollback_alias": "completed",
            "image_id": "unknown",
        },
        {
            "version_tag": "completed",
            "rollback_alias": "completed",
            "image_id": "completed",
            "unexpected": "value",
        },
    ],
)
def test_cleanup_journal_rejects_invalid_order_status_or_schema(journal):
    payload = update_task_fixture().to_dict()
    payload["cleanup_journal"] = journal

    with pytest.raises(ValueError, match="^invalid_state$"):
        UpdateTask.from_dict(payload)


def test_types_reject_naive_or_non_utc_timestamps():
    with pytest.raises(ValueError, match="timestamp_must_be_utc"):
        ImageIdentity(
            repository="ghcr.io/example/web",
            version="v0.2.1",
            digest="sha256:target",
            image_id="target-image",
            published_at=datetime.fromisoformat("2026-08-07T12:00:00"),
        )


def test_status_view_requires_canonical_versions_and_utc_timestamps():
    with pytest.raises(ValueError, match="invalid_version"):
        StatusView(
            current_version="0.2.1",
            current_published_at=None,
            latest_version="v0.3.0",
            latest_published_at=None,
            update_available=True,
            checked_at=None,
            task=None,
        )


def test_status_view_serializes_only_nullable_public_release_timestamps():
    view = StatusView(
        current_version="v0.2.1",
        current_published_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
        latest_version="v0.3.0",
        latest_published_at=None,
        update_available=True,
        checked_at=None,
        task=None,
    )

    assert view.to_dict() == {
        "current_version": "v0.2.1",
        "current_published_at": "2026-08-06T12:00:00+00:00",
        "latest_version": "v0.3.0",
        "latest_published_at": None,
        "update_available": True,
        "checked_at": None,
        "task": None,
    }


def update_task_fixture(**overrides):
    original = ImageIdentity(
        repository="ghcr.io/example/web",
        version="v0.2.1",
        digest="sha256:original",
        image_id="original-image",
        tags=("v0.2.1",),
        rollback_alias="rollback-v0.2.1",
        published_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
    )
    target = ImageIdentity(
        repository="ghcr.io/example/web",
        version="v0.3.0",
        digest="sha256:target",
        image_id="target-image",
        tags=("v0.3.0",),
        rollback_alias="rollback-v0.3.0",
        published_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
    )
    defaults = {
        "id": uuid4(),
        "original": original,
        "target": target,
        "stage": Stage.BACKING_UP,
        "created_at": datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
        "started_at": datetime(2026, 8, 7, 12, 1, tzinfo=UTC),
        "database_backup": "/data/backups/database.sql",
        "uploads_backup": "/data/backups/uploads.tar",
        "cleanup": CleanupStatus.PENDING,
    }
    defaults.update(overrides)
    return UpdateTask(**defaults)


def persistent_state_fixture(**overrides):
    task = update_task_fixture()
    defaults = {
        "last_check": CheckResult(
            current=task.original,
            target=task.target,
            available=True,
            checked_at=datetime(2026, 8, 7, 12, 2, tzinfo=UTC),
        ),
        "task": task,
    }
    defaults.update(overrides)
    return PersistentState(**defaults)
