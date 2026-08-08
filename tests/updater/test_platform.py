import json
import stat
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from tests.updater.fakes import FakeHTTPConnection, FakeHTTPResponse, ScriptedRunner
from updater import platform as platform_module
from updater.platform import (
    DockerPlatform,
    PlatformConfig,
    SafeOperationError,
)
from updater.runner import SafeCommandError
from updater.types import (
    CleanupJournal,
    CleanupStep,
    CleanupStepStatus,
    ImageIdentity,
    Stage,
    UpdateTask,
)

REPOSITORY = "ghcr.io/s450586793/shunda-finance-web"
ORIGINAL_ID = "sha256:" + "1" * 64
TARGET_ID = "sha256:" + "2" * 64
OTHER_ID = "sha256:" + "3" * 64
ORIGINAL_DIGEST = "sha256:" + "a" * 64
TARGET_DIGEST = "sha256:" + "b" * 64
CONTAINER_ID = "c" * 64
TASK_ID = UUID("12345678-1234-5678-9234-567812345678")
ROLLBACK_ALIAS = f"shunda-finance-rollback-web:{TASK_ID}"
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
COMPOSE_PREFIX = (
    "docker",
    "compose",
    "--project-name",
    "shunda-finance",
    "--env-file",
    "/config/.env",
    "-f",
    "/config/compose.yml",
)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_name", "other-project"),
        ("compose_file", Path("/host/other.yml")),
        ("env_file", Path("/host/other.env")),
        ("web_repository", "registry.invalid/other-web"),
        ("web_health_url", "http://other:8000/health/"),
        ("task_root", Path("/host/other-tasks")),
    ],
)
def test_platform_rejects_every_nonproduction_config_before_docker_mutation(
    field, value
):
    config = PlatformConfig(**{field: value})
    runner = ScriptedRunner(b"")

    with pytest.raises(SafeOperationError, match="^invalid_platform_config$"):
        DockerPlatform(runner, config)

    assert runner.calls == []


def _assign_runtime_config(platform, config):
    platform._config = config


def _force_runtime_config(platform, config):
    object.__setattr__(platform, "_config", config)


@pytest.mark.parametrize(
    "replace_config",
    [_assign_runtime_config, _force_runtime_config],
    ids=["ordinary-assignment", "object-setattr"],
)
def test_runtime_config_replacement_cannot_change_compose_argv(replace_config):
    runner = ScriptedRunner(b"")
    platform = DockerPlatform(runner)
    replace_config(
        platform,
        PlatformConfig(
            project_name="other-project",
            compose_file=Path("/host/other.yml"),
            env_file=Path("/host/other.env"),
        ),
    )

    platform.stop_web()

    assert _argv(runner) == [(*COMPOSE_PREFIX, "stop", "web")]


@pytest.mark.parametrize(
    "replace_config",
    [_assign_runtime_config, _force_runtime_config],
    ids=["ordinary-assignment", "object-setattr"],
)
def test_runtime_config_replacement_cannot_change_repository_argv(replace_config):
    runner = ScriptedRunner(SafeCommandError("command_failed"))
    platform = DockerPlatform(runner)
    replace_config(
        platform,
        PlatformConfig(web_repository="registry.invalid/other-web"),
    )

    with pytest.raises(SafeOperationError, match="^pull_failed$"):
        platform.resolve_stable()

    assert _argv(runner) == [("docker", "pull", f"{REPOSITORY}:stable")]


@pytest.mark.parametrize(
    "replace_config",
    [_assign_runtime_config, _force_runtime_config],
    ids=["ordinary-assignment", "object-setattr"],
)
def test_runtime_config_replacement_cannot_change_health_target(replace_config):
    connection = FakeHTTPConnection(FakeHTTPResponse(200, b'{"status":"ok"}'))
    factory_calls = []

    def factory(host, port, timeout):
        factory_calls.append((host, port, timeout))
        return connection

    platform = DockerPlatform(ScriptedRunner(), connection_factory=factory)
    replace_config(
        platform,
        PlatformConfig(web_health_url="http://other:9000/private"),
    )

    platform.health()

    assert factory_calls == [("web", 8000, 5.0)]
    assert connection.requests == [("GET", "/health/", {"Accept": "application/json"})]


@pytest.mark.parametrize(
    "replace_config",
    [_assign_runtime_config, _force_runtime_config],
    ids=["ordinary-assignment", "object-setattr"],
)
def test_runtime_config_replacement_cannot_change_task_override_path(
    tmp_path, monkeypatch, replace_config
):
    production_override = (
        Path("/state/tasks") / str(TASK_ID) / "target-compose.yml"
    )

    def write_task_override(task_id, filename, payload):
        assert (task_id, filename) == (TASK_ID, "target-compose.yml")
        assert payload.endswith(b"    pull_policy: never\n")
        return production_override

    monkeypatch.setattr(
        platform_module, "_write_task_override", write_task_override, raising=False
    )
    runner = ScriptedRunner(b"")
    platform = DockerPlatform(runner)
    replace_config(platform, PlatformConfig(task_root=tmp_path))

    platform.migrate_target(_target_identity(), task_id=TASK_ID)

    assert _argv(runner) == [
        (
            *COMPOSE_PREFIX,
            "-f",
            str(production_override),
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "web",
            "python",
            "manage.py",
            "migrate",
        )
    ]


def _argv(runner):
    return [call.argv for call in runner.calls]


def _container_json(
    image_id,
    config_image,
    *,
    project="shunda-finance",
    service="web",
    one_off="False",
):
    return json.dumps(
        {
            "Id": CONTAINER_ID,
            "Image": image_id,
            "ConfigImage": config_image,
            "Project": project,
            "Service": service,
            "OneOff": one_off,
        },
        separators=(",", ":"),
    ).encode()


