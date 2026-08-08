from __future__ import annotations

from pathlib import Path

import pytest

import updater.config as config  # noqa: PLR0402

TOKEN = "a" * 32


def environment(**overrides: str) -> dict[str, str]:
    values = {
        "SHUNDA_UPDATER_TOKEN": TOKEN,
        "SHUNDA_COMPOSE_PROJECT": "shunda-finance",
    }
    values.update(overrides)
    return values


@pytest.fixture
def fixed_paths(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    config_root = tmp_path / "config"
    state_root = tmp_path / "state"
    config_root.mkdir()
    state_root.mkdir()
    (config_root / "compose.yml").write_text("services: {}\n")
    (config_root / ".env").write_text("WEB_VERSION=v0.2.0\n")
    monkeypatch.setattr(config, "_CONFIG_ROOT", config_root)
    monkeypatch.setattr(config, "_STATE_ROOT", state_root)
    monkeypatch.setattr(config, "_COMPOSE_FILE", config_root / "compose.yml")
    monkeypatch.setattr(config, "_ENV_FILE", config_root / ".env")
    monkeypatch.setattr(config, "_STATE_FILE", state_root / "update-state.json")
    return config_root, state_root


@pytest.mark.parametrize("token", ["", "   ", "a" * 31, "中" * 10])
def test_from_env_rejects_missing_blank_or_short_private_token(fixed_paths, token):
    values = environment(SHUNDA_UPDATER_TOKEN=token)

    with pytest.raises(config.ConfigError) as error:
        config.UpdaterConfig.from_env(values)

    assert str(error.value) == "invalid_updater_configuration"
    assert TOKEN not in str(error.value)
    if token:
        assert token not in str(error.value)


def test_from_env_requires_exact_project_and_returns_fixed_production_values(
    fixed_paths,
):
    with pytest.raises(config.ConfigError):
        config.UpdaterConfig.from_env(environment(SHUNDA_COMPOSE_PROJECT="other"))

    result = config.UpdaterConfig.from_env(environment())

    assert result.token == TOKEN
    assert result.listen == ("0.0.0.0", 8090)
    assert result.state_file == fixed_paths[1] / "update-state.json"
    assert result.platform.project_name == "shunda-finance"
    assert result.platform.web_repository == "ghcr.io/s450586793/shunda-finance-web"


@pytest.mark.parametrize(
    "name",
    [
        "SHUNDA_COMPOSE_FILE",
        "SHUNDA_ENV_FILE",
        "SHUNDA_UPDATE_STATE_FILE",
        "SHUNDA_WEB_REPOSITORY",
        "SHUNDA_UPDATER_LISTEN",
    ],
)
def test_from_env_rejects_environment_replacement_of_fixed_boundaries(
    fixed_paths, name
):
    with pytest.raises(config.ConfigError) as error:
        config.UpdaterConfig.from_env(environment(**{name: "/tmp/unsafe"}))

    assert "/tmp/unsafe" not in str(error.value)


def test_from_env_rejects_config_symlink_escaping_fixed_root(fixed_paths):
    config_root, _state_root = fixed_paths
    outside = config_root.parent / "outside.yml"
    outside.write_text("unsafe\n")
    (config_root / "compose.yml").unlink()
    (config_root / "compose.yml").symlink_to(outside)

    with pytest.raises(config.ConfigError):
        config.UpdaterConfig.from_env(environment())


def test_from_env_rejects_state_symlink_escaping_fixed_root(fixed_paths):
    _config_root, state_root = fixed_paths
    outside = state_root.parent / "outside.json"
    outside.write_text("{}")
    (state_root / "update-state.json").symlink_to(outside)

    with pytest.raises(config.ConfigError):
        config.UpdaterConfig.from_env(environment())
