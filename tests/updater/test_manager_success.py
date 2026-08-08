from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from uuid import uuid4

import pytest

from updater.manager import UpdateConflict, UpdateManager
from updater.platform import SafeOperationError
from updater.store import FileStateStore
from updater.types import (
    CheckResult,
    CleanupJournal,
    CleanupStatus,
    CleanupStep,
    CleanupStepStatus,
    ImageIdentity,
    PersistentState,
    Stage,
    UpdateTask,
)

REPOSITORY = "ghcr.io/s450586793/shunda-finance-web"
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def _capture_error(call, errors) -> None:
    try:
        call()
    except Exception as error:  # noqa: BLE001
        errors.append(error)


def image(
    version: str,
    *,
    digest: str = "sha256:target",
    repository: str = REPOSITORY,
    published_at: datetime | None = NOW,
) -> ImageIdentity:
    return ImageIdentity(
        repository=repository,
        version=version,
        digest=digest,
        image_id=f"image-{version}",
        tags=(f"{repository}:{version}",),
        published_at=published_at,
    )


class RecordingStore(FileStateStore):
    def __init__(self, path: Path):
        super().__init__(path)
        self.saved: list[PersistentState] = []

    def save(self, state: PersistentState) -> None:
        self.saved.append(deepcopy(state))
        super().save(state)


class FakePlatform:
    def __init__(self):
        self.current = image("v0.2.0", digest="sha256:current")
        self.stable = image("v0.2.1")
        self.calls: list[str] = []
        self.health_targets: list[ImageIdentity] = []
        self.fail_cleanup = False
        self.fail_inspect_after: int | None = None
        self.inspect_calls = 0
        self.cleanup_observations: list[tuple[CleanupStep, CleanupStepStatus]] = []
        self.block_call: str | None = None
        self.operation_started = Event()
        self.release_operation = Event()
        self.stage_at_call: list[tuple[str, Stage]] = []
        self._state = lambda: PersistentState()
        self.inspect_started: Event | None = None
        self.release_inspect: Event | None = None
        self._active_calls = 0
        self.max_active_calls = 0
        self._active_lock = Lock()

    def record_stages_from(self, store: FileStateStore) -> None:
        self._state = store.load

    def _enter(self) -> None:
        with self._active_lock:
            self._active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self._active_calls)

    def _leave(self) -> None:
        with self._active_lock:
            self._active_calls -= 1

    def inspect_web(self) -> ImageIdentity:
        self._enter()
        try:
            self.inspect_calls += 1
            if (
                self.fail_inspect_after is not None
                and self.inspect_calls > self.fail_inspect_after
            ):
                raise SafeOperationError("inspect_failed")
            if self.inspect_started is not None:
                self.inspect_started.set()
            if self.release_inspect is not None:
                assert self.release_inspect.wait(timeout=1)
            return self.current
        finally:
            self._leave()

    def resolve_stable(self) -> ImageIdentity:
        self._enter()
        try:
            return self.stable
        finally:
            self._leave()

    def _call(self, name: str) -> None:
        task = self._state().task
        assert task is not None
        self.calls.append(name)
        self.stage_at_call.append((name, task.stage))
        self._block(name)

    def _block(self, name: str) -> None:
        if self.block_call != name:
            return
        self.operation_started.set()
        assert self.release_operation.wait(timeout=2)

    def create_backup(self) -> tuple[str, str]:
        self._call("backup")
        return "/data/backups/db.dump", "/data/backups/uploads.tar.gz"

    def verify_target(self, target: ImageIdentity) -> None:
        assert target == self.stable
        self._call("verify_target")

    def tag_rollback(self, task: UpdateTask) -> None:
        self._call("tag_rollback")

    def stop_web(self) -> None:
        self._call("stop_web")

    def migrate_target(self, target: ImageIdentity, *, task_id=None) -> None:
        assert target == self.stable
        self._call("migrate_target")

    def start_target(self, target: ImageIdentity, *, task_id=None) -> None:
        assert target == self.stable
        self.current = target
        self._call("start_target")

    def health(self, expected: ImageIdentity | None = None) -> None:
        assert expected == self.stable
        self.health_targets.append(expected)
        task = self._state().task
        assert task is not None
        self.stage_at_call.append(("health_target", task.stage))
        if task.stage is Stage.STABILIZING:
            self._block("stabilize")
            if sum(1 for stage in self.stage_at_call if stage[1] is Stage.STABILIZING) == 1:
                self.calls.append("stabilize")
            return
        self._block("health_target")
        self.calls.append("health_target")

    def persist_version(self, version: str) -> None:
        assert version == self.stable.version
        self._call("persist_version")

    def cleanup_original_step(
        self,
        task: UpdateTask,
        step: CleanupStep,
    ) -> None:
        persisted = self._state().task
        assert persisted is not None
        assert persisted.cleanup_journal is not None
        status = getattr(persisted.cleanup_journal, step.value)
        self.cleanup_observations.append((step, status))
        self._call(f"cleanup_{step.value}")
        if self.fail_cleanup:
            raise SafeOperationError("cleanup_failed")


