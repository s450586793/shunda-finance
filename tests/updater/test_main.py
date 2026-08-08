from __future__ import annotations

import signal
from pathlib import Path
from threading import Event, get_ident

import pytest

import updater.main as main_module
from updater.config import ConfigError, UpdaterConfig
from updater.platform import PlatformConfig

TOKEN = "t" * 32


class FakeManager:
    def __init__(self, events: list[str], failure: Exception | None = None):
        self.events = events
        self.failure = failure
        self.active_state = {"stage": "backing_up"}

    def recover(self) -> None:
        self.events.append("recover")
        if self.failure is not None:
            raise self.failure


class FakeServer:
    def __init__(self, events: list[str]):
        self.events = events
        self.shutdown_calls = 0
        self.shutdown_thread_ids: list[int] = []
        self.shutdown_started = Event()
        self.shutdown_finished = Event()
        self.close_calls = 0
        self.shutdown_failures = 0
        self.shutdown_release: Event | None = None

    def serve_forever(self, *, poll_interval: float) -> None:
        self.events.append(f"serve:{poll_interval}")

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.shutdown_thread_ids.append(get_ident())
        self.shutdown_started.set()
        self.events.append("shutdown")
        if self.shutdown_failures:
            self.shutdown_failures -= 1
            self.shutdown_finished.set()
            raise RuntimeError("shutdown failed")
        if self.shutdown_release is not None:
            assert self.shutdown_release.wait(timeout=1)
        self.shutdown_finished.set()

    def server_close(self) -> None:
        self.close_calls += 1
        self.events.append("close")


@pytest.fixture
def config() -> UpdaterConfig:
    return UpdaterConfig(
        token=TOKEN,
        listen=("0.0.0.0", 8090),
        state_file=Path("/state/update-state.json"),
        platform=PlatformConfig(),
    )


def test_build_manager_uses_fixed_platform_constructor(monkeypatch, config):
    calls: list[object] = []
    store = object()
    platform = object()
    expected_manager = object()

    monkeypatch.setattr(
        main_module, "FileStateStore", lambda path: calls.append(path) or store
    )
    monkeypatch.setattr(
        main_module, "DockerPlatform", lambda: calls.append("platform") or platform
    )
    monkeypatch.setattr(
        main_module,
        "UpdateManager",
        lambda actual_store, actual_platform: (
            calls.append((actual_store, actual_platform)) or expected_manager
        ),
    )

    result = main_module.build_manager(config)

    assert result is expected_manager
    assert calls == [Path("/state/update-state.json"), "platform", (store, platform)]


def test_main_recovers_before_creating_listener_and_preserves_active_state(
    monkeypatch, config
):
    events: list[str] = []
    manager = FakeManager(events)
    server = FakeServer(events)
    handlers: dict[int, object] = {}
    monkeypatch.setattr(main_module.UpdaterConfig, "from_env", lambda environ: config)
    monkeypatch.setattr(main_module, "build_manager", lambda actual_config: manager)
    monkeypatch.setattr(
        main_module,
        "build_server",
        lambda actual_config, actual_manager: events.append("build_server") or server,
    )
    monkeypatch.setattr(
        main_module.signal,
        "signal",
        lambda number, handler: handlers.setdefault(number, handler),
    )

    assert main_module.main() == 0

    assert events == ["recover", "build_server", "serve:0.5", "close"]
    assert server.close_calls == 1
    assert manager.active_state == {"stage": "backing_up"}
    assert set(handlers) == {signal.SIGINT, signal.SIGTERM}


def test_recovery_failure_prevents_listener_creation(monkeypatch, config):
    events: list[str] = []
    manager = FakeManager(events, RuntimeError("private recovery failure"))
    monkeypatch.setattr(main_module.UpdaterConfig, "from_env", lambda environ: config)
    monkeypatch.setattr(main_module, "build_manager", lambda actual_config: manager)
    monkeypatch.setattr(
        main_module, "build_server", lambda *_args: pytest.fail("listener created")
    )

    assert main_module.main() == 1
    assert events == ["recover"]


def test_shutdown_signal_handler_uses_one_non_daemon_thread_and_is_idempotent(
    monkeypatch,
):
    events: list[str] = []
    server = FakeServer(events)
    handlers: dict[int, object] = {}
    monkeypatch.setattr(
        main_module.signal,
        "signal",
        lambda number, handler: handlers.setdefault(number, handler),
    )

    controller = main_module.install_shutdown_handlers(server)

    handlers[signal.SIGTERM](signal.SIGTERM, None)  # type: ignore[operator]
    handlers[signal.SIGINT](signal.SIGINT, None)  # type: ignore[operator]
    assert server.shutdown_started.wait(timeout=1)
    controller.join()
    assert server.shutdown_calls == 1
    assert server.shutdown_thread_ids[0] != get_ident()
    assert controller._thread is not None
    assert controller._thread.daemon is False


def test_main_joins_shutdown_thread_and_closes_server(monkeypatch, config):
    events: list[str] = []
    manager = FakeManager(events)
    server = FakeServer(events)
    handlers: dict[int, object] = {}
    monkeypatch.setattr(main_module.UpdaterConfig, "from_env", lambda environ: config)
    monkeypatch.setattr(main_module, "build_manager", lambda actual_config: manager)
    monkeypatch.setattr(main_module, "build_server", lambda *_args: server)
    monkeypatch.setattr(
        main_module.signal,
        "signal",
        lambda number, handler: handlers.setdefault(number, handler),
    )

    def serve_forever(*, poll_interval: float) -> None:
        events.append(f"serve:{poll_interval}")
        handlers[signal.SIGTERM](signal.SIGTERM, None)  # type: ignore[operator]
        assert server.shutdown_started.wait(timeout=1)

    server.serve_forever = serve_forever  # type: ignore[method-assign]

    assert main_module.main() == 0
    assert server.shutdown_calls == 1
    assert server.close_calls == 1
    assert events[-1] == "close"


