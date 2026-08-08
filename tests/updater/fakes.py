from collections.abc import Sequence
from dataclasses import dataclass

from updater.runner import CompletedCommand


@dataclass(frozen=True)
class RunnerCall:
    argv: tuple[str, ...]
    timeout: float
    stdin: bytes | None


class ScriptedRunner:
    def __init__(self, *responses: CompletedCommand | Exception | bytes):
        self.responses = list(responses)
        self.calls: list[RunnerCall] = []

    def run(
        self,
        argv: Sequence[str],
        timeout: float,
        stdin: bytes | None = None,
    ) -> CompletedCommand:
        self.calls.append(RunnerCall(tuple(argv), timeout, stdin))
        if not self.responses:
            raise AssertionError("unexpected runner call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, bytes):
            return CompletedCommand(0, response, b"")
        return response


class FakeHTTPResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self, amount: int) -> bytes:
        return self._body[:amount]


class FakeHTTPConnection:
    def __init__(self, response: FakeHTTPResponse):
        self.response = response
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def request(self, method: str, path: str, headers: dict[str, str]) -> None:
        self.requests.append((method, path, headers))

    def getresponse(self) -> FakeHTTPResponse:
        return self.response

    def close(self) -> None:
        self.closed = True