@pytest.fixture
def clock():
    return [NOW]


@pytest.fixture
def store(tmp_path):
    return RecordingStore(tmp_path / "update-state.json")


@pytest.fixture
def platform():
    return FakePlatform()


@pytest.fixture
def manager(store, platform, clock):
    platform.record_stages_from(store)
    return UpdateManager(store, platform, now=lambda: clock[0], sleeper=lambda _: None)


def test_check_reports_only_a_newer_semver(manager, platform):
    current_published_at = datetime(2026, 8, 6, 11, 0, tzinfo=UTC)
    latest_published_at = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    platform.current = image(
        "v0.2.0",
        digest="sha256:current",
        published_at=current_published_at,
    )
    platform.stable = image("v0.2.1", published_at=latest_published_at)

    view = manager.check()

    assert view.update_available is True
    assert view.current_version == "v0.2.0"
    assert view.current_published_at == current_published_at
    assert view.latest_version == "v0.2.1"
    assert view.latest_published_at == latest_published_at
    assert view.task is None


def test_status_without_a_check_uses_nullable_inspected_current_publication(
    manager, platform
):
    platform.current = image(
        "v0.2.0",
        digest="sha256:current-private",
        published_at=None,
    )

    view = manager.status()

    assert view.current_published_at is None
    assert view.latest_published_at is None
    assert set(view.to_dict()) == {
        "current_version",
        "current_published_at",
        "latest_version",
        "latest_published_at",
        "update_available",
        "checked_at",
        "task",
    }
    assert "sha256:current-private" not in json.dumps(view.to_dict())


def test_status_with_a_check_keeps_the_checked_current_publication(manager, platform):
    checked_published_at = datetime(2026, 8, 6, 11, 0, tzinfo=UTC)
    platform.current = image(
        "v0.2.0",
        digest="sha256:checked",
        published_at=checked_published_at,
    )
    manager.check()
    inspect_calls = platform.inspect_calls
    platform.current = image(
        "v0.2.0",
        digest="sha256:later",
        published_at=datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
    )

    view = manager.status()

    assert view.current_published_at == checked_published_at
    assert platform.inspect_calls == inspect_calls


@pytest.mark.parametrize("target", ["v0.2.0", "v0.1.9"])
def test_check_rejects_same_or_older_versions_as_updates(manager, platform, target):
    platform.stable = image(target)

    view = manager.check()

    assert view.update_available is False
    assert view.latest_version == target


def test_check_does_not_offer_same_version_when_digest_changes(manager, platform):
    platform.stable = image("v0.2.0", digest="sha256:changed")

    view = manager.check()

    assert view.update_available is False


def test_check_rejects_mismatched_image_repositories(manager, platform):
    platform.stable = image("v0.2.1", repository="registry.invalid/other-web")

    with pytest.raises(UpdateConflict, match="^repository_mismatch$"):
        manager.check()


def test_start_rejects_a_check_older_than_two_minutes(manager, platform, clock):
    manager.check()
    clock[0] += timedelta(minutes=2, microseconds=1)

    with pytest.raises(UpdateConflict, match="^stale_check$"):
        manager.start("v0.2.1")


