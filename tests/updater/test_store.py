import json
import os
import stat
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from updater.store import FileStateStore, StateStoreError
from updater.types import (
    CheckResult,
    ImageIdentity,
    PersistentState,
    Stage,
    UpdateTask,
)


def test_store_returns_empty_state_for_missing_file(tmp_path):
    assert FileStateStore(tmp_path / "missing.json").load() == PersistentState()


def test_store_round_trip_uses_private_permissions(tmp_path):
    path = tmp_path / "nested" / "update-state.json"
    store = FileStateStore(path)
    state = persistent_state_fixture()

    store.save(state)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert store.load() == state


def test_store_rejects_state_larger_than_one_mebibyte(tmp_path):
    path = tmp_path / "update-state.json"
    write_private_file(path, b"x" * (FileStateStore.MAX_BYTES + 1))

    with pytest.raises(StateStoreError, match="state_too_large"):
        FileStateStore(path).load()


def test_store_rejects_unknown_json_fields(tmp_path):
    path = tmp_path / "update-state.json"
    write_private_file(
        path,
        json.dumps({"last_check": None, "task": None, "unexpected": True}).encode(),
    )

    with pytest.raises(StateStoreError, match="invalid_state"):
        FileStateStore(path).load()


@pytest.mark.parametrize(
    ("field", "value"),
    [("stage", "unknown"), ("id", "not-a-uuid")],
)
def test_store_rejects_invalid_enum_and_uuid(tmp_path, field, value):
    state = persistent_state_fixture().to_dict()
    state["task"][field] = value
    path = tmp_path / "update-state.json"
    write_private_file(path, json.dumps(state).encode())

    with pytest.raises(StateStoreError, match="invalid_state"):
        FileStateStore(path).load()


def test_store_rejects_symlink_state_file(tmp_path):
    target = tmp_path / "target.json"
    write_private_file(target, json.dumps(PersistentState().to_dict()).encode())
    path = tmp_path / "update-state.json"
    path.symlink_to(target)

    with pytest.raises(StateStoreError, match="^unsafe_state_path$"):
        FileStateStore(path).load()


def test_store_rejects_non_regular_state_file(tmp_path):
    path = tmp_path / "update-state.json"
    path.mkdir()

    with pytest.raises(StateStoreError, match="^unsafe_state_path$"):
        FileStateStore(path).load()


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o644])
def test_store_rejects_state_file_without_exact_private_mode(tmp_path, mode):
    path = tmp_path / "update-state.json"
    write_private_file(path, json.dumps(PersistentState().to_dict()).encode())
    path.chmod(mode)

    with pytest.raises(StateStoreError, match="^unsafe_state_path$"):
        FileStateStore(path).load()


def test_store_rejects_state_under_symlink_parent(tmp_path):
    real_parent = tmp_path / "real-state"
    real_parent.mkdir(mode=0o700)
    write_private_file(
        real_parent / "update-state.json",
        json.dumps(PersistentState().to_dict()).encode(),
    )
    linked_parent = tmp_path / "linked-state"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(StateStoreError, match="^unsafe_state_path$"):
        FileStateStore(linked_parent / "update-state.json").load()


def test_store_rejects_state_under_non_private_parent(tmp_path):
    parent = tmp_path / "state"
    parent.mkdir(mode=0o700)
    path = parent / "update-state.json"
    write_private_file(path, json.dumps(PersistentState().to_dict()).encode())
    parent.chmod(0o750)

    with pytest.raises(StateStoreError, match="^unsafe_state_path$"):
        FileStateStore(path).load()


def test_store_rejects_missing_state_under_symlink_parent(tmp_path):
    real_parent = tmp_path / "real-state"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-state"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(StateStoreError, match="^unsafe_state_path$"):
        FileStateStore(linked_parent / "missing.json").load()


def test_store_rejects_missing_state_under_non_private_parent(tmp_path):
    parent = tmp_path / "state"
    parent.mkdir(mode=0o750)

    with pytest.raises(StateStoreError, match="^unsafe_state_path$"):
        FileStateStore(parent / "missing.json").load()


def test_store_removes_temporary_file_when_replacement_fails(tmp_path, monkeypatch):
    path = tmp_path / "update-state.json"
    store = FileStateStore(path)

    def fail_replace(source, destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr("updater.store.os.replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        store.save(persistent_state_fixture())

    assert list(tmp_path.iterdir()) == []


def test_store_preserves_previous_valid_state_when_replacement_fails(
    tmp_path, monkeypatch
):
    path = tmp_path / "update-state.json"
    store = FileStateStore(path)
    original = persistent_state_fixture()
    replacement = persistent_state_fixture()
    store.save(original)

    def fail_replace(source, destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr("updater.store.os.replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        store.save(replacement)

    assert store.load() == original


def test_store_fsyncs_state_file_and_parent_directory(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "update-state.json"
    calls = []
    real_fsync = os.fsync

    def record_fsync(file_descriptor):
        calls.append(file_descriptor)
        return real_fsync(file_descriptor)

    monkeypatch.setattr("updater.store.os.fsync", record_fsync)

    FileStateStore(path).save(persistent_state_fixture())

    assert len(calls) == 2


def write_private_file(path, payload):
    path.write_bytes(payload)
    path.chmod(0o600)


def persistent_state_fixture():
    original = ImageIdentity(
        repository="ghcr.io/example/web",
        version="v0.2.1",
        digest="sha256:original",
        image_id="original-image",
    )
    target = ImageIdentity(
        repository="ghcr.io/example/web",
        version="v0.3.0",
        digest="sha256:target",
        image_id="target-image",
    )
    task = UpdateTask(
        id=uuid4(),
        original=original,
        target=target,
        stage=Stage.PULLING,
        created_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
    )
    return PersistentState(
        last_check=CheckResult(
            current=original,
            target=target,
            available=True,
            checked_at=datetime(2026, 8, 7, 12, 1, tzinfo=UTC),
        ),
        task=task,
    )