def _image_json(
    image_id,
    digest,
    version,
    *,
    tags=None,
    repo=REPOSITORY,
    revision="d" * 40,
    created="2026-08-07T08:09:10Z",
    os_name="linux",
    architecture="amd64",
    extra_digest=False,
):
    repo_digests = [f"{repo}@{digest}"]
    if extra_digest:
        repo_digests.append(f"{repo}@{'sha256:' + 'e' * 64}")
    return json.dumps(
        {
            "Id": image_id,
            "RepoTags": tags if tags is not None else [f"{repo}:{version}"],
            "RepoDigests": repo_digests,
            "Version": version,
            "Revision": revision,
            "Created": created,
            "Os": os_name,
            "Architecture": architecture,
        },
        separators=(",", ":"),
    ).encode()


def _original_identity(*, rollback_alias=ROLLBACK_ALIAS):
    return ImageIdentity(
        repository=REPOSITORY,
        version="v1.2.3",
        digest=ORIGINAL_DIGEST,
        image_id=ORIGINAL_ID,
        tags=(f"{REPOSITORY}:v1.2.3",),
        rollback_alias=rollback_alias,
        published_at=datetime(2026, 8, 7, 8, 9, 10, tzinfo=UTC),
    )


def _target_identity():
    return ImageIdentity(
        repository=REPOSITORY,
        version="v1.3.0",
        digest=TARGET_DIGEST,
        image_id=TARGET_ID,
        tags=(f"{REPOSITORY}:v1.3.0",),
        published_at=datetime(2026, 8, 7, 8, 9, 10, tzinfo=UTC),
    )


def _task():
    return UpdateTask(
        id=TASK_ID,
        original=_original_identity(),
        target=_target_identity(),
        stage=Stage.CLEANING,
        created_at=datetime(2026, 8, 7, 9, 0, tzinfo=UTC),
    )


def _task_with_cleanup_started(step: CleanupStep) -> UpdateTask:
    statuses = {candidate.value: CleanupStepStatus.NOT_STARTED for candidate in CleanupStep}
    for candidate in CleanupStep:
        if candidate is step:
            statuses[candidate.value] = CleanupStepStatus.STARTED
            break
        statuses[candidate.value] = CleanupStepStatus.COMPLETED
    return replace(_task(), cleanup_journal=CleanupJournal(**statuses))