def test_start_rejects_a_check_at_exactly_two_minutes(manager, clock):
    manager.check()
    clock[0] += timedelta(minutes=2)

    with pytest.raises(UpdateConflict, match="^stale_check$"):
        manager.start("v0.2.1")


def test_start_requires_the_checked_target_version(manager):
    manager.check()

    with pytest.raises(UpdateConflict, match="^target_mismatch$"):
        manager.start("v0.2.2")


def test_start_rejects_when_stable_changes_after_check(manager, platform):
    manager.check()
    platform.stable = image("v0.2.1", digest="sha256:replacement")

    with pytest.raises(UpdateConflict, match="^target_changed$"):
        manager.start("v0.2.1")


def test_start_rejects_when_no_newer_update_was_checked(manager, platform):
    platform.stable = image("v0.2.0")
    manager.check()

    with pytest.raises(UpdateConflict, match="^no_update$"):
        manager.start("v0.2.0")


def test_start_persists_the_caller_generated_task_id(manager, store):
    manager.check()
    task_id = uuid4()

    task, _execute = manager.start("v0.2.1", task_id=task_id)

    assert task.id == task_id
    assert store.load().task is not None
    assert store.load().task.id == task_id


def test_repeated_start_with_same_task_id_is_idempotent(manager, platform):
    manager.check()
    task_id = uuid4()

    first, _first_execute = manager.start("v0.2.1", task_id=task_id)
    calls_after_first = list(platform.calls)
    second, _second_execute = manager.start("v0.2.1", task_id=task_id)

    assert second == first
    assert platform.calls == calls_after_first


@pytest.mark.parametrize("task_id", ["not-a-uuid", 1, object()])
def test_start_rejects_non_uuid_correlation_id(manager, task_id):
    manager.check()

    with pytest.raises(UpdateConflict, match="^invalid_task_id$"):
        manager.start("v0.2.1", task_id=task_id)


def test_concurrent_checks_are_serialized(store, platform):
    started = Event()
    release = Event()
    platform.inspect_started = started
    platform.release_inspect = release
    platform.record_stages_from(store)
    manager = UpdateManager(store, platform, now=lambda: NOW, sleeper=lambda _: None)
    results = []

    first = Thread(target=lambda: results.append(manager.check()))
    second = Thread(target=lambda: results.append(manager.check()))
    first.start()
    assert started.wait(timeout=1)
    second.start()
    release.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(results) == 2
    assert platform.max_active_calls == 1


def test_concurrent_starts_create_only_one_task(manager, store):
    manager.check()
    ready = Event()
    go = Event()
    tasks = []
    errors = []

    def start() -> None:
        ready.set()
        assert go.wait(timeout=1)
        try:
            tasks.append(manager.start("v0.2.1")[0])
        except UpdateConflict as error:
            errors.append(error.code)

    first = Thread(target=start)
    second = Thread(target=start)
    first.start()
    second.start()
    assert ready.wait(timeout=1)
    go.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(tasks) == 1
    assert errors == ["task_active"]
    assert store.load().task is not None


@pytest.mark.parametrize("stage", [Stage.BACKING_UP, Stage.MANUAL_INTERVENTION])
def test_active_or_manual_task_blocks_check_and_start(store, platform, manager, stage):
    checked = CheckResult(
        current=platform.current,
        target=platform.stable,
        available=True,
        checked_at=NOW,
    )
    store.save(
        PersistentState(
            last_check=checked,
            task=UpdateTask(
                id=uuid4(),
                original=platform.current,
                target=platform.stable,
                stage=stage,
                created_at=NOW,
            ),
        )
    )

    with pytest.raises(UpdateConflict, match="^task_active$"):
        manager.check()
    with pytest.raises(UpdateConflict, match="^task_active$"):
        manager.start("v0.2.1")


