import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Final
from uuid import UUID

VERSION_PATTERN = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PUBLIC_ERROR_MESSAGES: Final = {
    "": "",
    "backup_failed": "备份失败，请联系管理员。",
    "pull_failed": "下载升级版本失败，请联系管理员。",
    "migration_failed": "升级失败，请联系管理员。",
    "health_check_failed": "升级后检查失败，请联系管理员。",
    "rollback_failed": "升级失败，需要人工处理。",
    "update_failed": "升级失败，请联系管理员。",
}


class Stage(StrEnum):
    CHECKING = "checking"
    BACKING_UP = "backing_up"
    PULLING = "pulling"
    STOPPING_WEB = "stopping_web"
    MIGRATING = "migrating"
    STARTING_WEB = "starting_web"
    CHECKING_HEALTH = "checking_health"
    STABILIZING = "stabilizing"
    PERSISTING_VERSION = "persisting_version"
    CLEANING = "cleaning"
    ROLLING_BACK = "rolling_back"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MANUAL_INTERVENTION = "manual_intervention"


class CleanupStatus(StrEnum):
    NOT_RUN = "not_run"
    COMPLETE = "complete"
    PENDING = "pending"


class CleanupStep(StrEnum):
    VERSION_TAG = "version_tag"
    ROLLBACK_ALIAS = "rollback_alias"
    IMAGE_ID = "image_id"


class CleanupStepStatus(StrEnum):
    NOT_STARTED = "not_started"
    STARTED = "started"
    COMPLETED = "completed"


