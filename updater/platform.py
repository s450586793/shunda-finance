import http.client
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from updater.runner import CommandRunner, CompletedCommand, SafeCommandError
from updater.types import (
    CleanupStep,
    CleanupStepStatus,
    ImageIdentity,
    UpdateTask,
    validate_version,
)

CONTAINER_FORMAT = (
    '{"Id":{{json .Id}},"Image":{{json .Image}},'
    '"ConfigImage":{{json .Config.Image}},'
    '"Project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"Service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"OneOff":{{json (index .Config.Labels "com.docker.compose.oneoff")}}}'
)
IMAGE_FORMAT = (
    '{"Id":{{json .Id}},"RepoTags":{{json .RepoTags}},'
    '"RepoDigests":{{json .RepoDigests}},'
    '"Version":{{json (index .Config.Labels "org.opencontainers.image.version")}},'
    '"Revision":{{json (index .Config.Labels "org.opencontainers.image.revision")}},'
    '"Created":{{json (index .Config.Labels "org.opencontainers.image.created")}},'
    '"Os":{{json .Os}},"Architecture":{{json .Architecture}}}'
)

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID_PATTERN = re.compile(r"^[0-9a-f]{12,64}$")
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
_ENVIRONMENT_LINE_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_BACKUP_PATTERN = re.compile(r"^/data/backups/db-([0-9]{8}-[0-9]{6})\.dump$")
_UPLOADS_PATTERN = re.compile(
    r"^/data/backups/uploads-([0-9]{8}-[0-9]{6})\.tar\.gz$"
)
_MAX_ENVIRONMENT_BYTES = 1024 * 1024
_MAX_HEALTH_BYTES = 64 * 1024
_PROJECT_NAME = "shunda-finance"
_COMPOSE_FILE = Path("/config/compose.yml")
_ENV_FILE = Path("/config/.env")
_WEB_REPOSITORY = "ghcr.io/s450586793/shunda-finance-web"
_WEB_HEALTH_URL = "http://web:8000/health/"
_TASK_ROOT = Path("/state/tasks")


class _Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        timeout: float,
        stdin: bytes | None = None,
    ) -> CompletedCommand: ...


class _HTTPResponse(Protocol):
    status: int

    def read(self, amount: int) -> bytes: ...


class _HTTPConnection(Protocol):
    def request(self, method: str, path: str, headers: dict[str, str]) -> None: ...

    def getresponse(self) -> _HTTPResponse: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[str, int, float], _HTTPConnection]


@dataclass(frozen=True)
class PlatformConfig:
    project_name: str = _PROJECT_NAME
    compose_file: Path = _COMPOSE_FILE
    env_file: Path = _ENV_FILE
    web_repository: str = _WEB_REPOSITORY
    web_health_url: str = _WEB_HEALTH_URL
    task_root: Path = _TASK_ROOT


@dataclass(frozen=True)
class _InspectedImage:
    identity: ImageIdentity
    revision: str
    created: str
    os_name: str
    architecture: str
    all_tags: tuple[str, ...]


class SafeOperationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class DockerPlatform:
    def __init__(
        self,
        runner: _Runner | None = None,
        config: PlatformConfig | None = None,
        *,
        connection_factory: ConnectionFactory | None = None,
    ):
        if config is not None and not _is_production_config(config):
            raise SafeOperationError("invalid_platform_config")
        self._runner = runner or CommandRunner()
        self._connection_factory = connection_factory or _open_connection

    def inspect_web(self) -> ImageIdentity:
        try:
            return self._inspect_web_image().identity
        except (SafeCommandError, TypeError, ValueError, UnicodeError) as error:
            raise SafeOperationError("inspect_failed") from error

    def resolve_stable(self) -> ImageIdentity:
        stable = f"{_WEB_REPOSITORY}:stable"
        try:
            self._runner.run(("docker", "pull", stable), timeout=300)
            locked_stable = self._inspect_image(stable)
            if stable not in locked_stable.all_tags:
                raise ValueError("stable_tag_missing")
            immutable = f"{_WEB_REPOSITORY}:{locked_stable.identity.version}"
            self._runner.run(("docker", "pull", immutable), timeout=300)
            inspected_immutable = self._inspect_image(immutable)
            if (
                immutable not in inspected_immutable.all_tags
                or not _same_locked_image(locked_stable, inspected_immutable)
            ):
                raise ValueError("immutable_identity_mismatch")
            return inspected_immutable.identity
        except (SafeCommandError, TypeError, ValueError, UnicodeError) as error:
            raise SafeOperationError("pull_failed") from error

    def verify_target(self, target: ImageIdentity) -> None:
        if not isinstance(target, ImageIdentity) or not self._is_recorded_target(target):
            raise SafeOperationError("target_identity_mismatch")
        reference = f"{_WEB_REPOSITORY}@{target.digest}"
        try:
            self._runner.run(("docker", "pull", reference), timeout=300)
        except SafeCommandError as error:
            raise SafeOperationError("pull_failed") from error
        try:
            inspected_target = self._inspect_image(reference)
            inspected_current = self._inspect_web_image()
            if not _same_identity(inspected_target.identity, target) or (
                inspected_target.os_name,
                inspected_target.architecture,
            ) != (inspected_current.os_name, inspected_current.architecture):
                raise ValueError("target_identity_mismatch")
        except (SafeCommandError, TypeError, ValueError, UnicodeError) as error:
            raise SafeOperationError("target_identity_mismatch") from error

    def create_backup(self) -> tuple[str, str]:
        try:
            result = self._runner.run(
                (*self._compose_prefix(), "exec", "-T", "web", "/app/scripts/backup.sh"),
                timeout=900,
            )
            database_backup, uploads_backup = _parse_backup_manifest(result.stdout)
            for path in (database_backup, uploads_backup):
                self._runner.run(
                    (*self._compose_prefix(), "exec", "-T", "web", "test", "-s", path),
                    timeout=30,
                )
            return database_backup, uploads_backup
        except (SafeCommandError, TypeError, ValueError, UnicodeError) as error:
            raise SafeOperationError("backup_failed") from error

    def tag_rollback(self, task: UpdateTask) -> None:
        if not isinstance(task, UpdateTask):
            raise SafeOperationError("rollback_identity_mismatch")
        alias = self._rollback_alias(task.id)
        if task.original.rollback_alias not in ("", alias) or not self._is_original(
            task.original, alias_optional=True
        ):
            raise SafeOperationError("rollback_identity_mismatch")
        try:
            actual = self._inspect_web_image().identity
            if not _same_identity(actual, task.original):
                raise ValueError("runtime_identity_mismatch")
            self._runner.run(
                ("docker", "image", "tag", task.original.image_id, alias), timeout=60
            )
            tagged = self._inspect_image(alias)
            if alias not in tagged.all_tags or not _same_identity(
                tagged.identity, task.original
            ):
                raise ValueError("rollback_identity_mismatch")
            task.original = replace(task.original, rollback_alias=alias)
        except (SafeCommandError, TypeError, ValueError, UnicodeError) as error:
            raise SafeOperationError("rollback_identity_mismatch") from error

    def stop_web(self) -> None:
        try:
            self._runner.run((*self._compose_prefix(), "stop", "web"), timeout=120)
        except SafeCommandError as error:
            raise SafeOperationError("stop_failed") from error

    def migrate_target(
        self, target: ImageIdentity, *, task_id: UUID | None = None
    ) -> None:
        try:
            override = self._write_target_override(target, task_id)
            self._runner.run(
                (
                    *self._compose_prefix(),
                    "-f",
                    str(override),
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    "web",
                    "python",
                    "manage.py",
                    "migrate",
                ),
                timeout=900,
            )
        except (OSError, SafeCommandError, TypeError, ValueError) as error:
            raise SafeOperationError("migration_failed") from error

    def start_target(
        self, target: ImageIdentity, *, task_id: UUID | None = None
    ) -> None:
        try:
            override = self._write_target_override(target, task_id)
            self._runner.run(
                (*self._compose_prefix(), "-f", str(override), "up", "-d", "--no-deps", "web"),
                timeout=180,
            )
        except (OSError, SafeCommandError, TypeError, ValueError) as error:
            raise SafeOperationError("start_failed") from error

    def start_rollback(self, task: UpdateTask) -> None:
        if not isinstance(task, UpdateTask):
            raise SafeOperationError("rollback_identity_mismatch")
        alias = self._rollback_alias(task.id)
        if task.original.rollback_alias != alias or not self._is_original(task.original):
            raise SafeOperationError("rollback_identity_mismatch")
        try:
            inspected = self._inspect_image(alias)
            if alias not in inspected.all_tags or not _same_identity(
                inspected.identity, task.original
            ):
                raise ValueError("rollback_identity_mismatch")
            override = self._write_override(task.id, "rollback-compose.yml", alias)
            self._runner.run(
                (*self._compose_prefix(), "-f", str(override), "up", "-d", "--no-deps", "web"),
                timeout=180,
            )
        except (OSError, SafeCommandError, TypeError, ValueError) as error:
            raise SafeOperationError("rollback_failed") from error

    def health(self, expected: ImageIdentity | None = None) -> None:
        connection: _HTTPConnection | None = None
        try:
            if expected is not None and not isinstance(expected, ImageIdentity):
                raise ValueError("invalid_expected_identity")
            split = urlsplit(_WEB_HEALTH_URL)
            if (
                split.scheme != "http"
                or split.hostname != "web"
                or split.port != 8000
                or split.path != "/health/"
                or split.username is not None
                or split.password is not None
                or split.query
                or split.fragment
            ):
                raise ValueError("invalid_health_url")
            connection = self._connection_factory("web", 8000, 5.0)
            connection.request("GET", "/health/", headers={"Accept": "application/json"})
            response = connection.getresponse()
            body = response.read(_MAX_HEALTH_BYTES + 1)
            if type(response.status) is not int or response.status != 200:
                raise ValueError("unhealthy_status")
            if len(body) > _MAX_HEALTH_BYTES:
                raise ValueError("health_response_too_large")
            payload = json.loads(body.decode("utf-8", errors="strict"))
            if type(payload) is not dict or payload != {"status": "ok"}:
                raise ValueError("unhealthy_payload")
            if expected is not None and not _same_identity(self.inspect_web(), expected):
                raise ValueError("health_identity_mismatch")
        except (
            SafeOperationError,
            http.client.HTTPException,
            OSError,
            TypeError,
            ValueError,
            UnicodeError,
            json.JSONDecodeError,
        ) as error:
            raise SafeOperationError("health_check_failed") from error
        finally:
            if connection is not None:
                connection.close()

    def persist_version(self, version: str) -> None:
        try:
            validated_version = validate_version(version)
            content, values = self._read_environment()
            if "SHUNDA_WEB_IMAGE_TAG" not in values:
                raise ValueError("missing_web_version")
            validate_version(values["SHUNDA_WEB_IMAGE_TAG"])
            replacement_count = 0
            updated_lines: list[str] = []
            for line in content.splitlines(keepends=True):
                ending = "\n" if line.endswith("\n") else ""
                raw_line = line[:-1] if ending else line
                if raw_line.endswith("\r"):
                    raw_line = raw_line[:-1]
                    ending = "\r\n" if ending else "\r"
                match = _ENVIRONMENT_LINE_PATTERN.fullmatch(raw_line)
                if match is not None and match.group(1) == "SHUNDA_WEB_IMAGE_TAG":
                    updated_lines.append(
                        f"SHUNDA_WEB_IMAGE_TAG={validated_version}{ending}"
                    )
                    replacement_count += 1
                else:
                    updated_lines.append(line)
            if replacement_count != 1:
                raise ValueError("invalid_web_version_count")
            _replace_environment_file("".join(updated_lines).encode("utf-8"))
        except (OSError, TypeError, ValueError, UnicodeError) as error:
            raise SafeOperationError("persist_failed") from error

    def cleanup_original_step(
        self,
        task: UpdateTask,
        step: CleanupStep,
    ) -> None:
        try:
            if not isinstance(step, CleanupStep):
                raise TypeError("invalid_cleanup_step")
            self._validate_cleanup_task(task)
            _content, environment = self._read_environment()
            if environment.get("SHUNDA_WEB_IMAGE_TAG") != task.target.version:
                raise ValueError("environment_target_mismatch")
            if (
                task.cleanup_journal is None
                or getattr(task.cleanup_journal, step.value)
                is not CleanupStepStatus.STARTED
            ):
                raise ValueError("cleanup_step_not_started")

            if step is CleanupStep.IMAGE_ID:
                self._cleanup_image_id(task)
                return

            inspected = self._inspect_image(task.original.image_id)
            if not _same_image_core(inspected.identity, task.original):
                raise ValueError("original_identity_mismatch")
            allowed_tags = {*task.original.tags, task.original.rollback_alias}
            if not set(inspected.all_tags).issubset(allowed_tags):
                raise ValueError("unexpected_image_tags")
            self._require_no_container_references(task.original.image_id)

            version_tag = task.original.tags[0]
            if step is CleanupStep.VERSION_TAG:
                if task.original.rollback_alias not in inspected.all_tags:
                    raise ValueError("rollback_alias_missing")
                if (
                    self._reference_image_id(task.original.rollback_alias)
                    != task.original.image_id
                ):
                    raise ValueError("alias_identity_mismatch")
                self._remove_cleanup_reference(
                    version_tag,
                    task.original.image_id,
                    inspected.all_tags,
                )
                return

            if version_tag in inspected.all_tags:
                raise ValueError("version_tag_still_present")
            if self._reference_image_id(version_tag) is not None:
                raise ValueError("version_tag_reference_present")
            self._remove_cleanup_reference(
                task.original.rollback_alias,
                task.original.image_id,
                inspected.all_tags,
            )
        except SafeCommandError as error:
            raise SafeOperationError("cleanup_failed") from error
        except (OSError, TypeError, ValueError, UnicodeError) as error:
            raise SafeOperationError("cleanup_refused") from error

    def _remove_cleanup_reference(
        self,
        reference: str,
        expected_image_id: str,
        inspected_tags: tuple[str, ...],
    ) -> None:
        referenced_id = self._reference_image_id(reference)
        if referenced_id is None:
            if reference in inspected_tags:
                raise ValueError("cleanup_reference_missing")
            return
        if referenced_id != expected_image_id:
            raise ValueError("cleanup_reference_mismatch")
        self._runner.run(("docker", "image", "rm", reference), timeout=60)
        if self._reference_image_id(reference) is not None:
            raise ValueError("cleanup_reference_remains")

    def _cleanup_image_id(self, task: UpdateTask) -> None:
        image_id = self._reference_image_id(task.original.image_id)
        if image_id is None:
            self._require_no_container_references(task.original.image_id)
            self._require_cleanup_references_absent(task)
            return
        if image_id != task.original.image_id:
            raise ValueError("cleanup_image_mismatch")
        inspected = self._inspect_image(task.original.image_id)
        if (
            not _same_image_core(inspected.identity, task.original)
            or inspected.all_tags
        ):
            raise ValueError("final_identity_mismatch")
        self._require_no_container_references(task.original.image_id)
        self._require_cleanup_references_absent(task)
        self._runner.run(
            ("docker", "image", "rm", task.original.image_id), timeout=60
        )
        if self._reference_image_id(task.original.image_id) is not None:
            raise ValueError("cleanup_image_remains")

    def _require_cleanup_references_absent(self, task: UpdateTask) -> None:
        for reference in (*task.original.tags, task.original.rollback_alias):
            if self._reference_image_id(reference) is not None:
                raise ValueError("cleanup_reference_present")

    def _compose_prefix(self) -> tuple[str, ...]:
        return (
            "docker",
            "compose",
            "--project-name",
            _PROJECT_NAME,
            "--env-file",
            str(_ENV_FILE),
            "-f",
            str(_COMPOSE_FILE),
        )

    def _inspect_web_image(self) -> _InspectedImage:
        result = self._runner.run(
            (*self._compose_prefix(), "ps", "--all", "-q", "web"), timeout=30
        )
        lines = result.stdout.decode("ascii", errors="strict").splitlines()
        if len(lines) != 1 or _CONTAINER_ID_PATTERN.fullmatch(lines[0]) is None:
            raise ValueError("invalid_container_id")
        container_id = lines[0]
        inspected = self._runner.run(
            (
                "docker",
                "container",
                "inspect",
                "--format",
                CONTAINER_FORMAT,
                container_id,
            ),
            timeout=30,
        )
        container = _exact_json(
            inspected.stdout,
            {"Id", "Image", "ConfigImage", "Project", "Service", "OneOff"},
        )
        if (
            not all(isinstance(container[key], str) for key in container)
            or container["Id"] != container_id
            or _DIGEST_PATTERN.fullmatch(container["Image"]) is None
            or container["Project"] != _PROJECT_NAME
            or container["Service"] != "web"
            or container["OneOff"] != "False"
            or not self._is_web_image_reference(container["ConfigImage"])
        ):
            raise ValueError("invalid_container_identity")
        image = self._inspect_image(container["Image"])
        if image.identity.image_id != container["Image"]:
            raise ValueError("container_image_mismatch")
        return image

    def _inspect_image(self, reference: str) -> _InspectedImage:
        result = self._runner.run(
            ("docker", "image", "inspect", "--format", IMAGE_FORMAT, reference),
            timeout=30,
        )
        payload = _exact_json(
            result.stdout,
            {
                "Id",
                "RepoTags",
                "RepoDigests",
                "Version",
                "Revision",
                "Created",
                "Os",
                "Architecture",
            },
        )
        string_fields = ("Id", "Version", "Revision", "Created", "Os", "Architecture")
        if (
            not all(isinstance(payload[field], str) for field in string_fields)
            or _DIGEST_PATTERN.fullmatch(payload["Id"]) is None
            or not isinstance(payload["RepoTags"], list)
            or not all(isinstance(tag, str) for tag in payload["RepoTags"])
            or len(set(payload["RepoTags"])) != len(payload["RepoTags"])
            or not isinstance(payload["RepoDigests"], list)
            or not all(isinstance(digest, str) for digest in payload["RepoDigests"])
            or len(set(payload["RepoDigests"])) != len(payload["RepoDigests"])
            or _REVISION_PATTERN.fullmatch(payload["Revision"]) is None
            or payload["Os"] != "linux"
            or not payload["Architecture"]
        ):
            raise ValueError("invalid_image_identity")
        version = validate_version(payload["Version"])
        digest_prefix = f"{_WEB_REPOSITORY}@"
        repository_digests = [
            value[len(digest_prefix) :]
            for value in payload["RepoDigests"]
            if value.startswith(digest_prefix)
        ]
        if (
            len(payload["RepoDigests"]) != 1
            or len(repository_digests) != 1
            or _DIGEST_PATTERN.fullmatch(repository_digests[0]) is None
        ):
            raise ValueError("ambiguous_repository_digest")
        published_at = _parse_utc(payload["Created"])
        version_tag = f"{_WEB_REPOSITORY}:{version}"
        tags = (version_tag,) if version_tag in payload["RepoTags"] else ()
        return _InspectedImage(
            identity=ImageIdentity(
                repository=_WEB_REPOSITORY,
                version=version,
                digest=repository_digests[0],
                image_id=payload["Id"],
                tags=tags,
                published_at=published_at,
            ),
            revision=payload["Revision"],
            created=payload["Created"],
            os_name=payload["Os"],
            architecture=payload["Architecture"],
            all_tags=tuple(payload["RepoTags"]),
        )

    def _write_target_override(
        self, target: ImageIdentity, task_id: UUID | None
    ) -> Path:
        if not self._is_recorded_target(target):
            raise ValueError("target_identity_mismatch")
        return self._write_override(
            task_id or uuid4(),
            "target-compose.yml",
            f"{_WEB_REPOSITORY}@{target.digest}",
        )

    def _write_override(self, task_id: UUID, filename: str, image: str) -> Path:
        if not isinstance(task_id, UUID) or str(UUID(str(task_id))) != str(task_id):
            raise ValueError("invalid_task_id")
        payload = (
            "services:\n"
            "  web:\n"
            f"    image: {image}\n"
            "    pull_policy: never\n"
        ).encode("ascii")
        return _write_task_override(task_id, filename, payload)

    def _rollback_alias(self, task_id: UUID) -> str:
        if not isinstance(task_id, UUID) or str(UUID(str(task_id))) != str(task_id):
            raise SafeOperationError("rollback_identity_mismatch")
        return f"shunda-finance-rollback-web:{task_id}"

    def _is_recorded_target(self, target: ImageIdentity) -> bool:
        if not isinstance(target, ImageIdentity):
            return False
        try:
            validate_version(target.version)
        except (TypeError, ValueError):
            return False
        return (
            target.repository == _WEB_REPOSITORY
            and _DIGEST_PATTERN.fullmatch(target.digest) is not None
            and _DIGEST_PATTERN.fullmatch(target.image_id) is not None
            and target.tags == (f"{_WEB_REPOSITORY}:{target.version}",)
            and target.rollback_alias == ""
        )

    def _is_original(self, original: ImageIdentity, *, alias_optional: bool = False) -> bool:
        if not isinstance(original, ImageIdentity):
            return False
        try:
            validate_version(original.version)
        except (TypeError, ValueError):
            return False
        expected_alias = re.fullmatch(
            r"shunda-finance-rollback-web:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            original.rollback_alias,
        )
        return (
            original.repository == _WEB_REPOSITORY
            and _DIGEST_PATTERN.fullmatch(original.digest) is not None
            and _DIGEST_PATTERN.fullmatch(original.image_id) is not None
            and original.tags == (f"{_WEB_REPOSITORY}:{original.version}",)
            and (
                expected_alias is not None
                or (alias_optional and original.rollback_alias == "")
            )
        )

    def _is_web_image_reference(self, reference: str) -> bool:
        repository = re.escape(_WEB_REPOSITORY)
        if re.fullmatch(repository + r"@sha256:[0-9a-f]{64}", reference):
            return True
        version_match = re.fullmatch(repository + r":(v[0-9]+\.[0-9]+\.[0-9]+)", reference)
        if version_match is not None:
            try:
                validate_version(version_match.group(1))
                return True
            except ValueError:
                return False
        return (
            re.fullmatch(
                r"shunda-finance-rollback-web:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                reference,
            )
            is not None
        )

    def _read_environment(self) -> tuple[str, dict[str, str]]:
        return _read_environment_file()

    def _validate_cleanup_task(self, task: UpdateTask) -> None:
        if not isinstance(task, UpdateTask):
            raise TypeError("invalid_cleanup_task")
        alias = self._rollback_alias(task.id)
        if (
            not self._is_original(task.original)
            or task.original.rollback_alias != alias
            or not self._is_recorded_target(task.target)
            or task.original.repository != task.target.repository
            or task.original.version == task.target.version
            or task.original.digest == task.target.digest
            or task.original.image_id == task.target.image_id
        ):
            raise ValueError("unsafe_cleanup_identity")

    def _require_no_container_references(self, image_id: str) -> None:
        result = self._runner.run(
            (
                "docker",
                "container",
                "ls",
                "--all",
                "--quiet",
                "--filter",
                f"ancestor={image_id}",
            ),
            timeout=30,
        )
        if result.stdout.strip():
            raise ValueError("image_has_container_reference")

    def _reference_image_id(self, reference: str) -> str | None:
        result = self._runner.run(
            ("docker", "image", "ls", "--no-trunc", "--quiet", reference),
            timeout=30,
        )
        lines = result.stdout.decode("ascii", errors="strict").splitlines()
        if not lines:
            return None
        if len(lines) != 1 or _DIGEST_PATTERN.fullmatch(lines[0]) is None:
            raise ValueError("ambiguous_image_reference")
        return lines[0]

    def _inspect_reference_image_id(self, reference: str) -> str:
        result = self._runner.run(
            ("docker", "image", "inspect", "--format", "{{json .Id}}", reference),
            timeout=30,
        )
        image_id = json.loads(result.stdout.decode("utf-8", errors="strict"))
        if not isinstance(image_id, str) or _DIGEST_PATTERN.fullmatch(image_id) is None:
            raise ValueError("invalid_inspected_image_id")
        return image_id


