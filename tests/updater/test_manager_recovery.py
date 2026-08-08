from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from updater.manager import UpdateManager
from updater.platform import SafeOperationError
from updater.store import FileStateStore
from updater.types import (
    CheckResult,
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
PRE_TAG_STAGES = (Stage.CHECKING, Stage.BACKING_UP, Stage.PULLING)
POST_TAG_STAGES = tuple(stage for stage in Stage if stage not in {
    *PRE_TAG_STAGES,
    Stage.SUCCEEDED,
    Stage.FAILED,
    Stage.MANUAL_INTERVENTION,
})
NONTERMINAL_STAGES = PRE_TAG_STAGES + POST_TAG_STAGES
POST_TAG_ROLLBACK_CASES = tuple(
    (stage, actual)
    for stage in POST_TAG_STAGES
    for actual in ("original", "target")
    if not (
        actual == "target"
        and stage in {Stage.PERSISTING_VERSION, Stage.CLEANING}
    )
)


def image(version: str, digest: str) -> ImageIdentity:
    return ImageIdentity(
        repository=REPOSITORY,
        version=version,
        digest=digest,
        image_id=f"image-{version}",
        tags=(f"{REPOSITORY}:{version}",),
        published_at=NOW,
    )


class RecoveryPlatform:
    def __init__(self):
        self.original = image("v0.2.0", "sha256:original")
        self.target = image("v0.2.1", "sha256:target")
        self.unrelated = image("v0.9.9", "sha256:unrelated")
        self.actual = self.original
        self.calls: list[str] = []
        self.persisted_versions: list[str] = []
        self.cleanup_calls: list[UpdateTask] = []
        self.cleanup_observations: list[tuple[CleanupStep, CleanupStepStatus]] = []
        self.deleted_cleanup_steps: set[CleanupStep] = set()
        self.interrupt_after_cleanup_step: CleanupStep | None = None
        self.fail_target_health = False
        self.fail_rollback = False
        self.fail_rollback_health = False
        self.fail_target_persist = False

    def inspect_web(self) -> ImageIdentity:
        self.calls.append("inspect")
        return self.actual

    def start_rollback(self, task: UpdateTask) -> None:
        assert task.original.rollback_alias == f"shunda-finance-rollback-web:{task.id}"
        self.calls.append("start_rollback")
        if self.fail_rollback:
            raise SafeOperationError("rollback_failed")
        self.actual = self.original

    def health(self, expected: ImageIdentity | None = None) -> None:
        assert expected is not None
        if self._matches_runtime_identity(expected, self.target):
            self.calls.append("health_target")
            if self.fail_target_health:
                raise SafeOperationError("health_check_failed")
            assert self.actual == self.target
            return
        assert self._matches_runtime_identity(expected, self.original)
        self.calls.append("health_rollback")
        if self.fail_rollback_health:
            raise SafeOperationError("health_check_failed")
        assert self.actual == self.original

    def persist_version(self, version: str) -> None:
        self.persisted_versions.append(version)
        self.calls.append(f"persist:{version}")
        if version == self.target.version and self.fail_target_persist:
            raise SafeOperationError("persist_failed")

    def cleanup_original_step(
        self,
        task: UpdateTask,
        step: CleanupStep,
    ) -> None:
        self.cleanup_calls.append(task)
        assert task.cleanup_journal is not None
        status = getattr(task.cleanup_journal, step.value)
        self.cleanup_observations.append((step, status))
        self.calls.append(f"cleanup:{step.value}")
        assert status is CleanupStepStatus.STARTED
        if step not in self.deleted_cleanup_steps:
            self.deleted_cleanup_steps.add(step)
        if self.interrupt_after_cleanup_step is step:
            self.interrupt_after_cleanup_step = None
            raise RuntimeError("simulated_process_interruption")

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


def active_state(stage: Stage, platform: RecoveryPlatform) -> PersistentState:
    task_id = uuid4()
    original = platform.original
    if stage not in PRE_TAG_STAGES:
        original = replace(
            original,
            rollback_alias=f"shunda-finance-rollback-web:{task_id}",
        )
    task = UpdateTask(
        id=task_id,
        original=original,
        target=platform.target,
        stage=stage,
        created_at=NOW,
    )
    return PersistentState(
        last_check=CheckResult(
            current=platform.original,
            target=platform.target,
            available=True,
            checked_at=NOW,
        ),
        task=task,
    )


@pytest.fixture
def store(tmp_path: Path) -> FileStateStore:
    return FileStateStore(tmp_path / "update-state.json")


@pytest.fixture
def platform() -> RecoveryPlatform:
    return RecoveryPlatform()


@pytest.fixture
def manager(store, platform) -> UpdateManager:
    return UpdateManager(store, platform, now=lambda: NOW, sleeper=lambda _: None)


@pytest.mark.parametrize("stage", PRE_TAG_STAGES)
def test_recover_pre_tag_original_runtime_marks_failed_without_rollback(
    manager, store, platform, stage
):
    store.save(active_state(stage, platform))

    manager.recover()

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.FAILED
    assert task.rolled_back is False
    assert task.error_code == "update_failed"
    assert platform.calls == ["inspect", "health_rollback"]
    assert platform.cleanup_calls == []


@pytest.mark.parametrize(("stage", "actual"), POST_TAG_ROLLBACK_CASES)
def test_recover_post_tag_runtime_restores_original_alias_without_cleanup(
    manager, store, platform, stage, actual
):
    platform.actual = getattr(platform, actual)
    store.save(active_state(stage, platform))

    manager.recover()

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.FAILED
    assert task.rolled_back is True
    assert task.error_code == "update_failed"
    expected_calls = ["inspect", "start_rollback", "health_rollback"]
    if stage in {Stage.PERSISTING_VERSION, Stage.CLEANING, Stage.ROLLING_BACK}:
        expected_calls.append("persist:v0.2.0")
    assert platform.calls == expected_calls
    assert platform.cleanup_calls == []


def test_recover_original_health_follows_source_stage_checkpoint(
    manager, monkeypatch, store, platform
):
    store.save(active_state(Stage.CHECKING, platform))
    events: list[str] = []
    save = store.save
    health = platform.health

    def record_save(state: PersistentState) -> None:
        assert state.task is not None
        events.append(f"save:{state.task.stage}")
        save(state)

    def record_health(expected: ImageIdentity | None = None) -> None:
        events.append("health")
        health(expected)

    monkeypatch.setattr(store, "save", record_save)
    monkeypatch.setattr(platform, "health", record_health)

    manager.recover()

    assert events[:2] == ["save:checking", "health"]


def test_recover_target_actions_follow_recovery_checkpoints(
    manager, monkeypatch, store, platform
):
    platform.actual = platform.target
    store.save(active_state(Stage.PERSISTING_VERSION, platform))
    events: list[str] = []
    save = store.save
    health = platform.health
    persist_version = platform.persist_version

    def record_save(state: PersistentState) -> None:
        assert state.task is not None
        events.append(f"save:{state.task.stage}")
        save(state)

    def record_health(expected: ImageIdentity | None = None) -> None:
        events.append("health")
        health(expected)

    def record_persist_version(version: str) -> None:
        events.append("persist")
        persist_version(version)

    monkeypatch.setattr(store, "save", record_save)
    monkeypatch.setattr(platform, "health", record_health)
    monkeypatch.setattr(platform, "persist_version", record_persist_version)

    manager.recover()

    assert events[:4] == [
        "save:persisting_version",
        "health",
        "save:persisting_version",
        "persist",
    ]


@pytest.mark.parametrize("stage", (Stage.PERSISTING_VERSION, Stage.CLEANING))
def test_recover_target_restart_keeps_completion_source_stage(
    manager, monkeypatch, store, platform, stage
):
    platform.actual = platform.target
    store.save(active_state(stage, platform))

    def interrupt_health(expected: ImageIdentity | None = None) -> None:
        raise RuntimeError("simulated_process_interruption")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(platform, "health", interrupt_health)
        with pytest.raises(RuntimeError, match="simulated_process_interruption"):
            manager.recover()

    interrupted_state = store.load()
    assert interrupted_state.task is not None
    assert interrupted_state.task.stage is stage

    restarted = UpdateManager(store, platform, now=lambda: NOW, sleeper=lambda _: None)
    restarted.recover()

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.SUCCEEDED
    assert task.cleanup is CleanupStatus.COMPLETE
    assert task.rolled_back is False


def test_recover_cleaning_restart_target_health_failure_restores_original_env(
    manager, monkeypatch, store, platform
):
    platform.actual = platform.target
    store.save(active_state(Stage.CLEANING, platform))

    def interrupt_health(expected: ImageIdentity | None = None) -> None:
        raise RuntimeError("simulated_process_interruption")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(platform, "health", interrupt_health)
        with pytest.raises(RuntimeError, match="simulated_process_interruption"):
            manager.recover()

    platform.fail_target_health = True
    restarted = UpdateManager(store, platform, now=lambda: NOW, sleeper=lambda _: None)
    restarted.recover()

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.FAILED
    assert task.rolled_back is True
    assert task.error_code == "update_failed"
    assert platform.persisted_versions == ["v0.2.0"]


def test_recover_pre_tag_original_restart_keeps_pre_tag_source_stage(
    manager, monkeypatch, store, platform
):
    store.save(active_state(Stage.CHECKING, platform))

    def interrupt_health(expected: ImageIdentity | None = None) -> None:
        raise RuntimeError("simulated_process_interruption")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(platform, "health", interrupt_health)
        with pytest.raises(RuntimeError, match="simulated_process_interruption"):
            manager.recover()

    interrupted_state = store.load()
    assert interrupted_state.task is not None
    assert interrupted_state.task.stage is Stage.CHECKING

    restarted = UpdateManager(store, platform, now=lambda: NOW, sleeper=lambda _: None)
    restarted.recover()

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.FAILED
    assert task.rolled_back is False
    assert task.error_code == "update_failed"


def test_recover_target_commits_terminal_task_and_check_without_refresh_inspect(
    manager, monkeypatch, store, platform
):
    platform.actual = platform.target
    store.save(active_state(Stage.CLEANING, platform))
    saved: list[PersistentState] = []
    save = store.save
    inspect_web = platform.inspect_web
    inspect_count = 0

    def record_save(state: PersistentState) -> None:
        saved.append(deepcopy(state))
        save(state)

    def fail_refresh_inspect() -> ImageIdentity:
        nonlocal inspect_count
        inspect_count += 1
        if inspect_count > 1:
            raise SafeOperationError("inspect_failed")
        return inspect_web()

    monkeypatch.setattr(store, "save", record_save)
    monkeypatch.setattr(platform, "inspect_web", fail_refresh_inspect)

    manager.recover()

    assert inspect_count == 1
    assert saved[-1].task is not None
    assert saved[-1].task.stage is Stage.SUCCEEDED
    assert saved[-1].task.cleanup is CleanupStatus.COMPLETE
    assert saved[-1].last_check is not None
    assert saved[-1].last_check.current == platform.target
    assert saved[-1].last_check.available is False


@pytest.mark.parametrize("stage", (Stage.PERSISTING_VERSION, Stage.CLEANING))
def test_recover_healthy_target_resumes_and_completes_cleanup(
    manager, store, platform, stage
):
    platform.actual = platform.target
    store.save(active_state(stage, platform))

    manager.recover()

    state = store.load()
    assert state.task is not None
    assert state.task.stage is Stage.SUCCEEDED
    assert state.task.cleanup is CleanupStatus.COMPLETE
    assert state.last_check is not None
    assert state.last_check.current == platform.target
    expected_calls = ["inspect", "health_target"]
    if stage is Stage.PERSISTING_VERSION:
        expected_calls.append("persist:v0.2.1")
    expected_calls.extend(
        [
            "cleanup:version_tag",
            "cleanup:rollback_alias",
            "cleanup:image_id",
        ]
    )
    assert platform.calls == expected_calls
    assert len(platform.cleanup_calls) == 3


@pytest.mark.parametrize("interrupted_step", tuple(CleanupStep))
def test_recover_cleanup_resumes_after_crash_immediately_after_each_deletion(
    manager, store, platform, interrupted_step
):
    platform.actual = platform.target
    platform.interrupt_after_cleanup_step = interrupted_step
    store.save(active_state(Stage.CLEANING, platform))

    with pytest.raises(RuntimeError, match="simulated_process_interruption"):
        manager.recover()

    interrupted = store.load().task
    assert interrupted is not None
    assert interrupted.stage is Stage.CLEANING
    assert interrupted.cleanup_journal is not None
    assert (
        getattr(interrupted.cleanup_journal, interrupted_step.value)
        is CleanupStepStatus.STARTED
    )

    restarted = UpdateManager(store, platform, now=lambda: NOW, sleeper=lambda _: None)
    restarted.recover()

    completed = store.load().task
    assert completed is not None
    assert completed.stage is Stage.SUCCEEDED
    assert completed.cleanup is CleanupStatus.COMPLETE
    assert completed.cleanup_journal is not None
    assert completed.cleanup_journal.complete is True
    assert platform.cleanup_observations.count(
        (interrupted_step, CleanupStepStatus.STARTED)
    ) == 2


def test_recover_legacy_cleaning_state_without_journal_fails_closed_to_manual(
    manager, store, platform
):
    platform.actual = platform.target
    state = active_state(Stage.CLEANING, platform)
    assert state.task is not None
    state.task.cleanup_journal = None
    store.save(state)

    manager.recover()

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.MANUAL_INTERVENTION
    assert task.error_code == "rollback_failed"
    assert platform.cleanup_calls == []


@pytest.mark.parametrize("stage", NONTERMINAL_STAGES)
@pytest.mark.parametrize("actual", ["unrelated", "missing"])
def test_recover_ambiguous_or_missing_runtime_enters_manual_without_cleanup(
    manager, store, platform, stage, actual
):
    platform.actual = (
        platform.unrelated
        if actual == "unrelated"
        else replace(platform.original, digest="", image_id="")
    )
    store.save(active_state(stage, platform))

    manager.recover()

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.MANUAL_INTERVENTION
    assert task.error_code == "rollback_failed"
    assert task.public_view().error_code == "rollback_failed"
    assert task.public_view().error_message == "升级失败，需要人工处理。"
    assert platform.calls == ["inspect"]
    assert platform.cleanup_calls == []


@pytest.mark.parametrize("stage", PRE_TAG_STAGES)
def test_recover_target_before_rollback_alias_enters_manual(
    manager, store, platform, stage
):
    platform.actual = platform.target
    store.save(active_state(stage, platform))

    manager.recover()

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.MANUAL_INTERVENTION
    assert task.error_code == "rollback_failed"
    assert platform.calls == ["inspect"]
    assert platform.cleanup_calls == []


@pytest.mark.parametrize("stage", (Stage.PERSISTING_VERSION, Stage.CLEANING))
def test_recover_unhealthy_target_rolls_back_and_restores_env(
    manager, store, platform, stage
):
    platform.actual = platform.target
    platform.fail_target_health = True
    store.save(active_state(stage, platform))

    manager.recover()

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.FAILED
    assert task.rolled_back is True
    assert platform.calls == [
        "inspect",
        "health_target",
        "start_rollback",
        "health_rollback",
        "persist:v0.2.0",
    ]
    assert platform.cleanup_calls == []


def test_recover_target_persist_failure_rolls_back_and_restores_env(
    manager, store, platform
):
    platform.actual = platform.target
    platform.fail_target_persist = True
    store.save(active_state(Stage.PERSISTING_VERSION, platform))

    manager.recover()

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.FAILED
    assert task.rolled_back is True
    assert task.error_code == "update_failed"
    assert platform.calls == [
        "inspect",
        "health_target",
        "persist:v0.2.1",
        "start_rollback",
        "health_rollback",
        "persist:v0.2.0",
    ]
    assert platform.cleanup_calls == []


@pytest.mark.parametrize("failure", ["start", "health"])
def test_recover_rollback_failure_enters_manual_without_cleanup(
    manager, store, platform, failure
):
    platform.actual = platform.target
    platform.fail_rollback = failure == "start"
    platform.fail_rollback_health = failure == "health"
    store.save(active_state(Stage.MIGRATING, platform))

    manager.recover()

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.MANUAL_INTERVENTION
    assert task.rolled_back is False
    assert task.error_code == "rollback_failed"
    assert platform.cleanup_calls == []


@pytest.mark.parametrize(
    "stage", (Stage.SUCCEEDED, Stage.FAILED, Stage.MANUAL_INTERVENTION)
)
def test_recover_terminal_task_is_a_noop(manager, store, platform, stage):
    state = active_state(stage, platform)
    store.save(state)

    manager.recover()

    assert store.load() == state
    assert platform.calls == []
    assert platform.cleanup_calls == []


def test_recover_without_task_is_a_noop(manager, store, platform):
    store.save(PersistentState())

    manager.recover()

    assert store.load() == PersistentState()
    assert platform.calls == []
    assert platform.cleanup_calls == []


def test_recover_missing_rollback_alias_enters_manual_without_cleanup(
    manager, store, platform
):
    state = active_state(Stage.MIGRATING, platform)
    assert state.task is not None
    state.task.original = replace(state.task.original, rollback_alias="")
    platform.actual = platform.target
    store.save(state)

    manager.recover()

    task = store.load().task
    assert task is not None
    assert task.stage is Stage.MANUAL_INTERVENTION
    assert task.error_code == "rollback_failed"
    assert platform.calls == ["inspect"]
    assert platform.cleanup_calls == []
