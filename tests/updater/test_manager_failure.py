from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from updater.manager import UpdateManager
from updater.platform import SafeOperationError
from updater.store import FileStateStore
from updater.types import (
    CleanupStatus,
    ImageIdentity,
    PersistentState,
    Stage,
    UpdateTask,
)

REPOSITORY = "ghcr.io/s450586793/shunda-finance-web"
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def image(version: str, digest: str) -> ImageIdentity:
    return ImageIdentity(
        repository=REPOSITORY,
        version=version,
        digest=digest,
        image_id=f"image-{version}",
        tags=(f"{REPOSITORY}:{version}",),
        published_at=NOW,
    )


class FailurePlatform:
    def __init__(self, failure: str | None = None):
        self.original = image("v0.2.0", "sha256:original")
        self.stable = image("v0.2.1", "sha256:target")
        self.current = self.original
        self.failure = failure
        self.fail_rollback = False
        self.fail_rollback_health = False
        self.fail_version_restore = False
        self.calls: list[str] = []
        self.persisted_versions: list[str] = []
        self.cleanup_calls: list[UpdateTask] = []
        self._state = lambda: PersistentState()

    def record_state(self, store: FileStateStore) -> None:
        self._state = store.load

    def inspect_web(self) -> ImageIdentity:
        return self.current

    def resolve_stable(self) -> ImageIdentity:
        return self.stable

    def create_backup(self) -> tuple[str, str]:
        self._call("backup")
        return "/data/backups/db.dump", "/data/backups/uploads.tar.gz"

    def verify_target(self, target: ImageIdentity) -> None:
        assert target == self.stable
        self._call("verify_target")

    def tag_rollback(self, task: UpdateTask) -> None:
        self._call("tag_rollback")
        task.original = replace(
            task.original,
            rollback_alias=f"shunda-finance-rollback-web:{task.id}",
        )

    def stop_web(self) -> None:
        self._call("stop_web")

    def migrate_target(self, target: ImageIdentity, *, task_id: UUID | None = None) -> None:
        assert target == self.stable
        self._call("migrate_target")

    def start_target(self, target: ImageIdentity, *, task_id: UUID | None = None) -> None:
        assert target == self.stable
        self._call("start_target")
        self.current = target

    def start_rollback(self, task: UpdateTask) -> None:
        assert task.original.rollback_alias == f"shunda-finance-rollback-web:{task.id}"
        self.calls.append("start_rollback")
        if self.fail_rollback:
            raise SafeOperationError("rollback_failed")
        self.current = self.original

    def health(self, expected: ImageIdentity | None = None) -> None:
        assert expected is not None
        if self._matches_runtime_identity(expected, self.original):
            self.calls.append("health_rollback")
            if self.fail_rollback_health:
                raise SafeOperationError("health_check_failed")
            assert self.current == self.original
            return
        assert expected == self.stable
        stage = self._state().task.stage
        if stage is Stage.STABILIZING:
            self._call("stabilize")
            return
        self._call("health_target")

    def persist_version(self, version: str) -> None:
        self.persisted_versions.append(version)
        self.calls.append(f"persist:{version}")
        if version == self.stable.version and self.failure == "persist_version":
            raise SafeOperationError("persist_failed")
        if version == self.original.version and self.fail_version_restore:
            raise SafeOperationError("persist_failed")

    def cleanup_original(self, task: UpdateTask) -> None:
        self.cleanup_calls.append(task)
        self.calls.append("cleanup_original")

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.failure == name:
            raise SafeOperationError(
                {
                    "backup": "backup_failed",
                    "verify_target": "pull_failed",
                    "tag_rollback": "target_identity_mismatch",
                    "stop_web": "stop_failed",
                    "migrate_target": "migration_failed",
                    "start_target": "start_failed",
                    "health_target": "health_check_failed",
                    "stabilize": "health_check_failed",
                }[name]
            )

    @staticmethod
    def _matches_runtime_identity(
        actual: ImageIdentity, expected: ImageIdentity
    ) -> bool:
        return (
            actual.repository,
            actual.version,
            actual.digest,
            actual.image_id,
            actual.tags,
            actual.published_at,
        ) == (
            expected.repository,
            expected.version,
            expected.digest,
            expected.image_id,
            expected.tags,
            expected.published_at,
        )


def run_started_task(manager: UpdateManager) -> None:
    manager.check()
    _task, execute = manager.start("v0.2.1")
    execute()


@pytest.fixture
def store(tmp_path: Path) -> FileStateStore:
    return FileStateStore(tmp_path / "update-state.json")


@pytest.fixture
def manager_factory(store):
    def create(platform: FailurePlatform) -> UpdateManager:
        platform.record_state(store)
        return UpdateManager(store, platform, now=lambda: NOW, sleeper=lambda _: None)

    return create


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        ("backup", "backup_failed"),
        ("verify_target", "pull_failed"),
        ("tag_rollback", "update_failed"),
    ],
)
def test_pre_stop_failure_leaves_web_running_without_rollback(
    manager_factory, store, failure, error_code
):
    platform = FailurePlatform(failure)
    manager = manager_factory(platform)

    run_started_task(manager)

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.FAILED
    assert task.rolled_back is False
    assert task.cleanup is CleanupStatus.NOT_RUN
    assert task.error_code == error_code
    assert task.public_view().error_code == error_code
    assert "rollback" not in platform.calls
    assert platform.current == platform.original
    assert platform.cleanup_calls == []


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        ("stop_web", "update_failed"),
        ("migrate_target", "migration_failed"),
        ("start_target", "update_failed"),
        ("health_target", "health_check_failed"),
        ("stabilize", "health_check_failed"),
        ("persist_version", "update_failed"),
    ],
)
def test_post_stop_failure_rolls_back_without_cleanup(
    manager_factory, store, failure, error_code
):
    platform = FailurePlatform(failure)
    manager = manager_factory(platform)

    run_started_task(manager)

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.FAILED
    assert task.rolled_back is True
    assert task.cleanup is CleanupStatus.NOT_RUN
    assert task.error_code == error_code
    assert task.public_view().error_code == error_code
    expected_tail = ["start_rollback", "health_rollback"]
    if failure == "persist_version":
        expected_tail.append("persist:v0.2.0")
    assert platform.calls[-len(expected_tail) :] == expected_tail
    assert platform.current == platform.original
    assert platform.cleanup_calls == []


def test_persist_failure_restores_original_env_after_rollback_health(
    manager_factory, store
):
    platform = FailurePlatform("persist_version")
    manager = manager_factory(platform)

    run_started_task(manager)

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.FAILED
    assert task.rolled_back is True
    assert platform.persisted_versions == ["v0.2.1", "v0.2.0"]
    assert platform.calls[-3:] == [
        "start_rollback",
        "health_rollback",
        "persist:v0.2.0",
    ]
    assert platform.cleanup_calls == []


@pytest.mark.parametrize("rollback_failure", ["start", "health", "env"])
def test_rollback_failure_requires_manual_intervention_without_cleanup(
    manager_factory, store, rollback_failure
):
    platform = FailurePlatform("persist_version" if rollback_failure == "env" else "migrate_target")
    platform.fail_rollback = rollback_failure == "start"
    platform.fail_rollback_health = rollback_failure == "health"
    platform.fail_version_restore = rollback_failure == "env"
    manager = manager_factory(platform)

    run_started_task(manager)

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.MANUAL_INTERVENTION
    assert task.rolled_back is False
    assert task.error_code == "rollback_failed"
    assert task.public_view().error_code == "rollback_failed"
    assert task.public_view().error_message == "升级失败，需要人工处理。"
    assert platform.cleanup_calls == []