def _target_env(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text("SHUNDA_WEB_IMAGE_TAG=v1.3.0\n")
    path.chmod(0o600)
    return path


def _platform(
    runner,
    monkeypatch,
    *,
    env_file: Path | None = None,
    task_root: Path | None = None,
):
    if env_file is not None:
        monkeypatch.setattr(
            platform_module,
            "_read_environment_file",
            lambda: platform_module._read_environment_at(env_file),
        )
        monkeypatch.setattr(
            platform_module,
            "_replace_environment_file",
            lambda payload: platform_module._atomic_private_replace(env_file, payload),
        )
    if task_root is not None:

        def write_task_override(task_id, filename, payload):
            platform_module._write_task_override_at(
                task_root, task_id, filename, payload
            )
            return Path("/state/tasks") / str(task_id) / filename

        monkeypatch.setattr(
            platform_module, "_write_task_override", write_task_override
        )
    return DockerPlatform(runner)


def test_inspect_web_uses_only_fixed_service_and_strict_inspects():
    runner = ScriptedRunner(
        f"{CONTAINER_ID}\n".encode(),
        _container_json(ORIGINAL_ID, f"{REPOSITORY}:v1.2.3"),
        _image_json(
            ORIGINAL_ID,
            ORIGINAL_DIGEST,
            "v1.2.3",
            tags=[f"{REPOSITORY}:v1.2.3"],
        ),
    )

    identity = DockerPlatform(runner).inspect_web()

    assert identity == _original_identity(rollback_alias="")
    assert _argv(runner) == [
        (*COMPOSE_PREFIX, "ps", "--all", "-q", "web"),
        ("docker", "container", "inspect", "--format", CONTAINER_FORMAT, CONTAINER_ID),
        ("docker", "image", "inspect", "--format", IMAGE_FORMAT, ORIGINAL_ID),
    ]


@pytest.mark.parametrize(
    "malformed",
    [
        b"not-json",
        b"[]",
        json.dumps({"Id": ORIGINAL_ID}).encode(),
        _container_json(ORIGINAL_ID, f"{REPOSITORY}:v1.2.3", service="db"),
        _container_json(ORIGINAL_ID, "postgres:16-alpine"),
        _container_json(
            ORIGINAL_ID,
            f"{REPOSITORY}:v1.2.3",
            one_off="True",
        ),
    ],
)
def test_inspect_web_rejects_malformed_or_non_web_container_without_leaking(malformed):
    runner = ScriptedRunner(f"{CONTAINER_ID}\n".encode(), malformed)

    with pytest.raises(SafeOperationError, match="^inspect_failed$") as raised:
        DockerPlatform(runner).inspect_web()

    assert "postgres" not in str(raised.value)
    assert len(runner.calls) == 2


@pytest.mark.parametrize(
    "container_output",
    [
        b"",
        f"{CONTAINER_ID}\n{'d' * 64}\n".encode(),
        b"../../host\n",
    ],
)
def test_inspect_web_rejects_ambiguous_container_id_before_inspect(container_output):
    runner = ScriptedRunner(container_output)

    with pytest.raises(SafeOperationError, match="^inspect_failed$"):
        DockerPlatform(runner).inspect_web()

    assert _argv(runner) == [(*COMPOSE_PREFIX, "ps", "--all", "-q", "web")]


def test_resolve_stable_on_fresh_daemon_pulls_and_verifies_immutable_tag():
    runner = ScriptedRunner(
        b"pulled-stable",
        _image_json(
            TARGET_ID,
            TARGET_DIGEST,
            "v1.3.0",
            tags=[f"{REPOSITORY}:stable"],
        ),
        b"pulled-version",
        _image_json(
            TARGET_ID,
            TARGET_DIGEST,
            "v1.3.0",
            tags=[f"{REPOSITORY}:stable", f"{REPOSITORY}:v1.3.0"],
        ),
    )

    identity = DockerPlatform(runner).resolve_stable()

    assert identity == _target_identity()
    assert identity.published_at == datetime(2026, 8, 7, 8, 9, 10, tzinfo=UTC)
    assert _argv(runner) == [
        ("docker", "pull", f"{REPOSITORY}:stable"),
        (
            "docker",
            "image",
            "inspect",
            "--format",
            IMAGE_FORMAT,
            f"{REPOSITORY}:stable",
        ),
        ("docker", "pull", f"{REPOSITORY}:v1.3.0"),
        (
            "docker",
            "image",
            "inspect",
            "--format",
            IMAGE_FORMAT,
            f"{REPOSITORY}:v1.3.0",
        ),
    ]


@pytest.mark.parametrize(
    "immutable_output",
    [
        _image_json(
            OTHER_ID,
            TARGET_DIGEST,
            "v1.3.0",
            tags=[f"{REPOSITORY}:stable", f"{REPOSITORY}:v1.3.0"],
        ),
        _image_json(
            TARGET_ID,
            ORIGINAL_DIGEST,
            "v1.3.0",
            tags=[f"{REPOSITORY}:stable", f"{REPOSITORY}:v1.3.0"],
        ),
        _image_json(
            TARGET_ID,
            TARGET_DIGEST,
            "v1.3.1",
            tags=[f"{REPOSITORY}:stable", f"{REPOSITORY}:v1.3.0"],
        ),
        _image_json(
            TARGET_ID,
            TARGET_DIGEST,
            "v1.3.0",
            tags=[f"{REPOSITORY}:stable", f"{REPOSITORY}:v1.3.0"],
            revision="e" * 40,
        ),
        _image_json(
            TARGET_ID,
            TARGET_DIGEST,
            "v1.3.0",
            tags=[f"{REPOSITORY}:stable", f"{REPOSITORY}:v1.3.0"],
            created="2026-08-07T08:09:10+00:00",
        ),
        _image_json(
            TARGET_ID,
            TARGET_DIGEST,
            "v1.3.0",
            tags=[f"{REPOSITORY}:stable", f"{REPOSITORY}:v1.3.0"],
            os_name="windows",
        ),
        _image_json(
            TARGET_ID,
            TARGET_DIGEST,
            "v1.3.0",
            tags=[f"{REPOSITORY}:stable", f"{REPOSITORY}:v1.3.0"],
            architecture="arm64",
        ),
        _image_json(
            TARGET_ID,
            TARGET_DIGEST,
            "v1.3.0",
            tags=[f"{REPOSITORY}:stable"],
        ),
    ],
    ids=[
        "image-id",
        "digest",
        "version",
        "revision",
        "created",
        "os",
        "architecture",
        "missing-immutable-tag",
    ],
)
def test_resolve_stable_rejects_immutable_tag_identity_mismatch(immutable_output):
    runner = ScriptedRunner(
        b"pulled-stable",
        _image_json(
            TARGET_ID,
            TARGET_DIGEST,
            "v1.3.0",
            tags=[f"{REPOSITORY}:stable"],
        ),
        b"pulled-version",
        immutable_output,
    )

    with pytest.raises(SafeOperationError, match="^pull_failed$"):
        DockerPlatform(runner).resolve_stable()

    assert _argv(runner)[2] == ("docker", "pull", f"{REPOSITORY}:v1.3.0")


@pytest.mark.parametrize(
    "image_output",
    [
        _image_json(TARGET_ID, TARGET_DIGEST, "latest"),
        _image_json(TARGET_ID, TARGET_DIGEST, "v1.3.0", revision=None),
        _image_json(TARGET_ID, TARGET_DIGEST, "v1.3.0", created=None),
        _image_json(TARGET_ID, TARGET_DIGEST, "v1.3.0", repo=REPOSITORY + "-updater"),
        _image_json(TARGET_ID, TARGET_DIGEST, "v1.3.0", extra_digest=True),
    ],
)
def test_resolve_stable_rejects_missing_or_ambiguous_identity(image_output):
    runner = ScriptedRunner(b"pulled", image_output)

    with pytest.raises(SafeOperationError, match="^pull_failed$"):
        DockerPlatform(runner).resolve_stable()


def test_verify_target_pulls_digest_and_matches_current_platform():
    runner = ScriptedRunner(
        b"pulled",
        _image_json(TARGET_ID, TARGET_DIGEST, "v1.3.0"),
        f"{CONTAINER_ID}\n".encode(),
        _container_json(ORIGINAL_ID, f"{REPOSITORY}:v1.2.3"),
        _image_json(ORIGINAL_ID, ORIGINAL_DIGEST, "v1.2.3"),
    )

    DockerPlatform(runner).verify_target(_target_identity())

    assert _argv(runner)[0:2] == [
        ("docker", "pull", f"{REPOSITORY}@{TARGET_DIGEST}"),
        (
            "docker",
            "image",
            "inspect",
            "--format",
            IMAGE_FORMAT,
            f"{REPOSITORY}@{TARGET_DIGEST}",
        ),
    ]


def test_verify_target_rejects_digest_identity_or_platform_mismatch():
    runner = ScriptedRunner(
        b"pulled",
        _image_json(TARGET_ID, TARGET_DIGEST, "v1.3.0", architecture="arm64"),
        f"{CONTAINER_ID}\n".encode(),
        _container_json(ORIGINAL_ID, f"{REPOSITORY}:v1.2.3"),
        _image_json(ORIGINAL_ID, ORIGINAL_DIGEST, "v1.2.3"),
    )

    with pytest.raises(SafeOperationError, match="^target_identity_mismatch$"):
        DockerPlatform(runner).verify_target(_target_identity())


@pytest.mark.parametrize(
    "target",
    [
        replace(_target_identity(), repository=REPOSITORY + "-updater"),
        replace(_target_identity(), digest="sha256:bad"),
        replace(_target_identity(), image_id="sha256:bad"),
        replace(_target_identity(), rollback_alias="caller-alias"),
    ],
)
def test_verify_target_rejects_caller_controlled_identity_before_any_command(target):
    runner = ScriptedRunner()

    with pytest.raises(SafeOperationError, match="^target_identity_mismatch$"):
        DockerPlatform(runner).verify_target(target)

    assert runner.calls == []


@pytest.mark.parametrize(
    ("operation", "expected_code"),
    [
        (lambda platform: platform.verify_target(object()), "target_identity_mismatch"),
        (
            lambda platform: platform.migrate_target(object(), task_id=TASK_ID),
            "migration_failed",
        ),
        (
            lambda platform: platform.start_target(object(), task_id=TASK_ID),
            "start_failed",
        ),
        (lambda platform: platform.tag_rollback(object()), "rollback_identity_mismatch"),
        (lambda platform: platform.start_rollback(object()), "rollback_identity_mismatch"),
        (
            lambda platform: platform.cleanup_original_step(
                object(), CleanupStep.VERSION_TAG
            ),
            "cleanup_refused",
        ),
    ],
)
def test_public_operations_map_invalid_callers_to_safe_errors_without_commands(
    tmp_path, monkeypatch, operation, expected_code
):
    runner = ScriptedRunner()
    platform = _platform(runner, monkeypatch, env_file=_target_env(tmp_path))

    with pytest.raises(SafeOperationError, match=f"^{expected_code}$"):
        operation(platform)

    assert runner.calls == []


def test_create_backup_accepts_only_exact_manifest_and_validates_both_files():
    manifest = (
        b"DB_BACKUP=/data/backups/db-20260807-120000.dump\n"
        b"UPLOADS_BACKUP=/data/backups/uploads-20260807-120000.tar.gz\n"
    )
    runner = ScriptedRunner(manifest, b"", b"")

    backups = DockerPlatform(runner).create_backup()

    assert backups == (
        "/data/backups/db-20260807-120000.dump",
        "/data/backups/uploads-20260807-120000.tar.gz",
    )
    assert _argv(runner) == [
        (*COMPOSE_PREFIX, "exec", "-T", "web", "/app/scripts/backup.sh"),
        (
            *COMPOSE_PREFIX,
            "exec",
            "-T",
            "web",
            "test",
            "-s",
            "/data/backups/db-20260807-120000.dump",
        ),
        (
            *COMPOSE_PREFIX,
            "exec",
            "-T",
            "web",
            "test",
            "-s",
            "/data/backups/uploads-20260807-120000.tar.gz",
        ),
    ]


@pytest.mark.parametrize(
    "manifest",
    [
        b"DB_BACKUP=/data/backups/db-20260807-120000.dump\n",
        b"DB_BACKUP=/data/backups/db-20260807-120000.dump\nDB_BACKUP=/data/backups/other.dump\nUPLOADS_BACKUP=/data/backups/uploads-20260807-120000.tar.gz\n",
        b"DB_BACKUP=/etc/passwd\nUPLOADS_BACKUP=/data/backups/uploads-20260807-120000.tar.gz\n",
        b"DB_BACKUP=/data/backups/db-20260807-120000.dump\nUPLOADS_BACKUP=/data/backups/uploads-20260807-120001.tar.gz\n",
        b"DB_BACKUP=/data/backups/db-20260807-120000.dump\nUPLOADS_BACKUP=/data/backups/uploads-20260807-120000.tar.gz\nEXTRA=value\n",
        b"DB_BACKUP=\xff\nUPLOADS_BACKUP=/data/backups/uploads-20260807-120000.tar.gz\n",
    ],
)
def test_create_backup_rejects_untrusted_manifest_before_path_validation(manifest):
    runner = ScriptedRunner(manifest)

    with pytest.raises(SafeOperationError, match="^backup_failed$"):
        DockerPlatform(runner).create_backup()

    assert len(runner.calls) == 1


def test_tag_rollback_verifies_runtime_and_creates_only_canonical_task_alias():
    task = _task()
    runner = ScriptedRunner(
        f"{CONTAINER_ID}\n".encode(),
        _container_json(ORIGINAL_ID, f"{REPOSITORY}:v1.2.3"),
        _image_json(ORIGINAL_ID, ORIGINAL_DIGEST, "v1.2.3"),
        b"tagged",
        _image_json(
            ORIGINAL_ID,
            ORIGINAL_DIGEST,
            "v1.2.3",
            tags=[f"{REPOSITORY}:v1.2.3", ROLLBACK_ALIAS],
        ),
    )

    DockerPlatform(runner).tag_rollback(task)

    assert task.original.rollback_alias == ROLLBACK_ALIAS
    assert _argv(runner)[3:] == [
        ("docker", "image", "tag", ORIGINAL_ID, ROLLBACK_ALIAS),
        ("docker", "image", "inspect", "--format", IMAGE_FORMAT, ROLLBACK_ALIAS),
    ]


def test_tag_rollback_refuses_noncanonical_recorded_alias_without_mutation():
    task = _task()
    task.original = replace(task.original, rollback_alias="malicious:latest")
    runner = ScriptedRunner()

    with pytest.raises(SafeOperationError, match="^rollback_identity_mismatch$"):
        DockerPlatform(runner).tag_rollback(task)

    assert runner.calls == []


def test_stop_web_uses_exact_compose_operation():
    runner = ScriptedRunner(b"")

    DockerPlatform(runner).stop_web()

    assert _argv(runner) == [(*COMPOSE_PREFIX, "stop", "web")]


def test_target_operations_write_private_digest_override_and_never_pull(
    tmp_path, monkeypatch
):
    runner = ScriptedRunner(b"", b"")
    platform = _platform(runner, monkeypatch, task_root=tmp_path)

    platform.migrate_target(_target_identity(), task_id=TASK_ID)
    platform.start_target(_target_identity(), task_id=TASK_ID)

    override = tmp_path / str(TASK_ID) / "target-compose.yml"
    production_override = (
        Path("/state/tasks") / str(TASK_ID) / "target-compose.yml"
    )
    assert override.read_text() == (
        "services:\n"
        "  web:\n"
        f"    image: {REPOSITORY}@{TARGET_DIGEST}\n"
        "    pull_policy: never\n"
    )
    assert stat.S_IMODE(override.stat().st_mode) == 0o600
    assert _argv(runner) == [
        (
            *COMPOSE_PREFIX,
            "-f",
            str(production_override),
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "web",
            "python",
            "manage.py",
            "migrate",
        ),
        (
            *COMPOSE_PREFIX,
            "-f",
            str(production_override),
            "up",
            "-d",
            "--no-deps",
            "web",
        ),
    ]


def test_start_rollback_writes_only_private_alias_override(tmp_path, monkeypatch):
    runner = ScriptedRunner(
        _image_json(
            ORIGINAL_ID,
            ORIGINAL_DIGEST,
            "v1.2.3",
            tags=[f"{REPOSITORY}:v1.2.3", ROLLBACK_ALIAS],
        ),
        b"",
    )
    task = _task()
    platform = _platform(runner, monkeypatch, task_root=tmp_path)

    platform.start_rollback(task)

    override = tmp_path / str(TASK_ID) / "rollback-compose.yml"
    production_override = (
        Path("/state/tasks") / str(TASK_ID) / "rollback-compose.yml"
    )
    assert override.read_text() == (
        "services:\n"
        "  web:\n"
        f"    image: {ROLLBACK_ALIAS}\n"
        "    pull_policy: never\n"
    )
    assert stat.S_IMODE(override.stat().st_mode) == 0o600
    assert _argv(runner) == [
        ("docker", "image", "inspect", "--format", IMAGE_FORMAT, ROLLBACK_ALIAS),
        (
            *COMPOSE_PREFIX,
            "-f",
            str(production_override),
            "up",
            "-d",
            "--no-deps",
            "web",
        ),
    ]


def test_all_generated_mutation_argv_exclude_shell_prune_force_and_other_services(
    tmp_path, monkeypatch
):
    runner = ScriptedRunner(
        b"",
        b"",
        b"",
        _image_json(
            ORIGINAL_ID,
            ORIGINAL_DIGEST,
            "v1.2.3",
            tags=[f"{REPOSITORY}:v1.2.3", ROLLBACK_ALIAS],
        ),
        b"",
    )
    platform = _platform(runner, monkeypatch, task_root=tmp_path)

    platform.stop_web()
    platform.migrate_target(_target_identity(), task_id=TASK_ID)
    platform.start_target(_target_identity(), task_id=TASK_ID)
    platform.start_rollback(_task())

    forbidden = {"sh", "bash", "-c", "prune", "--force", "db", "updater"}
    for call in runner.calls:
        assert forbidden.isdisjoint(call.argv)
        assert "caller-service" not in call.argv


def test_health_uses_direct_http_and_accepts_only_exact_ok_document():
    connection = FakeHTTPConnection(FakeHTTPResponse(200, b'{"status":"ok"}'))
    factory_calls = []

    def factory(host, port, timeout):
        factory_calls.append((host, port, timeout))
        return connection

    DockerPlatform(ScriptedRunner(), connection_factory=factory).health()

    assert factory_calls == [("web", 8000, 5.0)]
    assert connection.requests == [("GET", "/health/", {"Accept": "application/json"})]
    assert connection.closed is True


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (301, b'{"status":"ok"}'),
        (302, b'{"status":"ok"}'),
        (200, b'{"status":"degraded"}'),
        (200, b'{"status":"ok","extra":true}'),
        (200, b"not-json"),
        (200, b"x" * (64 * 1024 + 1)),
    ],
)
def test_health_rejects_redirects_malformed_json_and_oversized_body(status, body):
    connection = FakeHTTPConnection(FakeHTTPResponse(status, body))

    with pytest.raises(SafeOperationError, match="^health_check_failed$"):
        DockerPlatform(
            ScriptedRunner(), connection_factory=lambda *args: connection
        ).health()


