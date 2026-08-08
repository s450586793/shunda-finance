from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from updater.platform import PlatformConfig

_CONFIG_ROOT = Path("/config")
_STATE_ROOT = Path("/state")
_COMPOSE_FILE = _CONFIG_ROOT / "compose.yml"
_ENV_FILE = _CONFIG_ROOT / ".env"
_STATE_FILE = _STATE_ROOT / "update-state.json"
_FIXED_ENVIRONMENT_NAMES = frozenset(
    {
        "SHUNDA_COMPOSE_FILE",
        "SHUNDA_ENV_FILE",
        "SHUNDA_UPDATE_STATE_FILE",
        "SHUNDA_WEB_REPOSITORY",
        "SHUNDA_UPDATER_LISTEN",
    }
)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class UpdaterConfig:
    token: str
    listen: tuple[str, int]
    state_file: Path
    platform: PlatformConfig

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> UpdaterConfig:
        _reject_fixed_boundary_overrides(environ)
        token = _require_private_token(environ, "SHUNDA_UPDATER_TOKEN")
        _require_exact(environ, "SHUNDA_COMPOSE_PROJECT", "shunda-finance")
        _validated_child(_CONFIG_ROOT, _COMPOSE_FILE)
        _validated_child(_CONFIG_ROOT, _ENV_FILE)
        state_file = _validated_child(_STATE_ROOT, _STATE_FILE)
        return cls(
            token=token,
            listen=("0.0.0.0", 8090),
            state_file=state_file,
            platform=PlatformConfig(),
        )


def _reject_fixed_boundary_overrides(environ: Mapping[str, str]) -> None:
    if _FIXED_ENVIRONMENT_NAMES.intersection(environ):
        raise ConfigError("invalid_updater_configuration")


def _require_private_token(environ: Mapping[str, str], name: str) -> str:
    token = environ.get(name)
    if not isinstance(token, str) or not token.strip():
        raise ConfigError("invalid_updater_configuration")
    try:
        valid_length = len(token.encode("utf-8")) >= 32
    except UnicodeError as error:
        raise ConfigError("invalid_updater_configuration") from error
    if not valid_length:
        raise ConfigError("invalid_updater_configuration")
    return token


def _require_exact(environ: Mapping[str, str], name: str, expected: str) -> None:
    if environ.get(name) != expected:
        raise ConfigError("invalid_updater_configuration")


def _validated_child(root: Path, child: Path) -> Path:
    try:
        if root.is_symlink():
            raise ValueError("root_is_symlink")
        resolved_root = root.resolve(strict=True)
        resolved_child = child.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigError("invalid_updater_configuration") from error
    if not resolved_root.is_dir() or resolved_child.parent != resolved_root:
        raise ConfigError("invalid_updater_configuration")
    return resolved_child
