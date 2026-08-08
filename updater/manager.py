from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import UUID, uuid4

from updater.platform import DockerPlatform, SafeOperationError
from updater.store import FileStateStore
from updater.types import (
    CheckResult,
    CleanupStatus,
    CleanupStep,
    CleanupStepStatus,
    ImageIdentity,
    PersistentState,
    Stage,
    StatusView,
    TaskView,
    UpdateTask,
    version_key,
)


class UpdateConflict(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class UpdateManager:
    _CHECK_MAX_AGE = timedelta(minutes=2)
    _PRE_TAG_STAGES = frozenset(
        {Stage.CHECKING, Stage.BACKING_UP, Stage.PULLING}
    )
    _TARGET_COMPLETION_STAGES = frozenset(
        {Stage.PERSISTING_VERSION, Stage.CLEANING}
    )
    _TERMINAL_STAGES = frozenset(
        {Stage.SUCCEEDED, Stage.FAILED, Stage.MANUAL_INTERVENTION}
    )
    _CLEANUP_STEPS = (
        CleanupStep.VERSION_TAG,
        CleanupStep.ROLLBACK_ALIAS,
        CleanupStep.IMAGE_ID,
    )

    def __init__(
        self,
        store: FileStateStore,
        platform: DockerPlatform,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._store = store
        self._platform = platform
        self._now = now
        self._sleeper = sleeper
        self._operation_lock = RLock()
        self._state_lock = RLock()

    def check(self) -> StatusView:
        with self._operation_lock:
            state = self._load_state()
            self._reject_active_task(state)
            current = self._platform.inspect_web()
            target = self._platform.resolve_stable()
            self._require_matching_repositories(current, target)
            result = CheckResult(
                current=current,
                target=target,
                available=version_key(target.version) > version_key(current.version),
                checked_at=self._now(),
            )
            state = PersistentState(last_check=result, task=state.task)
            self._save_state(state)
            return self._status_from_state(state)

    def start(
        self,
        target_version: str,
        *,
        task_id: UUID | None = None,
    ) -> tuple[TaskView, Callable[[], None]]:
        with self._operation_lock:
            if task_id is not None and not isinstance(task_id, UUID):
                raise UpdateConflict("invalid_task_id")
            state = self._load_state()
            existing = state.task
            if task_id is not None and existing is not None and existing.id == task_id:
                if existing.target.version != target_version:
                    raise UpdateConflict("task_id_conflict")
                execute = (
                    (lambda: self._execute(existing.id))
                    if existing.stage is Stage.CHECKING
                    else (lambda: None)
                )
                return existing.public_view(), execute
            self._reject_active_task(state)
            checked = self._require_fresh_check(state.last_check, target_version)
            resolved = self._platform.resolve_stable()
            if (resolved.version, resolved.digest) != (
                checked.target.version,
                checked.target.digest,
            ):
                raise UpdateConflict("target_changed")
            task = UpdateTask(
                id=task_id or uuid4(),
                original=checked.current,
                target=checked.target,
                stage=Stage.CHECKING,
                created_at=self._now(),
            )
            self._save_state(PersistentState(last_check=checked, task=task))
            return task.public_view(), lambda: self._execute(task.id)

    def status(self) -> StatusView:
        state = self._load_state()
        if state.last_check is not None:
            return self._status_from_state(state)
        current = self._platform.inspect_web()
        return StatusView(
            current_version=current.version,
            current_published_at=current.published_at,
            latest_version=None,
            latest_published_at=None,
            update_available=False,
            checked_at=None,
            task=state.task.public_view() if state.task is not None else None,
        )

    def complete_pending_cleanup(self) -> PersistentState:
        with self._operation_lock:
            state = self._load_state()
            task = state.task
            if (
                task is None
                or task.stage is not Stage.SUCCEEDED
                or task.cleanup is not CleanupStatus.PENDING
                or task.cleanup_journal is None
            ):
                raise UpdateConflict("cleanup_not_pending")
            if not self._cleanup_original(task.id):
                raise UpdateConflict("cleanup_incomplete")
            task = self._task(task.id)
            if task.cleanup_journal is None or not task.cleanup_journal.complete:
                raise UpdateConflict("cleanup_incomplete")
            task.cleanup = CleanupStatus.COMPLETE
            self._save_task(task)
            return self._load_state()

    def recover(self) -> None:
        with self._operation_lock:
            state = self._load_state()
            task = state.task
            if task is None or task.stage in self._TERMINAL_STAGES:
                return
            if not self._has_complete_recovery_identities(task):
                self._finish_manual(task.id)
                return
            try:
                actual = self._platform.inspect_web()
            except SafeOperationError:
                self._finish_manual(task.id)
                return
            if not self._has_complete_image_identity(actual):
                self._finish_manual(task.id)
                return

            is_original = self._same_runtime_identity(actual, task.original)
            is_target = self._same_runtime_identity(actual, task.target)
            if is_original == is_target:
                self._finish_manual(task.id)
                return

            if task.stage in self._TARGET_COMPLETION_STAGES and is_target:
                self._complete_recovered_target(task.id)
                return
            if task.stage in self._PRE_TAG_STAGES:
                if not is_original:
                    self._finish_manual(task.id)
                    return
                self._checkpoint(task.id, task.stage)
                try:
                    self._platform.health(expected=self._task(task.id).original)
                except SafeOperationError:
                    self._finish_manual(task.id)
                    return
                self._finish_failed(task.id, "update_failed", rolled_back=False)
                return
            if not task.original.rollback_alias:
                self._finish_manual(task.id)
                return
            self._rollback_to_original(
                task.id,
                "update_failed",
                restore_original_version=task.stage
                in {
                    Stage.PERSISTING_VERSION,
                    Stage.CLEANING,
                    Stage.ROLLING_BACK,
                },
                preserve_source_stage=True,
            )

    def _execute(self, task_id: UUID) -> None:
        with self._operation_lock:
            self._require_ready_task(task_id)
            web_was_stopped = False
            try:
                self._checkpoint(task_id, Stage.BACKING_UP)
                self._record_backups(task_id, self._platform.create_backup())

                self._checkpoint(task_id, Stage.PULLING)
                self._platform.verify_target(self._task(task_id).target)
                self._checkpoint(task_id, Stage.PULLING)
                task = self._task(task_id)
                self._platform.tag_rollback(task)
                self._save_task(task)

                self._checkpoint(task_id, Stage.STOPPING_WEB)
                web_was_stopped = True
                self._platform.stop_web()
                self._checkpoint(task_id, Stage.MIGRATING)
                task = self._task(task_id)
                self._platform.migrate_target(task.target, task_id=task.id)
                self._checkpoint(task_id, Stage.STARTING_WEB)
                task = self._task(task_id)
                self._platform.start_target(task.target, task_id=task.id)

                self._checkpoint(task_id, Stage.CHECKING_HEALTH)
                self._platform.health(expected=self._task(task_id).target)
                self._stabilize(task_id)

                self._checkpoint(task_id, Stage.PERSISTING_VERSION)
                self._platform.persist_version(self._task(task_id).target.version)
            except SafeOperationError as error:
                self._handle_failure(
                    task_id,
                    self._safe_error_code(error.code),
                    web_was_stopped=web_was_stopped,
                )
                return

            self._checkpoint(task_id, Stage.CLEANING)
            cleanup_complete = self._cleanup_original(task_id)
            self._commit_success(task_id, cleanup_complete)

    def _handle_failure(
        self, task_id: UUID, code: str, *, web_was_stopped: bool
    ) -> None:
        if not web_was_stopped:
            self._finish_failed(task_id, code, rolled_back=False)
            return

        task = self._task(task_id)
        self._rollback_to_original(
            task_id,
            code,
            restore_original_version=task.stage is Stage.PERSISTING_VERSION,
        )

    def _complete_recovered_target(self, task_id: UUID) -> None:
        task = self._task(task_id)
        if task.stage is Stage.CLEANING and task.cleanup_journal is None:
            self._finish_manual(task_id)
            return
        persist_target_version = task.stage is Stage.PERSISTING_VERSION
        try:
            self._checkpoint(task_id, task.stage)
            task = self._task(task_id)
            self._platform.health(expected=task.target)
            if persist_target_version:
                self._checkpoint(task_id, task.stage)
                self._platform.persist_version(task.target.version)
        except SafeOperationError:
            self._rollback_to_original(
                task_id,
                "update_failed",
                restore_original_version=True,
                preserve_source_stage=True,
            )
            return
        if persist_target_version:
            self._checkpoint(task_id, Stage.CLEANING)
        cleanup_complete = self._cleanup_original(task_id)
        self._commit_success(task_id, cleanup_complete)

    def _rollback_to_original(
        self,
        task_id: UUID,
        code: str,
        *,
        restore_original_version: bool,
        preserve_source_stage: bool = False,
    ) -> None:
        task = self._task(task_id)
        checkpoint_stage = task.stage if preserve_source_stage else Stage.ROLLING_BACK
        task.stage = checkpoint_stage
        task.error_code = code
        self._save_task(task)
        try:
            task = self._task(task_id)
            self._platform.start_rollback(task)
            self._checkpoint(task_id, checkpoint_stage)
            self._platform.health(expected=task.original)
            if restore_original_version:
                self._checkpoint(task_id, checkpoint_stage)
                self._platform.persist_version(task.original.version)
        except SafeOperationError:
            self._finish_manual(task_id)
            return
        self._finish_failed(task_id, code, rolled_back=True)

    def _stabilize(self, task_id: UUID) -> None:
        self._checkpoint(task_id, Stage.STABILIZING)
        for _ in range(3):
            self._sleeper(5)
            self._checkpoint(task_id, Stage.STABILIZING)
            self._platform.health(expected=self._task(task_id).target)

    def _cleanup_original(self, task_id: UUID) -> bool:
        task = self._task(task_id)
        if task.cleanup_journal is None:
            return False
        if task.cleanup is CleanupStatus.NOT_RUN:
            task.cleanup = CleanupStatus.PENDING
            self._save_task(task)

        for step in self._CLEANUP_STEPS:
            task = self._task(task_id)
            journal = task.cleanup_journal
            if journal is None:
                return False
            status = getattr(journal, step.value)
            if status is CleanupStepStatus.COMPLETED:
                continue
            if status is CleanupStepStatus.NOT_STARTED:
                task.cleanup_journal = replace(
                    journal,
                    **{step.value: CleanupStepStatus.STARTED},
                )
                self._save_task(task)
            try:
                self._platform.cleanup_original_step(
                    self._task(task_id),
                    step,
                )
            except SafeOperationError:
                return False
            task = self._task(task_id)
            journal = task.cleanup_journal
            if (
                journal is None
                or getattr(journal, step.value) is not CleanupStepStatus.STARTED
            ):
                return False
            task.cleanup_journal = replace(
                journal,
                **{step.value: CleanupStepStatus.COMPLETED},
            )
            self._save_task(task)
        return True

    def _record_backups(self, task_id: UUID, backups: tuple[str, str]) -> None:
        database_backup, uploads_backup = backups
        task = self._task(task_id)
        task.database_backup = database_backup
        task.uploads_backup = uploads_backup
        self._save_task(task)

    def _checkpoint(self, task_id: UUID, stage: Stage) -> None:
        task = self._task(task_id)
        task.stage = stage
        if task.started_at is None:
            task.started_at = self._now()
        self._save_task(task)

    def _commit_success(self, task_id: UUID, cleanup_complete: bool) -> None:
        with self._state_lock:
            state = self._store.load()
            task = self._require_task(state, task_id)
            task.stage = Stage.SUCCEEDED
            task.cleanup = (
                CleanupStatus.COMPLETE
                if cleanup_complete
                else CleanupStatus.PENDING
            )
            task.finished_at = self._now()
            self._store.save(
                PersistentState(
                    last_check=CheckResult(
                        current=task.target,
                        target=task.target,
                        available=False,
                        checked_at=self._now(),
                    ),
                    task=task,
                )
            )

    def _finish_failed(self, task_id: UUID, code: str, *, rolled_back: bool) -> None:
        task = self._task(task_id)
        task.stage = Stage.FAILED
        task.rolled_back = rolled_back
        task.error_code = code
        task.finished_at = self._now()
        self._save_task(task)

    def _finish_manual(self, task_id: UUID) -> None:
        task = self._task(task_id)
        task.stage = Stage.MANUAL_INTERVENTION
        task.error_code = "rollback_failed"
        task.finished_at = self._now()
        self._save_task(task)

    def _task(self, task_id: UUID) -> UpdateTask:
        return self._require_task(self._load_state(), task_id)

    def _save_task(self, task: UpdateTask) -> None:
        with self._state_lock:
            state = self._store.load()
            self._store.save(PersistentState(last_check=state.last_check, task=task))

    def _load_state(self) -> PersistentState:
        with self._state_lock:
            return self._store.load()

    def _save_state(self, state: PersistentState) -> None:
        with self._state_lock:
            self._store.save(state)

    @staticmethod
    def _require_task(state: PersistentState, task_id: UUID) -> UpdateTask:
        if state.task is None or state.task.id != task_id:
            raise UpdateConflict("task_not_found")
        return state.task

    def _require_ready_task(self, task_id: UUID) -> None:
        task = self._task(task_id)
        if task.stage is not Stage.CHECKING:
            raise UpdateConflict("task_not_ready")

    def _require_fresh_check(
        self, checked: CheckResult | None, target_version: str
    ) -> CheckResult:
        version_key(target_version)
        if checked is None:
            raise UpdateConflict("stale_check")
        if target_version != checked.target.version:
            raise UpdateConflict("target_mismatch")
        if not checked.available:
            raise UpdateConflict("no_update")
        age = self._now() - checked.checked_at
        if age < timedelta() or age >= self._CHECK_MAX_AGE:
            raise UpdateConflict("stale_check")
        self._require_matching_repositories(checked.current, checked.target)
        return checked

    @staticmethod
    def _require_matching_repositories(
        current: ImageIdentity, target: ImageIdentity
    ) -> None:
        if current.repository != target.repository:
            raise UpdateConflict("repository_mismatch")

    @staticmethod
    def _safe_error_code(code: str) -> str:
        return code if code in {
            "backup_failed",
            "pull_failed",
            "migration_failed",
            "health_check_failed",
        } else "update_failed"

    @staticmethod
    def _has_complete_recovery_identities(task: UpdateTask) -> bool:
        return UpdateManager._has_complete_image_identity(
            task.original
        ) and UpdateManager._has_complete_image_identity(task.target)

    @staticmethod
    def _has_complete_image_identity(identity: ImageIdentity) -> bool:
        return (
            isinstance(identity, ImageIdentity)
            and all(
                isinstance(value, str) and value
                for value in (
                    identity.repository,
                    identity.version,
                    identity.digest,
                    identity.image_id,
                )
            )
            and isinstance(identity.tags, tuple)
            and bool(identity.tags)
            and all(isinstance(tag, str) and tag for tag in identity.tags)
        )

    @staticmethod
    def _same_runtime_identity(
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

    @staticmethod
    def _reject_active_task(state: PersistentState) -> None:
        task = state.task
        if task is not None and not (
            task.stage is Stage.FAILED
            or (
                task.stage is Stage.SUCCEEDED
                and task.cleanup is CleanupStatus.COMPLETE
            )
        ):
            raise UpdateConflict("task_active")

    @staticmethod
    def _status_from_state(state: PersistentState) -> StatusView:
        checked = state.last_check
        if checked is None:
            raise UpdateConflict("stale_check")
        return StatusView(
            current_version=checked.current.version,
            current_published_at=checked.current.published_at,
            latest_version=checked.target.version,
            latest_published_at=checked.target.published_at,
            update_available=checked.available,
            checked_at=checked.checked_at,
            task=state.task.public_view() if state.task is not None else None,
        )