def test_persist_version_atomically_changes_one_env_line_and_preserves_private_mode(
    tmp_path, monkeypatch
):
    env_file = tmp_path / ".env"
    env_file.write_bytes(
        b"POSTGRES_DB=finance\nSHUNDA_WEB_IMAGE_TAG=v1.2.3\nOTHER=value\n"
    )
    env_file.chmod(0o600)
    platform = _platform(ScriptedRunner(), monkeypatch, env_file=env_file)

    platform.persist_version("v1.3.0")

    assert env_file.read_bytes() == (
        b"POSTGRES_DB=finance\nSHUNDA_WEB_IMAGE_TAG=v1.3.0\nOTHER=value\n"
    )
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [env_file]


@pytest.mark.parametrize(
    "content",
    [
        b"OTHER=value\n",
        b"SHUNDA_WEB_IMAGE_TAG=v1.2.3\nSHUNDA_WEB_IMAGE_TAG=v1.2.3\n",
        b"DUP=value\nDUP=other\nSHUNDA_WEB_IMAGE_TAG=v1.2.3\n",
        b"SHUNDA_WEB_IMAGE_TAG=latest\n",
    ],
)
def test_persist_version_rejects_missing_duplicate_or_invalid_environment_keys(
    tmp_path, monkeypatch, content
):
    env_file = tmp_path / ".env"
    env_file.write_bytes(content)
    env_file.chmod(0o600)

    with pytest.raises(SafeOperationError, match="^persist_failed$"):
        _platform(
            ScriptedRunner(), monkeypatch, env_file=env_file
        ).persist_version("v1.3.0")

    assert env_file.read_bytes() == content