def _read_environment_file() -> tuple[str, dict[str, str]]:
    return _read_environment_at(_ENV_FILE)


def _read_environment_at(path: Path) -> tuple[str, dict[str, str]]:
    file_stat = path.lstat()
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or stat.S_IMODE(file_stat.st_mode) != 0o600
        or file_stat.st_size > _MAX_ENVIRONMENT_BYTES
    ):
        raise ValueError("unsafe_environment_file")
    content = path.read_bytes().decode("utf-8", errors="strict")
    values: dict[str, str] = {}
    for line in content.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _ENVIRONMENT_LINE_PATTERN.fullmatch(line)
        if match is None or match.group(1) in values:
            raise ValueError("invalid_environment")
        values[match.group(1)] = match.group(2)
    return content, values


def _replace_environment_file(payload: bytes) -> None:
    _atomic_private_replace(_ENV_FILE, payload)


def _write_task_override(task_id: UUID, filename: str, payload: bytes) -> Path:
    return _write_task_override_at(_TASK_ROOT, task_id, filename, payload)


def _write_task_override_at(
    task_root: Path, task_id: UUID, filename: str, payload: bytes
) -> Path:
    task_directory = task_root / str(task_id)
    if task_directory.exists() and task_directory.is_symlink():
        raise ValueError("unsafe_task_directory")
    task_directory.mkdir(parents=True, exist_ok=True)
    os.chmod(task_directory, 0o700)
    path = task_directory / filename
    _atomic_private_replace(path, payload)
    return path