@dataclass(frozen=True)
class CleanupJournal:
    version_tag: CleanupStepStatus = CleanupStepStatus.NOT_STARTED
    rollback_alias: CleanupStepStatus = CleanupStepStatus.NOT_STARTED
    image_id: CleanupStepStatus = CleanupStepStatus.NOT_STARTED

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"version_tag", "rollback_alias", "image_id"}
    )

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, CleanupStepStatus)
            for value in (self.version_tag, self.rollback_alias, self.image_id)
        ):
            raise ValueError("invalid_state")
        if (
            self.rollback_alias is not CleanupStepStatus.NOT_STARTED
            and self.version_tag is not CleanupStepStatus.COMPLETED
        ):
            raise ValueError("invalid_state")
        if (
            self.image_id is not CleanupStepStatus.NOT_STARTED
            and self.rollback_alias is not CleanupStepStatus.COMPLETED
        ):
            raise ValueError("invalid_state")

    @property
    def complete(self) -> bool:
        return self.image_id is CleanupStepStatus.COMPLETED

    def to_dict(self) -> dict[str, str]:
        return {
            "version_tag": self.version_tag.value,
            "rollback_alias": self.rollback_alias.value,
            "image_id": self.image_id.value,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "CleanupJournal":
        data = _require_exact_keys(payload, cls._FIELDS)
        if not all(isinstance(data[field], str) for field in cls._FIELDS):
            raise ValueError("invalid_state")
        try:
            return cls(
                version_tag=CleanupStepStatus(data["version_tag"]),
                rollback_alias=CleanupStepStatus(data["rollback_alias"]),
                image_id=CleanupStepStatus(data["image_id"]),
            )
        except ValueError as error:
            raise ValueError("invalid_state") from error


def validate_version(value: str) -> str:
    if not isinstance(value, str) or VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid_version")
    return value


def version_key(value: str) -> tuple[int, int, int]:
    version = validate_version(value)
    match = VERSION_PATTERN.fullmatch(version)
    assert match is not None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _public_error(error_code: str) -> tuple[str, str]:
    message = PUBLIC_ERROR_MESSAGES.get(error_code)
    if message is None:
        return "update_failed", PUBLIC_ERROR_MESSAGES["update_failed"]
    return error_code, message


def _validate_utc(timestamp: datetime | None) -> None:
    if timestamp is None:
        return
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        raise ValueError("timestamp_must_be_utc")
    if timestamp.utcoffset() != UTC.utcoffset(timestamp):
        raise ValueError("timestamp_must_be_utc")


def _datetime_to_dict(timestamp: datetime | None) -> str | None:
    _validate_utc(timestamp)
    return timestamp.isoformat() if timestamp is not None else None


def _datetime_from_dict(value: Any, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise TypeError("invalid_timestamp")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("invalid_timestamp") from error
    _validate_utc(timestamp)
    return timestamp


def _require_exact_keys(payload: Any, expected: frozenset[str]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("invalid_state")
    return payload


@dataclass(frozen=True)
class ImageIdentity:
    repository: str
    version: str
    digest: str
    image_id: str
    tags: tuple[str, ...] = ()
    rollback_alias: str = ""
    published_at: datetime | None = None

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "repository",
            "version",
            "digest",
            "image_id",
            "tags",
            "rollback_alias",
            "published_at",
        }
    )

    def __post_init__(self) -> None:
        validate_version(self.version)
        _validate_utc(self.published_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "version": self.version,
            "digest": self.digest,
            "image_id": self.image_id,
            "tags": list(self.tags),
            "rollback_alias": self.rollback_alias,
            "published_at": _datetime_to_dict(self.published_at),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "ImageIdentity":
        data = _require_exact_keys(payload, cls._FIELDS)
        if (
            not all(
                isinstance(data[field], str)
                for field in (
                    "repository",
                    "version",
                    "digest",
                    "image_id",
                    "rollback_alias",
                )
            )
            or not isinstance(data["tags"], list)
            or not all(isinstance(tag, str) for tag in data["tags"])
        ):
            raise ValueError("invalid_state")
        return cls(
            repository=data["repository"],
            version=data["version"],
            digest=data["digest"],
            image_id=data["image_id"],
            tags=tuple(data["tags"]),
            rollback_alias=data["rollback_alias"],
            published_at=_datetime_from_dict(data["published_at"], nullable=True),
        )


@dataclass(frozen=True)
class CheckResult:
    current: ImageIdentity
    target: ImageIdentity
    available: bool
    checked_at: datetime

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"current", "target", "available", "checked_at"}
    )

    def __post_init__(self) -> None:
        _validate_utc(self.checked_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current.to_dict(),
            "target": self.target.to_dict(),
            "available": self.available,
            "checked_at": _datetime_to_dict(self.checked_at),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "CheckResult":
        data = _require_exact_keys(payload, cls._FIELDS)
        if type(data["available"]) is not bool:
            raise ValueError("invalid_state")
        checked_at = _datetime_from_dict(data["checked_at"])
        assert checked_at is not None
        return cls(
            current=ImageIdentity.from_dict(data["current"]),
            target=ImageIdentity.from_dict(data["target"]),
            available=data["available"],
            checked_at=checked_at,
        )


@dataclass
class UpdateTask:
    id: UUID
    original: ImageIdentity
    target: ImageIdentity
    stage: Stage
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    database_backup: str = ""
    uploads_backup: str = ""
    rolled_back: bool = False
    cleanup: CleanupStatus = CleanupStatus.NOT_RUN
    cleanup_journal: CleanupJournal | None = field(default_factory=CleanupJournal)
    error_code: str = ""
    error_message: str = ""

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "id",
            "original",
            "target",
            "stage",
            "created_at",
            "started_at",
            "finished_at",
            "database_backup",
            "uploads_backup",
            "rolled_back",
            "cleanup",
            "cleanup_journal",
            "error_code",
            "error_message",
        }
    )

    def __post_init__(self) -> None:
        _validate_utc(self.created_at)
        _validate_utc(self.started_at)
        _validate_utc(self.finished_at)

    def public_view(self) -> "TaskView":
        error_code, error_message = _public_error(self.error_code)
        return TaskView(
            id=self.id,
            from_version=self.original.version,
            to_version=self.target.version,
            stage=self.stage,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            backup_complete=bool(self.database_backup and self.uploads_backup),
            rolled_back=self.rolled_back,
            cleanup=self.cleanup,
            error_code=error_code,
            error_message=error_message,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "original": self.original.to_dict(),
            "target": self.target.to_dict(),
            "stage": self.stage.value,
            "created_at": _datetime_to_dict(self.created_at),
            "started_at": _datetime_to_dict(self.started_at),
            "finished_at": _datetime_to_dict(self.finished_at),
            "database_backup": self.database_backup,
            "uploads_backup": self.uploads_backup,
            "rolled_back": self.rolled_back,
            "cleanup": self.cleanup.value,
            "cleanup_journal": (
                self.cleanup_journal.to_dict()
                if self.cleanup_journal is not None
                else None
            ),
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "UpdateTask":
        legacy_fields = cls._FIELDS - {"cleanup_journal"}
        if not isinstance(payload, dict) or set(payload) not in {
            cls._FIELDS,
            legacy_fields,
        }:
            raise ValueError("invalid_state")
        data = payload
        if (
            not isinstance(data["id"], str)
            or not all(
                isinstance(data[field], str)
                for field in (
                    "stage",
                    "database_backup",
                    "uploads_backup",
                    "cleanup",
                    "error_code",
                    "error_message",
                )
            )
            or type(data["rolled_back"]) is not bool
        ):
            raise ValueError("invalid_state")
        try:
            task_id = UUID(data["id"])
            stage = Stage(data["stage"])
            cleanup = CleanupStatus(data["cleanup"])
        except ValueError as error:
            raise ValueError("invalid_state") from error
        created_at = _datetime_from_dict(data["created_at"])
        assert created_at is not None
        return cls(
            id=task_id,
            original=ImageIdentity.from_dict(data["original"]),
            target=ImageIdentity.from_dict(data["target"]),
            stage=stage,
            created_at=created_at,
            started_at=_datetime_from_dict(data["started_at"], nullable=True),
            finished_at=_datetime_from_dict(data["finished_at"], nullable=True),
            database_backup=data["database_backup"],
            uploads_backup=data["uploads_backup"],
            rolled_back=data["rolled_back"],
            cleanup=cleanup,
            cleanup_journal=(
                CleanupJournal.from_dict(data["cleanup_journal"])
                if data.get("cleanup_journal") is not None
                else None
            ),
            error_code=data["error_code"],
            error_message=data["error_message"],
        )


@dataclass(frozen=True)
class PersistentState:
    last_check: CheckResult | None = None
    task: UpdateTask | None = None

    _FIELDS: ClassVar[frozenset[str]] = frozenset({"last_check", "task"})

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_check": self.last_check.to_dict()
            if self.last_check is not None
            else None,
            "task": self.task.to_dict() if self.task is not None else None,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "PersistentState":
        data = _require_exact_keys(payload, cls._FIELDS)
        return cls(
            last_check=(
                CheckResult.from_dict(data["last_check"])
                if data["last_check"] is not None
                else None
            ),
            task=UpdateTask.from_dict(data["task"])
            if data["task"] is not None
            else None,
        )


@dataclass(frozen=True)
class TaskView:
    id: UUID
    from_version: str
    to_version: str
    stage: Stage
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    backup_complete: bool
    rolled_back: bool
    cleanup: CleanupStatus
    error_code: str
    error_message: str

    def __post_init__(self) -> None:
        validate_version(self.from_version)
        validate_version(self.to_version)
        _validate_utc(self.created_at)
        _validate_utc(self.started_at)
        _validate_utc(self.finished_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "from_version": self.from_version,
            "to_version": self.to_version,
            "stage": self.stage.value,
            "created_at": _datetime_to_dict(self.created_at),
            "started_at": _datetime_to_dict(self.started_at),
            "finished_at": _datetime_to_dict(self.finished_at),
            "backup_complete": self.backup_complete,
            "rolled_back": self.rolled_back,
            "cleanup": self.cleanup.value,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class StatusView:
    current_version: str
    current_published_at: datetime | None
    latest_version: str | None
    latest_published_at: datetime | None
    update_available: bool
    checked_at: datetime | None
    task: TaskView | None

    def __post_init__(self) -> None:
        validate_version(self.current_version)
        _validate_utc(self.current_published_at)
        if self.latest_version is not None:
            validate_version(self.latest_version)
        _validate_utc(self.latest_published_at)
        _validate_utc(self.checked_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_version": self.current_version,
            "current_published_at": _datetime_to_dict(self.current_published_at),
            "latest_version": self.latest_version,
            "latest_published_at": _datetime_to_dict(self.latest_published_at),
            "update_available": self.update_available,
            "checked_at": _datetime_to_dict(self.checked_at),
            "task": self.task.to_dict() if self.task is not None else None,
        }


def image_identity_from_dict(payload: Any) -> ImageIdentity:
    return ImageIdentity.from_dict(payload)


def check_result_from_dict(payload: Any) -> CheckResult:
    return CheckResult.from_dict(payload)


def update_task_from_dict(payload: Any) -> UpdateTask:
    return UpdateTask.from_dict(payload)


def persistent_state_from_dict(payload: Any) -> PersistentState:
    return PersistentState.from_dict(payload)
