import logging
import math
import os
import selectors
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

MAX_OUTPUT_BYTES = 64 * 1024
MAX_STREAM_OUTPUT_BYTES = MAX_OUTPUT_BYTES
MAX_TOTAL_OUTPUT_BYTES = MAX_OUTPUT_BYTES
OUTPUT_READ_CHUNK_BYTES = 8 * 1024
PROCESS_TERMINATE_GRACE_SECONDS = 0.1
COMMAND_ENVIRONMENT = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C",
}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompletedCommand:
    returncode: int
    stdout: bytes
    stderr: bytes


class SafeCommandError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _OutputLimitExceeded(RuntimeError):
    pass


class CommandRunner:
    def run(
        self,
        argv: Sequence[str],
        timeout: float,
        stdin: bytes | None = None,
    ) -> CompletedCommand:
        command = _validated_command(argv, timeout, stdin)
        executable = Path(command[0]).name
        try:
            process = _start_process(command, stdin)
        except OSError as error:
            raise self._safe_error("command_unavailable", executable) from error
        except subprocess.SubprocessError as error:
            raise self._safe_error("command_failed", executable) from error

        try:
            stdout, stderr = _collect_bounded_output(process, timeout)
        except _OutputLimitExceeded as error:
            raise self._safe_error("command_output_too_large", executable) from error
        except subprocess.TimeoutExpired as error:
            raise self._safe_error("command_timeout", executable) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise self._safe_error("command_failed", executable) from error

        if process.returncode != 0:
            raise self._safe_error("command_failed", executable)
        return CompletedCommand(
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    @staticmethod
    def _safe_error(code: str, executable: str) -> SafeCommandError:
        logger.warning("command error code=%s executable=%s", code, executable)
        return SafeCommandError(code)


def _start_process(
    command: tuple[str, ...], stdin: bytes | None
) -> subprocess.Popen[bytes]:
    if stdin is None:
        return _popen(command, subprocess.DEVNULL)
    with tempfile.TemporaryFile() as input_file:
        input_file.write(stdin)
        input_file.seek(0)
        return _popen(command, input_file)


def _popen(
    command: tuple[str, ...], stdin: int | BinaryIO
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=COMMAND_ENVIRONMENT,
        bufsize=0,
    )


def _collect_bounded_output(
    process: subprocess.Popen[bytes], timeout: float
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        _terminate_and_reap(process)
        raise subprocess.SubprocessError("missing_output_pipe")

    deadline = time.monotonic() + timeout
    streams = {
        "stdout": bytearray(),
        "stderr": bytearray(),
    }
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(process.args, timeout)
            for key, _event_mask in events:
                chunk = os.read(key.fd, OUTPUT_READ_CHUNK_BYTES)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                stream = streams[key.data]
                total_size = len(streams["stdout"]) + len(streams["stderr"])
                if (
                    len(stream) + len(chunk) > MAX_STREAM_OUTPUT_BYTES
                    or total_size + len(chunk) > MAX_TOTAL_OUTPUT_BYTES
                ):
                    raise _OutputLimitExceeded
                stream.extend(chunk)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        process.wait(timeout=remaining)
        return bytes(streams["stdout"]), bytes(streams["stderr"])
    except BaseException:
        _terminate_and_reap(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        pass
    process.wait()


def _validated_command(
    argv: Sequence[str], timeout: float, stdin: bytes | None
) -> tuple[str, ...]:
    if (
        isinstance(argv, (str, bytes))
        or not isinstance(argv, Sequence)
        or not argv
        or any(not isinstance(value, str) or not value or "\0" in value for value in argv)
        or isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or (stdin is not None and not isinstance(stdin, bytes))
    ):
        raise SafeCommandError("invalid_command")
    return tuple(argv)