def _open_connection(host: str, port: int, timeout: float) -> _HTTPConnection:
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _is_production_config(config: object) -> bool:
    return type(config) is PlatformConfig and config == PlatformConfig()


def _exact_json(payload: bytes, expected_keys: set[str]) -> dict[str, object]:
    decoded = json.loads(payload.decode("utf-8", errors="strict"))
    if type(decoded) is not dict or set(decoded) != expected_keys:
        raise ValueError("invalid_json")
    return decoded


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("invalid_created_timestamp")
    return parsed


def _same_identity(actual: ImageIdentity, expected: ImageIdentity) -> bool:
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


def _same_image_core(actual: ImageIdentity, expected: ImageIdentity) -> bool:
    return (
        actual.repository,
        actual.version,
        actual.digest,
        actual.image_id,
        actual.published_at,
    ) == (
        expected.repository,
        expected.version,
        expected.digest,
        expected.image_id,
        expected.published_at,
    )


def _same_locked_image(actual: _InspectedImage, expected: _InspectedImage) -> bool:
    return (
        _same_image_core(actual.identity, expected.identity)
        and actual.revision == expected.revision
        and actual.created == expected.created
        and actual.os_name == expected.os_name
        and actual.architecture == expected.architecture
    )


def _parse_backup_manifest(payload: bytes) -> tuple[str, str]:
    lines = payload.decode("utf-8", errors="strict").splitlines()
    if len(lines) != 2:
        raise ValueError("invalid_backup_manifest")
    values: dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            raise ValueError("invalid_backup_manifest")
        key, value = line.split("=", 1)
        if key in values:
            raise ValueError("duplicate_backup_manifest_key")
        values[key] = value
    if set(values) != {"DB_BACKUP", "UPLOADS_BACKUP"}:
        raise ValueError("invalid_backup_manifest_keys")
    database_match = _BACKUP_PATTERN.fullmatch(values["DB_BACKUP"])
    uploads_match = _UPLOADS_PATTERN.fullmatch(values["UPLOADS_BACKUP"])
    if (
        database_match is None
        or uploads_match is None
        or database_match.group(1) != uploads_match.group(1)
    ):
        raise ValueError("invalid_backup_paths")
    return values["DB_BACKUP"], values["UPLOADS_BACKUP"]


def _atomic_private_replace(path: Path, payload: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary_file:
            temporary_path = Path(temporary_file.name)
            os.chmod(temporary_path, 0o600)
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