def test_successful_transaction_persists_a_checkpoint_before_every_operation(
    manager, store, platform
):
    manager.check()
    _task, execute = manager.start("v0.2.1")
    sleeps = []
    manager._sleeper = sleeps.append

    execute()

    state = store.load()
    assert platform.calls == [
        "backup",
        "verify_target",
        "tag_rollback",
        "stop_web",
        "migrate_target",
        "start_target",
        "health_target",
        "stabilize",
        "persist_version",
        "cleanup_version_tag",
        "cleanup_rollback_alias",
        "cleanup_image_id",
    ]
    assert platform.stage_at_call == [
        ("backup", Stage.BACKING_UP),
        ("verify_target", Stage.PULLING),
        ("tag_rollback", Stage.PULLING),
        ("stop_web", Stage.STOPPING_WEB),
        ("migrate_target", Stage.MIGRATING),
        ("start_target", Stage.STARTING_WEB),
        ("health_target", Stage.CHECKING_HEALTH),
        ("health_target", Stage.STABILIZING),
        ("health_target", Stage.STABILIZING),
        ("health_target", Stage.STABILIZING),
        ("persist_version", Stage.PERSISTING_VERSION),
        ("cleanup_version_tag", Stage.CLEANING),
        ("cleanup_rollback_alias", Stage.CLEANING),
        ("cleanup_image_id", Stage.CLEANING),
    ]
    assert sleeps == [5, 5, 5]
    assert state.task is not None
    assert state.task.stage is Stage.SUCCEEDED
    assert state.task.cleanup is CleanupStatus.COMPLETE
    assert state.task.public_view().backup_complete is True
    assert state.last_check is not None
    assert state.last_check.current == platform.stable
    assert state.last_check.available is False
    assert all("db" not in call and "updater" not in call for call in platform.calls)


def test_cleanup_persists_started_before_and_completed_after_every_deletion(
    manager, store, platform
):
    manager.check()
    _task, execute = manager.start("v0.2.1")

    execute()

    assert platform.cleanup_observations == [
        (CleanupStep.VERSION_TAG, CleanupStepStatus.STARTED),
        (CleanupStep.ROLLBACK_ALIAS, CleanupStepStatus.STARTED),
        (CleanupStep.IMAGE_ID, CleanupStepStatus.STARTED),
    ]
    cleaning_journals = [
        saved.task.cleanup_journal
        for saved in store.saved
        if saved.task is not None and saved.task.stage is Stage.CLEANING
    ]
    assert cleaning_journals[-6:] == [
        CleanupJournal(
            CleanupStepStatus.STARTED,
            CleanupStepStatus.NOT_STARTED,
            CleanupStepStatus.NOT_STARTED,
        ),
        CleanupJournal(
            CleanupStepStatus.COMPLETED,
            CleanupStepStatus.NOT_STARTED,
            CleanupStepStatus.NOT_STARTED,
        ),
        CleanupJournal(
            CleanupStepStatus.COMPLETED,
            CleanupStepStatus.STARTED,
            CleanupStepStatus.NOT_STARTED,
        ),
        CleanupJournal(
            CleanupStepStatus.COMPLETED,
            CleanupStepStatus.COMPLETED,
            CleanupStepStatus.NOT_STARTED,
        ),
        CleanupJournal(
            CleanupStepStatus.COMPLETED,
            CleanupStepStatus.COMPLETED,
            CleanupStepStatus.STARTED,
        ),
        CleanupJournal(
            CleanupStepStatus.COMPLETED,
            CleanupStepStatus.COMPLETED,
            CleanupStepStatus.COMPLETED,
        ),
    ]


def test_success_and_refreshed_check_are_committed_once_without_final_inspect(
    manager, store, platform
):
    manager.check()
    platform.fail_inspect_after = 1
    _task, execute = manager.start("v0.2.1")

    execute()

    succeeded = [
        saved
        for saved in store.saved
        if saved.task is not None and saved.task.stage is Stage.SUCCEEDED
    ]
    assert len(succeeded) == 1
    assert succeeded[0].last_check is not None
    assert succeeded[0].last_check.current == platform.stable
    assert platform.inspect_calls == 1


def test_success_with_pending_cleanup_blocks_check_and_start(manager, store, platform):
    manager.check()
    _view, _execute = manager.start("v0.2.1")
    state = store.load()
    assert state.task is not None
    state.task.stage = Stage.SUCCEEDED
    state.task.cleanup = CleanupStatus.PENDING
    store.save(state)

    with pytest.raises(UpdateConflict, match="^task_active$"):
        manager.check()
    with pytest.raises(UpdateConflict, match="^task_active$"):
        manager.start("v0.2.1")