def test_persist_version_rejects_non_private_or_symlink_environment_file(
    tmp_path, monkeypatch
):
    real = tmp_path / "real.env"
    real.write_text("SHUNDA_WEB_IMAGE_TAG=v1.2.3\n")
    real.chmod(0o644)
    platform = _platform(ScriptedRunner(), monkeypatch, env_file=real)

    with pytest.raises(SafeOperationError, match="^persist_failed$"):
        platform.persist_version("v1.3.0")

    real.chmod(0o600)
    link = tmp_path / ".env"
    link.symlink_to(real)
    with pytest.raises(SafeOperationError, match="^persist_failed$"):
        _platform(
            ScriptedRunner(), monkeypatch, env_file=link
        ).persist_version("v1.3.0")


def test_cleanup_steps_revalidate_and_remove_only_the_recorded_reference(
    tmp_path, monkeypatch
):
    runner = ScriptedRunner(
        _image_json(
            ORIGINAL_ID,
            ORIGINAL_DIGEST,
            "v1.2.3",
            tags=[f"{REPOSITORY}:v1.2.3", ROLLBACK_ALIAS],
        ),
        b"",
        f"{ORIGINAL_ID}\n".encode(),
        f"{ORIGINAL_ID}\n".encode(),
        b"removed-tag",
        b"",
        _image_json(
            ORIGINAL_ID,
            ORIGINAL_DIGEST,
            "v1.2.3",
            tags=[ROLLBACK_ALIAS],
        ),
        b"",
        b"",
        f"{ORIGINAL_ID}\n".encode(),
        b"removed-alias",
        b"",
        f"{ORIGINAL_ID}\n".encode(),
        _image_json(ORIGINAL_ID, ORIGINAL_DIGEST, "v1.2.3", tags=[]),
        b"",
        b"",
        b"",
        b"removed-image",
        b"",
    )
    platform = _platform(runner, monkeypatch, env_file=_target_env(tmp_path))

    for step in CleanupStep:
        platform.cleanup_original_step(_task_with_cleanup_started(step), step)

    removals = [
        call.argv for call in runner.calls if call.argv[:3] == ("docker", "image", "rm")
    ]
    assert removals == [
        ("docker", "image", "rm", f"{REPOSITORY}:v1.2.3"),
        ("docker", "image", "rm", ROLLBACK_ALIAS),
        ("docker", "image", "rm", ORIGINAL_ID),
    ]


