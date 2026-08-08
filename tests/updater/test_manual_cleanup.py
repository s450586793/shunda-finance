from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

import updater.platform as updater_platform
import updater.store as updater_store
from updater import manual_cleanup
from updater.platform import SafeOperationError
from updater.store import FileStateStore, StateStoreError
from updater.types import (
    CleanupJournal,
    CleanupStatus,
    CleanupStep,
    CleanupStepStatus,
    ImageIdentity,
    PersistentState,
    Stage,
    UpdateTask,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
REPOSITORY = "ghcr.io/s450586793/shunda-finance-web"


def image(version: str, *, digest: str, image_id: str, rollback_alias: str = "") -> ImageIdentity:
    return ImageIdentity(
        repository=REPOSITORY,
        version=version,
        digest=digest,
        image_id=image_id,
        tags=(f"{REPOSITORY}:{version}",),
        rollback_alias=rollback_alias,
        published_at=NOW,
    )


def task_fixture(*, stage: Stage = Stage.SUCCEEDED, cleanup: CleanupStatus = CleanupStatus.PENDING) -> UpdateTask:
    task_id = uuid4()
    return UpdateTask(
        id=task_id,
        original=image(
            "v0.2.0",
            digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            image_id="sha256:1111111111111111111111111111111111111111111111111111111111111111",
            rollback_alias=f"shunda-finance-rollback-web:{task_id}",
        ),
        target=image(
            "v0.2.1",
            digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            image_id="sha256:2222222222222222222222222222222222222222222222222222222222222222",
        ),
        stage=stage,
        cleanup=cleanup,
        created_at=NOW,
        started_at=NOW,
        finished_at=NOW,
        database_backup="/data/backups/db-20260807-120000.dump",
        uploads_backup="/data/backups/uploads-20260807-120000.tar.gz",
    )


class FakePlatform:
    def __init__(self, failure: Exception | None = None):
        self.failure = failure
        self.calls: list[CleanupStep] = []

    def cleanup_original_step(
        self,
        task: UpdateTask,
        step: CleanupStep,
    ) -> None:
        assert task.cleanup_journal is not None
        assert getattr(task.cleanup_journal, step.value) is CleanupStepStatus.STARTED
        self.calls.append(step)
        if self.failure is not None:
            raise self.failure


def test_build_store_uses_fixed_state_path(monkeypatch):
    calls: list[Path] = []
    expected_store = object()

    monkeypatch.setattr(
        updater_store,
        "FileStateStore",
        lambda path: calls.append(path) or expected_store,
    )

    result = manual_cleanup.build_store()

    assert result is expected_store
    assert calls == [Path("/state/update-state.json")]


def test_build_platform_uses_fixed_constructor(monkeypatch):
    calls: list[str] = []
    expected_platform = object()

    monkeypatch.setattr(
        updater_platform,
        "DockerPlatform",
        lambda: calls.append("platform") or expected_platform,
    )

    result = manual_cleanup.build_platform()

    assert result is expected_platform
    assert calls == ["platform"]


def test_run_pending_cleanup_marks_task_complete_and_persists_atomically(tmp_path):
    path = tmp_path / "update-state.json"
    store = FileStateStore(path)
    state = PersistentState(task=task_fixture())
    store.save(state)
    platform = FakePlatform()

    result = manual_cleanup.run_pending_cleanup(store, platform)

    assert platform.calls == [
        CleanupStep.VERSION_TAG,
        CleanupStep.ROLLBACK_ALIAS,
        CleanupStep.IMAGE_ID,
    ]
    assert result.task is not None
    assert result.task.cleanup is CleanupStatus.COMPLETE
    assert store.load().task is not None
    assert store.load().task.cleanup is CleanupStatus.COMPLETE
    assert store.load().task.cleanup_journal is not None
    assert store.load().task.cleanup_journal.complete is True


def test_run_pending_cleanup_resumes_from_durable_partial_journal(tmp_path):
    path = tmp_path / "update-state.json"
    store = FileStateStore(path)
    task = task_fixture()
    task.cleanup_journal = CleanupJournal(
        version_tag=CleanupStepStatus.COMPLETED,
        rollback_alias=CleanupStepStatus.STARTED,
    )
    store.save(PersistentState(task=task))
    platform = FakePlatform()

    manual_cleanup.run_pending_cleanup(store, platform)

    assert platform.calls == [
        CleanupStep.ROLLBACK_ALIAS,
        CleanupStep.IMAGE_ID,
    ]
    completed = store.load().task
    assert completed is not None
    assert completed.cleanup is CleanupStatus.COMPLETE
    assert completed.cleanup_journal is not None
    assert completed.cleanup_journal.complete is True


@pytest.mark.parametrize(
    "state",
    [
        PersistentState(),
        PersistentState(task=task_fixture(stage=Stage.FAILED)),
        PersistentState(task=task_fixture(cleanup=CleanupStatus.COMPLETE)),
    ],
)
def test_run_pending_cleanup_rejects_missing_nonterminal_or_nonpending_task(tmp_path, state):
    path = tmp_path / "update-state.json"
    store = FileStateStore(path)
    store.save(state)
    platform = FakePlatform()

    with pytest.raises(manual_cleanup.ManualCleanupError, match="^cleanup_not_pending$"):
        manual_cleanup.run_pending_cleanup(store, platform)

    assert platform.calls == []


def test_run_pending_cleanup_propagates_platform_failure_without_persisting_complete(tmp_path):
    path = tmp_path / "update-state.json"
    store = FileStateStore(path)
    state = PersistentState(task=task_fixture())
    store.save(state)
    platform = FakePlatform(SafeOperationError("cleanup_refused"))

    with pytest.raises(manual_cleanup.ManualCleanupError, match="^cleanup_incomplete$"):
        manual_cleanup.run_pending_cleanup(store, platform)

    assert store.load().task is not None
    assert store.load().task.cleanup is CleanupStatus.PENDING


def test_main_returns_zero_and_prints_success(monkeypatch, capsys):
    calls: list[tuple[object, object]] = []
    store = object()
    platform = object()

    monkeypatch.setattr(manual_cleanup, "build_store", lambda: store)
    monkeypatch.setattr(manual_cleanup, "build_platform", lambda: platform)
    monkeypatch.setattr(
        manual_cleanup,
        "run_pending_cleanup",
        lambda actual_store, actual_platform: calls.append((actual_store, actual_platform)),
    )

    assert manual_cleanup.main() == 0
    assert calls == [(store, platform)]
    assert capsys.readouterr().out == "cleanup completed\n"


@pytest.mark.parametrize(
    "error",
    [
        manual_cleanup.ManualCleanupError("cleanup_not_pending"),
        SafeOperationError("cleanup_refused"),
        StateStoreError("invalid_state"),
        OSError("private disk error"),
        ValueError("private parse error"),
        RuntimeError("private runtime error"),
        ImportError("private import error"),
    ],
)
def test_main_returns_one_and_only_prints_fixed_manual_intervention_message(monkeypatch, capsys, error):
    monkeypatch.setattr(manual_cleanup, "build_store", lambda: object())
    monkeypatch.setattr(manual_cleanup, "build_platform", lambda: object())

    def fail(_store, _platform):
        raise error

    monkeypatch.setattr(manual_cleanup, "run_pending_cleanup", fail)

    assert manual_cleanup.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "cleanup requires manual intervention\n"


def test_cli_catches_updater_import_error_without_traceback():
    project_root = Path(__file__).resolve().parents[2]
    program = textwrap.dedent(
        """
        import builtins
        import runpy

        real_import = builtins.__import__

        def fail_updater_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name.startswith("updater."):
                raise ImportError("private updater import sentinel")
            return real_import(name, globals, locals, fromlist, level)

        builtins.__import__ = fail_updater_import
        runpy.run_module("updater.manual_cleanup", run_name="__main__")
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "cleanup requires manual intervention\n"