@pytest.mark.parametrize(
    ("blocked_call", "expected_stage"),
    [
        ("backup", Stage.BACKING_UP),
        ("verify_target", Stage.PULLING),
        ("migrate_target", Stage.MIGRATING),
        ("health_target", Stage.CHECKING_HEALTH),
        ("stabilize", Stage.STABILIZING),
    ],
)
def test_status_returns_latest_checkpoint_while_platform_operation_is_blocked(
    manager, platform, blocked_call, expected_stage
):
    manager.check()
    _task, execute = manager.start("v0.2.1")
    platform.block_call = blocked_call
    execution_errors = []
    status_results = []
    status_done = Event()

    def read_status() -> None:
        status_results.append(manager.status())
        status_done.set()

    execution = Thread(target=lambda: _capture_error(execute, execution_errors))
    execution.start()
    assert platform.operation_started.wait(timeout=1)
    status_reader = Thread(target=read_status)
    status_reader.start()
    returned_while_blocked = status_done.wait(timeout=0.2)
    platform.release_operation.set()
    execution.join(timeout=2)
    status_reader.join(timeout=2)

    assert returned_while_blocked is True
    assert execution_errors == []
    assert len(status_results) == 1
    assert status_results[0].task is not None
    assert status_results[0].task.stage is expected_stage


def test_stabilization_uses_three_fixed_delayed_health_probes(manager, platform):
    manager.check()
    _task, execute = manager.start("v0.2.1")
    sleeps = []
    manager._sleeper = sleeps.append

    execute()

    assert sleeps == [5, 5, 5]
    assert platform.health_targets == [platform.stable] * 4


def test_cleanup_failure_leaves_a_successful_task_pending_cleanup(
    manager, store, platform
):
    manager.check()
    _task, execute = manager.start("v0.2.1")
    platform.fail_cleanup = True

    execute()

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.SUCCEEDED
    assert task.cleanup is CleanupStatus.PENDING


def test_pending_cleanup_blocks_until_the_same_journal_is_completed(
    manager, store, platform
):
    manager.check()
    _task, execute = manager.start("v0.2.1")
    platform.fail_cleanup = True
    execute()

    with pytest.raises(UpdateConflict, match="^task_active$"):
        manager.check()
    with pytest.raises(UpdateConflict, match="^task_active$"):
        manager.start("v0.2.1")

    platform.fail_cleanup = False
    completed = manager.complete_pending_cleanup()

    assert completed.task is not None
    assert completed.task.cleanup is CleanupStatus.COMPLETE
    assert manager.check().task is not None


def test_terminal_save_failure_leaves_recoverable_cleaning_state(
    manager, monkeypatch, store, platform
):
    manager.check()
    _task, execute = manager.start("v0.2.1")
    save = store.save
    calls_at_failure = []

    def fail_terminal_save(state: PersistentState) -> None:
        if state.task is not None and state.task.stage is Stage.SUCCEEDED:
            calls_at_failure.extend(platform.calls)
            raise OSError("simulated_terminal_save_failure")
        save(state)

    monkeypatch.setattr(store, "save", fail_terminal_save)

    with pytest.raises(OSError, match="simulated_terminal_save_failure"):
        execute()

    persisted = store.load()
    assert persisted.task is not None
    assert persisted.task.stage is Stage.CLEANING
    assert persisted.task.cleanup_journal is not None
    assert persisted.task.cleanup_journal.complete is True
    assert persisted.last_check is not None
    assert persisted.last_check.available is True
    assert platform.calls == calls_at_failure


def test_execution_closure_cannot_run_a_completed_task_twice(manager, platform):
    manager.check()
    _task, execute = manager.start("v0.2.1")

    execute()
    calls_after_first_run = list(platform.calls)

    with pytest.raises(UpdateConflict, match="^task_not_ready$"):
        execute()
    assert platform.calls == calls_after_first_run


def test_default_clock_produces_a_utc_check_timestamp(store, platform):
    platform.record_stages_from(store)
    manager = UpdateManager(store, platform, sleeper=lambda _: None)

    view = manager.check()

    assert view.checked_at is not None
    assert view.checked_at.tzinfo is UTC