def test_cleanup_exposes_no_unjournaled_bulk_entry_point():
    assert not hasattr(DockerPlatform, "cleanup_original")


@pytest.mark.parametrize("step", tuple(CleanupStep))
def test_cleanup_missing_requires_a_started_durable_journal_before_commands(
    tmp_path, monkeypatch, step
):
    runner = ScriptedRunner()

    with pytest.raises(SafeOperationError, match="^cleanup_refused$"):
        _platform(
            runner, monkeypatch, env_file=_target_env(tmp_path)
        ).cleanup_original_step(_task(), step)

    assert runner.calls == []


def test_cleanup_version_tag_accepts_exact_absence_after_started_checkpoint(
    tmp_path, monkeypatch
):
    runner = ScriptedRunner(
        _image_json(
            ORIGINAL_ID,
            ORIGINAL_DIGEST,
            "v1.2.3",
            tags=[ROLLBACK_ALIAS],
        ),
        b"",
        f"{ORIGINAL_ID}\n".encode(),
        b"",
    )
    platform = _platform(runner, monkeypatch, env_file=_target_env(tmp_path))

    platform.cleanup_original_step(
        _task_with_cleanup_started(CleanupStep.VERSION_TAG),
        CleanupStep.VERSION_TAG,
    )

    assert not any(call.argv[:3] == ("docker", "image", "rm") for call in runner.calls)


