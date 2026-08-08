import logging
import os
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from updater.runner import MAX_OUTPUT_BYTES, CommandRunner, SafeCommandError

FIXED_ENVIRONMENT = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C",
}


def test_runner_uses_exact_argv_without_shell_or_inherited_environment(monkeypatch):
    monkeypatch.setenv("SHUNDA_UPDATER_TOKEN", "token-must-not-be-inherited")
    literal_argument = "literal;$(printf shell-expanded)"
    script = (
        "import os,sys;"
        "payload=sys.stdin.buffer.read();"
        "token=os.environ.get('SHUNDA_UPDATER_TOKEN','missing').encode();"
        "sys.stdout.buffer.write(payload+b'|'+sys.argv[1].encode()+b'|'+token)"
    )

    result = CommandRunner().run(
        (sys.executable, "-c", script, literal_argument),
        timeout=2,
        stdin=b"input",
    )

    assert result.returncode == 0
    assert result.stdout == b"input|literal;$(printf shell-expanded)|missing"
    assert result.stderr == b""


def test_runner_accepts_output_at_the_aggregate_boundary():
    stdout_size = MAX_OUTPUT_BYTES // 2
    stderr_size = MAX_OUTPUT_BYTES - stdout_size

    result = run_output_process(stdout_size, stderr_size)

    assert result.stdout == b"a" * stdout_size
    assert result.stderr == b"b" * stderr_size


def test_runner_rejects_stdout_over_the_per_stream_boundary():
    with pytest.raises(SafeCommandError, match="^command_output_too_large$"):
        run_output_process(MAX_OUTPUT_BYTES + 1, 0)


def test_runner_rejects_combined_output_over_the_aggregate_boundary():
    first_stream = MAX_OUTPUT_BYTES // 2

    with pytest.raises(SafeCommandError, match="^command_output_too_large$"):
        run_output_process(first_stream, MAX_OUTPUT_BYTES - first_stream + 1)


def test_runner_terminates_kills_and_reaps_process_on_output_overflow(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = (
        "import os,signal,sys,time;"
        "from pathlib import Path;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "Path(sys.argv[1]).write_text(str(os.getpid()));"
        "sys.stdout.buffer.write(b'x'*(int(sys.argv[2])+1));"
        "sys.stdout.buffer.flush();"
        "time.sleep(60)"
    )
    started_at = time.monotonic()

    with pytest.raises(SafeCommandError, match="^command_output_too_large$"):
        CommandRunner().run(
            (sys.executable, "-c", script, str(pid_file), str(MAX_OUTPUT_BYTES)),
            timeout=2,
        )

    assert time.monotonic() - started_at < 1.5
    child_pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.parametrize(
    "argv",
    [object(), (), ("",), ("docker", ""), ("docker\0hidden", "version")],
)
def test_runner_rejects_empty_or_nul_argv_without_spawning(argv, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", fail_unexpected_spawn)

    with pytest.raises(SafeCommandError, match="^invalid_command$"):
        CommandRunner().run(argv, timeout=1)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_runner_rejects_invalid_timeout_without_spawning(timeout, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", fail_unexpected_spawn)

    with pytest.raises(SafeCommandError, match="^invalid_command$"):
        CommandRunner().run(("docker", "version"), timeout=timeout)


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (OSError("private host path /var/run/docker.sock"), "command_unavailable"),
        (subprocess.SubprocessError("private process detail"), "command_failed"),
    ],
)
def test_runner_maps_process_start_failures_to_safe_codes(
    failure, expected_code, monkeypatch, caplog
):
    monkeypatch.setattr(subprocess, "Popen", Mock(side_effect=failure))

    with caplog.at_level(logging.WARNING), pytest.raises(
        SafeCommandError, match=f"^{expected_code}$"
    ) as raised:
        CommandRunner().run(("/usr/bin/true",), timeout=1)

    assert raised.value.code == expected_code
    assert "/var/run" not in str(raised.value)
    assert caplog.messages == [
        f"command error code={expected_code} executable=true"
    ]


def test_runner_maps_timeout_to_safe_code_and_reaps_process(caplog):
    script = "import time; time.sleep(60)"

    with caplog.at_level(logging.WARNING), pytest.raises(
        SafeCommandError, match="^command_timeout$"
    ) as raised:
        CommandRunner().run((sys.executable, "-c", script), timeout=0.05)

    assert raised.value.code == "command_timeout"
    assert caplog.messages == [
        f"command error code=command_timeout executable={os.path.basename(sys.executable)}"
    ]


def test_runner_maps_nonzero_exit_without_exposing_output(caplog):
    secret = "Authorization: Bearer private-token sha256:private"
    script = (
        "import sys;"
        "payload=sys.argv[1].encode();"
        "sys.stdout.buffer.write(payload);"
        "sys.stderr.buffer.write(payload);"
        "sys.exit(17)"
    )

    with caplog.at_level(logging.WARNING), pytest.raises(
        SafeCommandError, match="^command_failed$"
    ) as raised:
        CommandRunner().run((sys.executable, "-c", script, secret), timeout=2)

    executable = os.path.basename(sys.executable)
    assert raised.value.code == "command_failed"
    assert "private-token" not in str(raised.value)
    assert caplog.messages == [
        f"command error code=command_failed executable={executable}"
    ]


def run_output_process(stdout_size: int, stderr_size: int):
    script = (
        "import sys;"
        "sys.stdout.buffer.write(b'a'*int(sys.argv[1]));"
        "sys.stdout.buffer.flush();"
        "sys.stderr.buffer.write(b'b'*int(sys.argv[2]));"
        "sys.stderr.buffer.flush()"
    )
    return CommandRunner().run(
        (sys.executable, "-c", script, str(stdout_size), str(stderr_size)),
        timeout=2,
    )


def fail_unexpected_spawn(*_args, **_kwargs):
    pytest.fail("invalid command spawned a process")