def test_main_restores_previous_signal_handlers_after_close_when_serving_fails(
    monkeypatch, config
):
    events: list[object] = []
    manager = FakeManager(events)
    server = FakeServer(events)
    original_int = object()
    original_term = object()
    signal_calls: list[tuple[int, object]] = []
    originals = {signal.SIGINT: original_int, signal.SIGTERM: original_term}
    monkeypatch.setattr(main_module.UpdaterConfig, "from_env", lambda environ: config)
    monkeypatch.setattr(main_module, "build_manager", lambda actual_config: manager)
    monkeypatch.setattr(main_module, "build_server", lambda *_args: server)
    monkeypatch.setattr(
        main_module.signal, "getsignal", lambda number: originals[number]
    )

    def replace_handler(number: int, handler: object) -> None:
        signal_calls.append((number, handler))
        events.append(("signal", number, handler))

    monkeypatch.setattr(main_module.signal, "signal", replace_handler)

    def fail_serve(*, poll_interval: float) -> None:
        events.append(f"serve:{poll_interval}")
        raise RuntimeError("private serving failure")

    server.serve_forever = fail_serve  # type: ignore[method-assign]

    assert main_module.main() == 1
    assert server.close_calls == 1
    assert [number for number, _handler in signal_calls] == [
        signal.SIGINT,
        signal.SIGTERM,
        signal.SIGINT,
        signal.SIGTERM,
    ]
    assert callable(signal_calls[0][1])
    assert callable(signal_calls[1][1])
    assert signal_calls[2:] == [
        (signal.SIGINT, original_int),
        (signal.SIGTERM, original_term),
    ]
    assert events[-3:] == [
        "close",
        ("signal", signal.SIGINT, original_int),
        ("signal", signal.SIGTERM, original_term),
    ]


def test_shutdown_failure_can_be_retried_by_a_later_signal(monkeypatch):
    events: list[str] = []
    server = FakeServer(events)
    server.shutdown_failures = 1
    handlers: dict[int, object] = {}
    monkeypatch.setattr(
        main_module.signal,
        "signal",
        lambda number, handler: handlers.setdefault(number, handler),
    )

    controller = main_module.install_shutdown_handlers(server)
    handlers[signal.SIGTERM](signal.SIGTERM, None)  # type: ignore[operator]
    assert server.shutdown_started.wait(timeout=1)
    controller.join()
    assert server.shutdown_calls == 1
    first_thread = controller._thread

    handlers[signal.SIGINT](signal.SIGINT, None)  # type: ignore[operator]
    controller.join()
    assert server.shutdown_calls == 2
    assert controller._thread is not first_thread


def test_finalization_captures_active_thread_and_rejects_repeated_signals(monkeypatch):
    events: list[str] = []
    server = FakeServer(events)
    server.shutdown_release = Event()
    handlers: dict[int, object] = {}
    monkeypatch.setattr(
        main_module.signal,
        "signal",
        lambda number, handler: handlers.setdefault(number, handler),
    )

    controller = main_module.install_shutdown_handlers(server)
    handlers[signal.SIGTERM](signal.SIGTERM, None)  # type: ignore[operator]
    assert server.shutdown_started.wait(timeout=1)

    captured = controller.begin_finalization()
    handlers[signal.SIGINT](signal.SIGINT, None)  # type: ignore[operator]
    assert captured is controller._thread
    assert server.shutdown_calls == 1

    server.shutdown_release.set()
    assert captured is not None
    captured.join(timeout=1)
    assert not captured.is_alive()


def test_main_finalization_blocks_retry_after_failed_shutdown(monkeypatch, config):
    events: list[str] = []
    manager = FakeManager(events)
    server = FakeServer(events)
    server.shutdown_failures = 1
    handlers: dict[int, object] = {}
    monkeypatch.setattr(main_module.UpdaterConfig, "from_env", lambda environ: config)
    monkeypatch.setattr(main_module, "build_manager", lambda actual_config: manager)
    monkeypatch.setattr(main_module, "build_server", lambda *_args: server)
    monkeypatch.setattr(
        main_module.signal,
        "signal",
        lambda number, handler: handlers.setdefault(number, handler),
    )

    def serve_forever(*, poll_interval: float) -> None:
        events.append(f"serve:{poll_interval}")
        handlers[signal.SIGTERM](signal.SIGTERM, None)  # type: ignore[operator]
        assert server.shutdown_started.wait(timeout=1)
        assert server.shutdown_finished.wait(timeout=1)

    def close_with_late_signal() -> None:
        server.close_calls += 1
        events.append("close")
        handlers[signal.SIGINT](signal.SIGINT, None)  # type: ignore[operator]

    server.serve_forever = serve_forever  # type: ignore[method-assign]
    server.server_close = close_with_late_signal  # type: ignore[method-assign]

    assert main_module.main() == 0
    assert server.shutdown_calls == 1
    assert server.close_calls == 1


def test_main_hides_token_when_startup_configuration_fails(monkeypatch, capsys):
    secret = "private-token-value-must-never-appear"

    def fail_config(_environ):
        raise ConfigError(f"bad config {secret}")

    monkeypatch.setattr(main_module.UpdaterConfig, "from_env", fail_config)

    assert main_module.main() == 2
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert captured.err == "updater startup failed\n"