def test_cleanup_version_tag_fails_closed_when_daemon_query_fails(
    tmp_path, monkeypatch
):
    runner = ScriptedRunner(
        _image_json(
            ORIGINAL_ID,
            ORIGINAL_DIGEST,
            "v1.2.3",
            tags=[f"{REPOSITORY}:v1.2.3", ROLLBACK_ALIAS],
        ),
        b"",
        SafeCommandError("command_failed"),
    )

    with pytest.raises(SafeOperationError, match="^cleanup_failed$"):
        _platform(
            runner, monkeypatch, env_file=_target_env(tmp_path)
        ).cleanup_original_step(
            _task_with_cleanup_started(CleanupStep.VERSION_TAG),
            CleanupStep.VERSION_TAG,
        )

    assert not any(call.argv[:3] == ("docker", "image", "rm") for call in runner.calls)


def test_cleanup_version_tag_rejects_reference_to_another_image(
    tmp_path, monkeypatch
):
    runner = ScriptedRunner(
        _image_json(
            ORIGINAL_ID,
            ORIGINAL_DIGEST,
            "v1.2.3",
            tags=[f"{REPOSITORY}:v1.2.3", ROLLBACK_ALIAS],
        ),
        b"",
        f"{ORIGINAL_ID}\n".encode(),
        f"{OTHER_ID}\n".encode(),
    )

    with pytest.raises(SafeOperationError, match="^cleanup_refused$"):
        _platform(
            runner, monkeypatch, env_file=_target_env(tmp_path)
        ).cleanup_original_step(
            _task_with_cleanup_started(CleanupStep.VERSION_TAG),
            CleanupStep.VERSION_TAG,
        )

    assert not any(call.argv[:3] == ("docker", "image", "rm") for call in runner.calls)


def test_cleanup_version_tag_rejects_daemon_absence_that_conflicts_with_inspect(
    tmp_path, monkeypatch
):
    runner = ScriptedRunner(
        _image_json(
            ORIGINAL_ID,
            ORIGINAL_DIGEST,
            "v1.2.3",
            tags=[f"{REPOSITORY}:v1.2.3", ROLLBACK_ALIAS],
        ),
        b"",
        f"{ORIGINAL_ID}\n".encode(),
        b"",
    )

    with pytest.raises(SafeOperationError, match="^cleanup_refused$"):
        _platform(
            runner, monkeypatch, env_file=_target_env(tmp_path)
        ).cleanup_original_step(
            _task_with_cleanup_started(CleanupStep.VERSION_TAG),
            CleanupStep.VERSION_TAG,
        )

    assert not any(call.argv[:3] == ("docker", "image", "rm") for call in runner.calls)


def test_cleanup_alias_rejects_when_version_tag_is_still_present(tmp_path, monkeypatch):
    runner = ScriptedRunner(
        _image_json(
            ORIGINAL_ID,
            ORIGINAL_DIGEST,
            "v1.2.3",
            tags=[f"{REPOSITORY}:v1.2.3", ROLLBACK_ALIAS],
        ),
        b"",
    )

    with pytest.raises(SafeOperationError, match="^cleanup_refused$"):
        _platform(
            runner, monkeypatch, env_file=_target_env(tmp_path)
        ).cleanup_original_step(
            _task_with_cleanup_started(CleanupStep.ROLLBACK_ALIAS),
            CleanupStep.ROLLBACK_ALIAS,
        )

    assert not any(call.argv[:3] == ("docker", "image", "rm") for call in runner.calls)


@pytest.mark.parametrize("step", tuple(CleanupStep))
def test_cleanup_step_rejects_ambiguous_daemon_reference_without_deletion(
    tmp_path, monkeypatch, step
):
    ambiguous = f"{ORIGINAL_ID}\n{OTHER_ID}\n".encode()
    outputs = {
        CleanupStep.VERSION_TAG: (
            _image_json(
                ORIGINAL_ID,
                ORIGINAL_DIGEST,
                "v1.2.3",
                tags=[f"{REPOSITORY}:v1.2.3", ROLLBACK_ALIAS],
            ),
            b"",
            f"{ORIGINAL_ID}\n".encode(),
            ambiguous,
        ),
        CleanupStep.ROLLBACK_ALIAS: (
            _image_json(
                ORIGINAL_ID,
                ORIGINAL_DIGEST,
                "v1.2.3",
                tags=[ROLLBACK_ALIAS],
            ),
            b"",
            b"",
            ambiguous,
        ),
        CleanupStep.IMAGE_ID: (ambiguous,),
    }
    runner = ScriptedRunner(*outputs[step])
    platform = _platform(runner, monkeypatch, env_file=_target_env(tmp_path))

    with pytest.raises(SafeOperationError, match="^cleanup_refused$"):
        platform.cleanup_original_step(_task_with_cleanup_started(step), step)

    assert not any(call.argv[:3] == ("docker", "image", "rm") for call in runner.calls)


def test_cleanup_image_id_accepts_exact_absence_after_started_checkpoint(
    tmp_path, monkeypatch
):
    runner = ScriptedRunner(b"", b"", b"", b"")
    platform = _platform(runner, monkeypatch, env_file=_target_env(tmp_path))

    platform.cleanup_original_step(
        _task_with_cleanup_started(CleanupStep.IMAGE_ID),
        CleanupStep.IMAGE_ID,
    )

    assert not any(call.argv[:3] == ("docker", "image", "rm") for call in runner.calls)


def test_cleanup_image_id_rejects_remaining_version_reference(tmp_path, monkeypatch):
    runner = ScriptedRunner(
        f"{ORIGINAL_ID}\n".encode(),
        _image_json(ORIGINAL_ID, ORIGINAL_DIGEST, "v1.2.3", tags=[]),
        b"",
        f"{ORIGINAL_ID}\n".encode(),
    )

    with pytest.raises(SafeOperationError, match="^cleanup_refused$"):
        _platform(
            runner, monkeypatch, env_file=_target_env(tmp_path)
        ).cleanup_original_step(
            _task_with_cleanup_started(CleanupStep.IMAGE_ID),
            CleanupStep.IMAGE_ID,
        )

    assert not any(call.argv[:3] == ("docker", "image", "rm") for call in runner.calls)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda task: replace(
            task, original=replace(task.original, repository=REPOSITORY + "-updater")
        ),
        lambda task: replace(task, original=replace(task.original, version="v1.2.4")),
        lambda task: replace(task, original=replace(task.original, digest=TARGET_DIGEST)),
        lambda task: replace(task, original=replace(task.original, image_id=TARGET_ID)),
        lambda task: replace(task, original=replace(task.original, rollback_alias="bad")),
        lambda task: replace(task, original=replace(task.original, tags=("postgres:16",))),
        lambda task: replace(task, target=replace(task.target, repository=REPOSITORY + "-updater")),
    ],
)
def test_cleanup_rejects_mismatched_recorded_identity_before_commands(
    tmp_path, monkeypatch, mutate
):
    task = mutate(_task())
    runner = ScriptedRunner()

    with pytest.raises(SafeOperationError, match="^cleanup_refused$"):
        _platform(runner, monkeypatch, env_file=_target_env(tmp_path)).cleanup_original_step(
            task, CleanupStep.VERSION_TAG
        )

    assert runner.calls == []


def test_cleanup_rejects_any_container_reference_before_deletion(
    tmp_path, monkeypatch
):
    runner = ScriptedRunner(
        _image_json(
            ORIGINAL_ID,
            ORIGINAL_DIGEST,
            "v1.2.3",
            tags=[f"{REPOSITORY}:v1.2.3", ROLLBACK_ALIAS],
        ),
        f"{CONTAINER_ID}\n".encode(),
    )

    with pytest.raises(SafeOperationError, match="^cleanup_refused$"):
        _platform(runner, monkeypatch, env_file=_target_env(tmp_path)).cleanup_original_step(
            _task_with_cleanup_started(CleanupStep.VERSION_TAG),
            CleanupStep.VERSION_TAG,
        )

    assert not any(call.argv[0:3] == ("docker", "image", "rm") for call in runner.calls)


@pytest.mark.parametrize(
    "tags",
    [
        [f"{REPOSITORY}:v1.2.3", ROLLBACK_ALIAS, "postgres:16-alpine"],
        [f"{REPOSITORY}:v1.2.3", ROLLBACK_ALIAS, REPOSITORY + "-updater:v1.2.3"],
        [f"{REPOSITORY}:v9.9.9", ROLLBACK_ALIAS],
    ],
)
def test_cleanup_rejects_unrecorded_db_updater_or_web_tags(
    tmp_path, monkeypatch, tags
):
    runner = ScriptedRunner(
        _image_json(ORIGINAL_ID, ORIGINAL_DIGEST, "v1.2.3", tags=tags)
    )

    with pytest.raises(SafeOperationError, match="^cleanup_refused$"):
        _platform(runner, monkeypatch, env_file=_target_env(tmp_path)).cleanup_original_step(
            _task_with_cleanup_started(CleanupStep.VERSION_TAG),
            CleanupStep.VERSION_TAG,
        )

    assert len(runner.calls) == 1


def test_cleanup_rejects_missing_labels_or_tag_pointing_to_other_image(
    tmp_path, monkeypatch
):
    missing_labels = ScriptedRunner(
        _image_json(
            ORIGINAL_ID,
            ORIGINAL_DIGEST,
            "v1.2.3",
            tags=[f"{REPOSITORY}:v1.2.3", ROLLBACK_ALIAS],
            revision=None,
        )
    )
    platform = _platform(
        missing_labels, monkeypatch, env_file=_target_env(tmp_path)
    )
    with pytest.raises(SafeOperationError, match="^cleanup_refused$"):
        platform.cleanup_original_step(
            _task_with_cleanup_started(CleanupStep.VERSION_TAG),
            CleanupStep.VERSION_TAG,
        )

    wrong_tag = ScriptedRunner(
        _image_json(
            ORIGINAL_ID,
            ORIGINAL_DIGEST,
            "v1.2.3",
            tags=[f"{REPOSITORY}:v1.2.3", ROLLBACK_ALIAS],
        ),
        b"",
        f"{ORIGINAL_ID}\n".encode(),
        f"{OTHER_ID}\n".encode(),
    )
    with pytest.raises(SafeOperationError, match="^cleanup_refused$"):
        _platform(
            wrong_tag, monkeypatch, env_file=_target_env(tmp_path)
        ).cleanup_original_step(
            _task_with_cleanup_started(CleanupStep.VERSION_TAG),
            CleanupStep.VERSION_TAG,
        )
    assert not any(call.argv[0:3] == ("docker", "image", "rm") for call in wrong_tag.calls)


def test_cleanup_rejects_duplicate_environment_keys_without_docker_commands(
    tmp_path, monkeypatch
):
    env_file = tmp_path / ".env"
    env_file.write_bytes(
        b"SHUNDA_WEB_IMAGE_TAG=v1.3.0\nSHUNDA_WEB_IMAGE_TAG=v1.3.0\n"
    )
    env_file.chmod(0o600)
    runner = ScriptedRunner()

    with pytest.raises(SafeOperationError, match="^cleanup_refused$"):
        _platform(runner, monkeypatch, env_file=env_file).cleanup_original_step(
            _task(), CleanupStep.VERSION_TAG
        )

    assert runner.calls == []
