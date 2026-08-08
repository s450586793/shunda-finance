# 顺达财务系统 Web 手动升级实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为顺达财务系统交付仅老板可操作的 Web 手动升级能力，通过 GitHub/GHCR 发布不可变镜像，在 DSM 上先备份、失败自动回滚，并在成功后精准删除本次旧 Web 镜像。

**Architecture:** 新增独立 Python updater 容器，只有它挂载 Docker socket，并通过固定 Compose/Docker argv 管理同一项目的 `web` 服务。Django 以内部 Token 代理 Owner 请求并提供进度页面；GitHub Actions 发布 Web/updater 不可变镜像，DSM 首次人工切换后由 Web 页面完成后续 Web-only 升级。

**Tech Stack:** Python 3.12、Django 5.2、Python stdlib HTTP server、Docker CLI/Compose v2、PostgreSQL 16、Gunicorn、Node test runner、Playwright、GitHub Actions、GHCR、DSM Container Manager。

## Global Constraints

- 源码仓库为公开 GitHub `s450586793/shunda-finance`，公开仓库和镜像层不得包含 `.env`、生产数据、附件、备份、Token、Cookie、私钥或账号口令。
- release 只接受规范 `vX.Y.Z`；Web/updater 都发布不可变版本标签，两个镜像全部成功后才能移动 Web `stable`。
- updater 不使用 `stable`，DSM 必须通过 `SHUNDA_UPDATER_IMAGE_TAG=vX.Y.Z` 固定版本，并且只能人工升级 updater。
- 只有 updater 挂载 `/var/run/docker.sock`；Web 不得挂载 Docker socket，updater 不得发布宿主机端口。
- Web 页面只升级 Compose project `shunda-finance` 的 `web` 服务，不得替换 `db` 或 updater。
- 浏览器输入不得控制镜像名、服务名、Compose 路径、环境文件、Docker API 路径、宿主路径或命令。
- 目标切换使用 `repo@sha256:digest` 任务 override 和 `pull_policy: never`；回滚使用任务级本地 alias 和 `pull_policy: never`。
- 升级前数据库 dump 和上传附件归档必须均成功且非空；失败不得停止 Web。
- 数据库迁移必须向后兼容旧 Web；破坏性迁移不得进入 `stable`，生产升级不自动反向迁移或自动恢复数据库。
- 成功后只删除任务记录且重新验证过的旧 Web 标签、alias 和 image ID；禁止 `docker image prune`、模糊匹配和 `--force`。
- 回滚失败或实际身份含糊必须进入 `manual_intervention`，阻止新任务并保留新旧镜像。
- Token、Cookie、数据库口令、Docker digest、image ID、alias、内部路径、原始命令错误和堆栈不得进入浏览器、普通日志或审计记录。
- 外部地址 `http://sd.ace-station.top:1111` 和现有财务数据卷保持不变；允许 Web 升级期间约 10-60 秒短暂停机。
- 所有新行为先写失败测试；只暂存当前任务列出的文件，禁止 `git add .`。

## File Structure

- Create `updater/types.py`: SemVer、阶段、镜像身份、检查结果、任务和脱敏 View。
- Create `updater/store.py`: mode-`0600` JSON 状态文件的原子读写。
- Create `updater/runner.py`: 无 Shell 的有界子进程执行器和安全错误。
- Create `updater/platform.py`: 固定 Docker/Compose inspect、备份、切换、健康、持久版本和精准清理。
- Create `updater/manager.py`: 单任务状态机、成功流程、回滚和重启恢复。
- Create `updater/http_server.py`: 内部 Token API、严格 JSON 边界和安全响应。
- Create `updater/config.py`, `updater/main.py`: 固定环境配置、依赖装配和优雅关闭。
- Create `apps/system_update/`: Django updater client、Owner 页面/API、请求审计模型和 migration。
- Create `templates/system_update/index.html`: 紧凑版本、进度、结果和确认 UI。
- Create `static/js/system-update.js`: 检查、启动、轮询和断线恢复。
- Modify `apps/accounts/`: Owner decorator 和安全 bootstrap command。
- Modify `apps/core/views.py`: 数据库感知健康检查。
- Modify `scripts/backup.sh`: 输出 updater 可严格解析的两项备份 manifest。
- Modify `Dockerfile`, `compose.yml`, `.env.example`, `.dockerignore`: Web/updater 镜像和 DSM 三服务边界。
- Create `.github/workflows/release-images.yml`: 测试、不可变发布和 Web stable promotion。
- Create `scripts/system-update-*.sh`: Compose 契约、DSM 成功/回滚 smoke 和部署脚本。

---

### Task 1: Updater 状态类型与原子存储

**Files:**
- Create: `updater/__init__.py`
- Create: `updater/types.py`
- Create: `updater/store.py`
- Create: `tests/updater/__init__.py`
- Create: `tests/updater/test_types.py`
- Create: `tests/updater/test_store.py`

**Interfaces:**
- Produces: `validate_version(value: str) -> str`, `version_key(value: str) -> tuple[int, int, int]`, `Stage`, `CleanupStatus`, `ImageIdentity`, `CheckResult`, `UpdateTask`, `PersistentState`, `TaskView`, `StatusView`。
- Produces: `FileStateStore(path: Path)`, `FileStateStore.load() -> PersistentState`, `FileStateStore.save(state: PersistentState) -> None`。

- [ ] **Step 1: Write failing type and redaction tests**

```python
def test_validate_version_accepts_only_canonical_release_versions():
    assert validate_version("v0.2.1") == "v0.2.1"
    for value in ("0.2.1", "v0.2", "v0.2.1-rc.1", "latest", "v0.2.1 evil"):
        with pytest.raises(ValueError):
            validate_version(value)


def test_task_public_view_excludes_private_docker_identity():
    task = update_task_fixture()
    encoded = json.dumps(task.public_view().to_dict())
    for private in ("sha256:", "rollback", "/data/backups", "ghcr.io"):
        assert private not in encoded
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `pytest tests/updater/test_types.py -q`

Expected: FAIL because `updater.types` does not exist.

- [ ] **Step 3: Implement exact enums and dataclasses**

```python
VERSION_PATTERN = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


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
```

Define the exact dataclass fields below; every timestamp is timezone-aware UTC and every tuple is serialized as a JSON array:

```python
@dataclass(frozen=True)
class ImageIdentity:
    repository: str
    version: str
    digest: str
    image_id: str
    tags: tuple[str, ...] = ()
    rollback_alias: str = ""
    published_at: datetime | None = None


@dataclass(frozen=True)
class CheckResult:
    current: ImageIdentity
    target: ImageIdentity
    available: bool
    checked_at: datetime


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
    error_code: str = ""
    error_message: str = ""


@dataclass(frozen=True)
class PersistentState:
    last_check: CheckResult | None = None
    task: UpdateTask | None = None


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


@dataclass(frozen=True)
class StatusView:
    current_version: str
    latest_version: str | None
    latest_published_at: datetime | None
    update_available: bool
    checked_at: datetime | None
    task: TaskView | None
```

`UpdateTask.public_view()` may populate only these View fields. `version_key()` validates first, then returns the three integer regex groups.

- [ ] **Step 4: Write failing atomic store tests**

Cover missing file, round trip, parent mode `0700`, state mode `0600`, `1 MiB` limit, unknown JSON field rejection, invalid enum/UUID, temporary-file cleanup, fsync, and injected `os.replace` failure preserving the previous valid state.

```python
def test_store_round_trip_uses_private_permissions(tmp_path):
    path = tmp_path / "nested" / "update-state.json"
    store = FileStateStore(path)
    state = persistent_state_fixture()
    store.save(state)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert store.load() == state
```

- [ ] **Step 5: Implement strict JSON serialization and atomic replacement**

Write to a same-directory `NamedTemporaryFile`, `chmod(0600)`, flush, `os.fsync`, close, `os.replace`, then fsync the parent directory. Decode through explicit `from_dict` functions that compare exact key sets before constructing dataclasses; never use permissive `**payload` on untrusted state.

```python
class FileStateStore:
    MAX_BYTES = 1_048_576

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> PersistentState:
        if not self.path.exists():
            return PersistentState()
        if self.path.stat().st_size > self.MAX_BYTES:
            raise StateStoreError("state_too_large")
        return persistent_state_from_dict(json.loads(self.path.read_bytes()))

    def save(self, state: PersistentState) -> None:
        payload = json.dumps(state.to_dict(), separators=(",", ":")).encode()
        if len(payload) > self.MAX_BYTES:
            raise StateStoreError("state_too_large")
        atomic_private_replace(self.path, payload)
```

- [ ] **Step 6: Verify and commit**

Run: `pytest tests/updater/test_types.py tests/updater/test_store.py -q`

```bash
git add updater/__init__.py updater/types.py updater/store.py tests/updater/__init__.py tests/updater/test_types.py tests/updater/test_store.py
git diff --cached --check
git commit -m "feat: 定义升级状态与原子存储"
```

### Task 2: Web 备份、版本与数据库健康契约

**Files:**
- Modify: `scripts/backup.sh`
- Modify: `apps/core/views.py`
- Modify: `config/settings/base.py`
- Modify: `config/settings/prod.py`
- Modify: `.env.example`
- Modify: `tests/test_deployment.py`
- Modify: `tests/test_health.py`

**Interfaces:**
- Produces: successful `backup.sh` stdout exactly `DB_BACKUP=<absolute path>` and `UPLOADS_BACKUP=<absolute path>` after both atomic moves。
- Produces: `GET /health/` returns 200 only after `SELECT 1`, otherwise 503 with `{"status":"unavailable"}`。
- Produces: `settings.SHUNDA_RELEASE_VERSION`, validated as canonical `vX.Y.Z` in production。

- [ ] **Step 1: Write failing backup manifest, health and release-version tests**

```python
def test_health_check_returns_503_when_database_query_fails(client, monkeypatch):
    monkeypatch.setattr("apps.core.views.connections", failing_connections())
    response = client.get("/health/")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_production_release_version_is_canonical(monkeypatch):
    configure_production(monkeypatch)
    monkeypatch.setenv("SHUNDA_RELEASE_VERSION", "latest")
    with pytest.raises(ImproperlyConfigured, match="SHUNDA_RELEASE_VERSION"):
        reload_production_settings()
```

Add a subprocess test with fake `pg_dump`, `tar`, `mv` and `find` proving the two manifest lines appear only after both final files exist and are nonempty; failure produces no success manifest.

- [ ] **Step 2: Run tests and verify RED**

Run: `pytest tests/test_health.py tests/test_deployment.py -q`

Expected: FAIL because health does not query PostgreSQL, release version is absent, and backup stdout has no manifest.

- [ ] **Step 3: Implement the runtime contracts**

```python
def health_check(request):
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
        if row != (1,):
            raise DatabaseError("unexpected health result")
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})
```

Set base `SHUNDA_RELEASE_VERSION` to the development-safe canonical default `v0.0.0`; production must replace it through `_required_value` and validate it with the same exact release regex. In `backup.sh`, run `test -s` after both moves, then print the two exact keys with private absolute paths for updater consumption.

- [ ] **Step 4: Verify and commit**

Run: `pytest tests/test_health.py tests/test_deployment.py -q`

```bash
git add scripts/backup.sh apps/core/views.py config/settings/base.py config/settings/prod.py .env.example tests/test_deployment.py tests/test_health.py
git diff --cached --check
git commit -m "feat: 加固升级备份与健康检查"
```

### Task 3: 受限 Docker/Compose 平台

**Files:**
- Create: `updater/runner.py`
- Create: `updater/platform.py`
- Create: `tests/updater/fakes.py`
- Create: `tests/updater/test_runner.py`
- Create: `tests/updater/test_platform.py`

**Interfaces:**
- Consumes: Task 1 `ImageIdentity`, `validate_version`。
- Produces: `CommandRunner.run(argv: Sequence[str], timeout: float, stdin: bytes | None = None) -> CompletedCommand`。
- Produces: `DockerPlatform.inspect_web()`, `resolve_stable()`, `verify_target()`, `create_backup()`, `tag_rollback()`, `stop_web()`, `migrate_target()`, `start_target()`, `start_rollback()`, `health()`, `persist_version()`, `cleanup_original()`。

- [ ] **Step 1: Write failing runner tests**

Assert argv execution uses `shell=False`, strips environment to a fixed allowlist, caps stdout/stderr at `64 KiB`, enforces timeouts, and maps all OS/process failures to `SafeCommandError(code)` whose string never contains raw output.

```python
def test_runner_never_uses_a_shell(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, b"ok", b""))
    monkeypatch.setattr(subprocess, "run", run)
    CommandRunner().run(("docker", "version"), timeout=5)
    assert run.call_args.kwargs["shell"] is False
```

- [ ] **Step 2: Run runner tests and verify RED**

Run: `pytest tests/updater/test_runner.py -q`

- [ ] **Step 3: Implement the bounded runner**

Use `subprocess.run` with a tuple argv, `shell=False`, `check=False`, fixed `PATH`, `LANG=C`, `capture_output=True`, caller timeout and optional bytes input. Reject NUL and empty argv elements. Log only error code and executable basename.

```python
completed = subprocess.run(
    tuple(argv),
    input=stdin,
    capture_output=True,
    check=False,
    shell=False,
    timeout=timeout,
    env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
)
if completed.returncode:
    raise SafeCommandError("command_failed")
return CompletedCommand(completed.returncode, bounded(completed.stdout), bounded(completed.stderr))
```

- [ ] **Step 4: Write failing platform whitelist tests**

Use a scripted fake runner and assert exact argv sequences for:

- `docker compose --project-name shunda-finance --env-file /config/.env -f /config/compose.yml ps -q web`;
- container and image inspect with strict JSON parsing;
- `docker pull ghcr.io/s450586793/shunda-finance-web:stable` followed by exact digest/OCI label inspection;
- backup exec and two `test -s` validations;
- task override containing only `services.web.image=repo@sha256:digest` and `pull_policy=never`;
- rollback override containing only canonical task alias and `pull_policy=never`;
- version `.env` atomic replacement changing exactly one `SHUNDA_WEB_IMAGE_TAG` line and preserving mode `0600`;
- cleanup refusing mismatched repository/version/digest/ID/alias/tags, any container reference, `db`/updater images, missing labels and duplicate environment keys.

Mutation assertions must prove no generated argv contains `sh`, `bash`, `-c`, `prune`, `--force`, arbitrary service names or caller-controlled paths.

- [ ] **Step 5: Implement fixed platform configuration and identity checks**

```python
@dataclass(frozen=True)
class PlatformConfig:
    project_name: str = "shunda-finance"
    compose_file: Path = Path("/config/compose.yml")
    env_file: Path = Path("/config/.env")
    web_repository: str = "ghcr.io/s450586793/shunda-finance-web"
    web_health_url: str = "http://web:8000/health/"
```

Create override files inside `/state/tasks/<uuid>/` with mode `0600`. Parse Docker JSON with exact type/key checks. `create_backup()` accepts only the two exact manifest keys under `/data/backups/` and validates each through fixed `docker compose exec -T web test -s <path>` argv. Health accepts only direct HTTP 200 with `{"status":"ok"}` and rejects redirects.

The only valid rollback alias is `shunda-finance-rollback-web:<canonical-task-uuid>`. Cleanup removes only the exact original version tags recorded before the switch, then that task alias, then the verified image ID; a missing tag may be ignored only when image inspect proves it no longer points at the original ID.

- [ ] **Step 6: Verify and commit**

Run: `pytest tests/updater/test_runner.py tests/updater/test_platform.py -q`

```bash
git add updater/runner.py updater/platform.py tests/updater/fakes.py tests/updater/test_runner.py tests/updater/test_platform.py
git diff --cached --check
git commit -m "feat: 限定升级 Docker 操作"
```

### Task 4: 版本检查与成功状态机

**Files:**
- Create: `updater/manager.py`
- Create: `tests/updater/test_manager_success.py`

**Interfaces:**
- Consumes: `FileStateStore`, `DockerPlatform`。
- Produces: `UpdateManager.check() -> StatusView`, `UpdateManager.start(target_version: str) -> tuple[TaskView, Callable[[], None]]`, `UpdateManager.status() -> StatusView`。

- [ ] **Step 1: Write failing check tests**

Cover current/target valid versions, no update, downgrade, same version with changed digest, mismatched repository, stale check after 2 minutes, concurrent check serialization and stable changing between Check and Start.

```python
def test_check_reports_only_a_newer_semver(manager, platform):
    platform.current = image("v0.2.0")
    platform.stable = image("v0.2.1")
    view = manager.check()
    assert view.update_available is True
    assert view.current_version == "v0.2.0"
    assert view.latest_version == "v0.2.1"
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `pytest tests/updater/test_manager_success.py -q`

- [ ] **Step 3: Implement Check, Status and exact Start acceptance**

Compare versions through Task 1 `version_key()`. Persist the full private `CheckResult`; return only `StatusView`. `start()` re-resolves stable and requires its version and digest to equal the unexpired check before it creates a UUID task.

```python
def check(self) -> StatusView:
    with self._operation_lock:
        self._reject_active_task()
        current = self._platform.inspect_web()
        target = self._platform.resolve_stable()
        result = CheckResult(
            current=current,
            target=target,
            available=version_key(target.version) > version_key(current.version),
            checked_at=self._now(),
        )
        self._save(last_check=result)
        return self.status()


def start(self, target_version: str) -> tuple[TaskView, Callable[[], None]]:
    checked = self._require_fresh_check(target_version, max_age=timedelta(minutes=2))
    resolved = self._platform.resolve_stable()
    if (resolved.version, resolved.digest) != (checked.target.version, checked.target.digest):
        raise UpdateConflict("target_changed")
    return self._create_task(checked)
```

- [ ] **Step 4: Write failing successful transaction test**

Assert the exact call order and checkpoint after every operation:

```python
assert platform.calls == [
    "backup", "verify_target", "tag_rollback", "stop_web", "migrate_target",
    "start_target", "health_target", "stabilize", "persist_version", "cleanup_original",
]
assert state.task.stage is Stage.SUCCEEDED
assert state.task.cleanup is CleanupStatus.COMPLETE
```

Also cover cleanup failure producing `succeeded + pending`, db/updater never appearing in platform calls, and active/manual task rejecting Check/Start.

- [ ] **Step 5: Implement the success state machine**

`start()` returns a closure bound to the created task UUID; the HTTP layer may run only that closure in one background thread. Each platform action is preceded by a saved stage checkpoint. After success, refresh `last_check.current` from actual Web and mark `update_available=False`.

```python
def _execute(self, task_id: UUID) -> None:
    self._transition(task_id, Stage.BACKING_UP)
    self._record_backups(task_id, self._platform.create_backup())
    self._transition(task_id, Stage.PULLING)
    self._platform.verify_target(self._task(task_id).target)
    self._platform.tag_rollback(self._task(task_id))
    self._transition(task_id, Stage.STOPPING_WEB)
    self._platform.stop_web()
    self._transition(task_id, Stage.MIGRATING)
    self._platform.migrate_target(self._task(task_id).target)
    self._transition(task_id, Stage.STARTING_WEB)
    self._platform.start_target(self._task(task_id).target)
    self._complete_health_persist_and_cleanup(task_id)
```

- [ ] **Step 6: Verify and commit**

Run: `pytest tests/updater/test_manager_success.py -q`

```bash
git add updater/manager.py tests/updater/test_manager_success.py
git diff --cached --check
git commit -m "feat: 实现 Web 升级成功流程"
```

### Task 5: 失败回滚与重启恢复

**Files:**
- Modify: `updater/manager.py`
- Create: `tests/updater/test_manager_failure.py`
- Create: `tests/updater/test_manager_recovery.py`

**Interfaces:**
- Produces: `UpdateManager.recover() -> None`，在 HTTP listener 启动前调用一次。

- [ ] **Step 1: Write the failure matrix before implementation**

Parameterize every platform stage. Backup/pull/identity failures must leave Web running and perform zero rollback calls. Stop/migrate/start/health/stabilize/persist failures must call rollback alias with `pull_policy: never`, health-check old Web, restore the previous `.env` tag if it changed, then finish as `failed` with `rolled_back=True`.

```python
@pytest.mark.parametrize("failure", AFTER_STOP_FAILURES)
def test_post_stop_failure_rolls_back_and_preserves_both_images(failure):
    manager, platform, store = failure_manager(failure)
    run_started_task(manager)
    assert platform.calls[-2:] == ["start_rollback", "health_rollback"]
    assert store.load().task.stage is Stage.FAILED
    assert platform.cleanup_calls == []
```

- [ ] **Step 2: Run failure tests and verify RED**

Run: `pytest tests/updater/test_manager_failure.py -q`

- [ ] **Step 3: Implement conservative rollback**

Do not reuse a cancelled request context for background work. Rollback errors become bounded `rollback_failed` and `manual_intervention`; they never trigger cleanup. Keep original and target identities in private state.

```python
def _handle_failure(self, task_id: UUID, code: str, *, web_was_stopped: bool) -> None:
    if not web_was_stopped:
        self._finish_failed(task_id, code, rolled_back=False)
        return
    self._transition(task_id, Stage.ROLLING_BACK)
    try:
        self._platform.start_rollback(self._task(task_id))
        self._platform.health(expected=self._task(task_id).original)
    except SafeOperationError:
        self._finish_manual(task_id, "rollback_failed")
        return
    self._finish_failed(task_id, code, rolled_back=True)
```

- [ ] **Step 4: Write restart recovery tests**

Cover every nonterminal stage with actual images in original, target and ambiguous combinations. Recover target only for `persisting_version`/`cleaning` when target health is proven; otherwise restore old alias. Missing alias, digest, ID or unexpected container image enters manual intervention. Terminal tasks are unchanged.

```python
def test_recover_ambiguous_runtime_enters_manual_without_cleanup(manager, platform, store):
    store.save(active_state(Stage.STARTING_WEB))
    platform.actual = unrelated_image()
    manager.recover()
    assert store.load().task.stage is Stage.MANUAL_INTERVENTION
    assert platform.cleanup_calls == []
```

- [ ] **Step 5: Implement and verify recovery**

Run: `pytest tests/updater/test_manager_failure.py tests/updater/test_manager_recovery.py -q`

- [ ] **Step 6: Commit**

```bash
git add updater/manager.py tests/updater/test_manager_failure.py tests/updater/test_manager_recovery.py
git diff --cached --check
git commit -m "feat: 支持升级回滚和恢复"
```

### Task 6: Updater 内部 API 与进程

**Files:**
- Create: `updater/config.py`
- Create: `updater/http_server.py`
- Create: `updater/main.py`
- Create: `tests/updater/test_config.py`
- Create: `tests/updater/test_http_server.py`
- Create: `tests/updater/test_main.py`

**Interfaces:**
- Consumes: Task 4/5 `UpdateManager`。
- Produces: fixed internal HTTP API at `:8090` and executable `python -m updater.main`。

- [ ] **Step 1: Write failing configuration tests**

Require non-whitespace Token of at least 32 UTF-8 bytes, exact `shunda-finance` project, `/config/compose.yml`, `/config/.env`, `/state/update-state.json`, fixed GHCR repository and loopback-safe listen `:8090`. Reject path traversal, symlinks escaping `/config` or `/state`, alternate repositories and arbitrary listen hosts.

- [ ] **Step 2: Implement strict `UpdaterConfig.from_env()`**

Use explicit environment names and never include values in error messages. Validate file parents through `Path.resolve()` and exact allowed roots.

```python
@dataclass(frozen=True)
class UpdaterConfig:
    token: str
    listen: tuple[str, int]
    state_file: Path
    platform: PlatformConfig

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "UpdaterConfig":
        token = require_private_token(environ, "SHUNDA_UPDATER_TOKEN", minimum_bytes=32)
        require_exact(environ, "SHUNDA_COMPOSE_PROJECT", "shunda-finance")
        return cls(
            token=token,
            listen=("0.0.0.0", 8090),
            state_file=validated_child("/state", "/state/update-state.json"),
            platform=PlatformConfig(),
        )
```

- [ ] **Step 3: Write failing API tests**

Assert:

- `/health` is public and only GET;
- other routes require exact `Authorization: Bearer <token>` with `hmac.compare_digest`;
- Check and Start accept only one JSON object, reject unknown keys, arrays, null, trailing JSON and bodies above `4 KiB`;
- active conflicts return 409, safe operational failures return 503, malformed input returns 400;
- all public responses serialize Views and never contain repository/digest/ID/alias/path/Token/raw exception;
- Start creates exactly one worker thread and repeated Start cannot duplicate a task.

- [ ] **Step 4: Implement HTTP routing with stdlib server**

Use `ThreadingHTTPServer` and a handler factory bound to manager/token. Set `Content-Type: application/json`, `Cache-Control: no-store`, response size bounds and fixed error documents. Disable default request logging or replace it with method/path/status only.

```python
ROUTES = {
    ("GET", "/health"): handle_health,
    ("GET", "/v1/status"): handle_status,
    ("POST", "/v1/check"): handle_check,
    ("POST", "/v1/update"): handle_start,
}


def authorize(header: str, expected_token: str) -> bool:
    prefix = "Bearer "
    return header.startswith(prefix) and hmac.compare_digest(
        header[len(prefix):].encode(), expected_token.encode()
    )
```

- [ ] **Step 5: Write and implement main lifecycle tests**

Prove `recover()` completes before `serve_forever()`, SIGTERM calls `shutdown()`, active state is preserved for next recovery, and startup configuration errors contain no secret.

```python
def main() -> int:
    config = UpdaterConfig.from_env(os.environ)
    manager = build_manager(config)
    manager.recover()
    server = build_server(config, manager)
    install_shutdown_handlers(server)
    server.serve_forever(poll_interval=0.5)
    return 0
```

- [ ] **Step 6: Verify and commit**

Run: `pytest tests/updater/test_config.py tests/updater/test_http_server.py tests/updater/test_main.py -q`

```bash
git add updater/config.py updater/http_server.py updater/main.py tests/updater/test_config.py tests/updater/test_http_server.py tests/updater/test_main.py
git diff --cached --check
git commit -m "feat: 提供内部升级服务"
```

### Task 7: Owner 权限、bootstrap 和 Django updater client

**Files:**
- Create: `apps/accounts/bootstrap.py`
- Create: `apps/accounts/management/commands/bootstrap_owner_user.py`
- Modify: `apps/accounts/management/commands/bootstrap_finance_user.py`
- Modify: `apps/accounts/decorators.py`
- Modify: `config/settings/base.py`
- Modify: `config/settings/prod.py`
- Create: `apps/system_update/__init__.py`
- Create: `apps/system_update/apps.py`
- Create: `apps/system_update/client.py`
- Create: `tests/accounts/test_bootstrap_owner_user.py`
- Modify: `tests/accounts/test_bootstrap_finance_user.py`
- Create: `tests/system_update/__init__.py`
- Create: `tests/system_update/test_client.py`
- Modify: `tests/test_health.py`

**Interfaces:**
- Produces: `owner_required(view)`。
- Produces: `bootstrap_role_user(...)` shared command helper without changing finance command behavior。
- Produces: `UpdaterClient.status()`, `.check()`, `.start(target_version)` returning strict public dataclasses or `UpdaterUnavailable(code)`。

- [ ] **Step 1: Write failing Owner authorization and bootstrap tests**

Mirror every finance bootstrap case for owner: create, idempotent reuse, explicit reset, stdin, missing/weak password, no leakage and one audit. Add decorator tests proving anonymous redirects, Finance receives 403, Owner succeeds, and superuser without Owner role receives 403.

- [ ] **Step 2: Refactor common bootstrap behavior and add owner command**

Keep exact finance environment names. Owner uses `BOOTSTRAP_OWNER_USERNAME`, `BOOTSTRAP_OWNER_PASSWORD`, `Role.OWNER` and audit action `owner_user.bootstrapped`. `assign_role` continues enforcing one business role per user.

```python
class Command(BaseCommand):
    help = "幂等创建或授权初始老板用户"

    def handle(self, *args, **options):
        return bootstrap_role_user(
            role=Role.OWNER,
            username_env="BOOTSTRAP_OWNER_USERNAME",
            password_env="BOOTSTRAP_OWNER_PASSWORD",
            audit_action="owner_user.bootstrapped",
            options=options,
            stdout=self.stdout,
        )
```

- [ ] **Step 3: Write failing client/config tests**

Require `SHUNDA_UPDATER_URL` and Token together in production, URL exactly `http://updater:8090`, no query/userinfo/fragments, and Token non-whitespace >= 32 bytes. Mock HTTP for timeout, refusal, 401/409/503, oversized response, unknown JSON field, wrong enum and raw-secret body.

```python
client = UpdaterClient("http://updater:8090", token, transport=fake)
status = client.check()
assert status.latest_version == "v0.2.1"
assert fake.last_request.body == b"{}"
assert fake.last_request.headers["Authorization"] == f"Bearer {token}"
```

- [ ] **Step 4: Implement bounded strict client**

Define `UpdaterTaskView` and `UpdaterStatusView` in `client.py` with the exact Task 1 public JSON fields. Use `urllib.request` with fixed route strings, JSON bodies, connect/total timeout under 10 seconds and max response `64 KiB`. Map raw failures to fixed codes; never expose response bodies.

```python
class UpdaterClient:
    def status(self) -> UpdaterStatusView:
        return self._request("GET", "/v1/status", body=None)

    def check(self) -> UpdaterStatusView:
        return self._request("POST", "/v1/check", body={})

    def start(self, target_version: str) -> UpdaterTaskView:
        return self._request(
            "POST", "/v1/update", body={"target_version": target_version}
        )
```

- [ ] **Step 5: Verify and commit**

Run: `pytest tests/accounts/test_bootstrap_finance_user.py tests/accounts/test_bootstrap_owner_user.py tests/system_update/test_client.py tests/test_health.py -q`

```bash
git add apps/accounts/bootstrap.py apps/accounts/management/commands/bootstrap_owner_user.py apps/accounts/management/commands/bootstrap_finance_user.py apps/accounts/decorators.py config/settings/base.py config/settings/prod.py apps/system_update/__init__.py apps/system_update/apps.py apps/system_update/client.py tests/accounts/test_bootstrap_owner_user.py tests/accounts/test_bootstrap_finance_user.py tests/system_update/__init__.py tests/system_update/test_client.py tests/test_health.py
git diff --cached --check
git commit -m "feat: 增加老板升级权限与客户端"
```

### Task 8: Django 升级代理、请求记录与导航

**Files:**
- Create: `apps/system_update/models.py`
- Create: `apps/system_update/migrations/__init__.py`
- Create: `apps/system_update/migrations/0001_initial.py`
- Create: `apps/system_update/views.py`
- Create: `apps/system_update/urls.py`
- Modify: `config/settings/base.py`
- Modify: `config/urls.py`
- Modify: `apps/core/context_processors.py`
- Create: `tests/system_update/test_models.py`
- Create: `tests/system_update/test_views.py`

**Interfaces:**
- Produces routes `system-update:index`, `system-update:status`, `system-update:check`, `system-update:start` under `/system/update/`。
- Produces `SystemUpdateRequest(task_id, requested_by, target_version, result, terminal_recorded_at)` for idempotent audit correlation。

- [ ] **Step 1: Write failing model and authorization tests**

Assert UUID task uniqueness, canonical version validation, protected requester, anonymous redirect, Finance/superuser-without-role 403, Owner GET success and Owner-only navigation. Do not rely only on hidden links.

- [ ] **Step 2: Write failing API contract tests**

Assert Status is GET-only; Check and Start are POST-only and CSRF protected. Check sends exact `{}`. Start accepts only `target_version`, requires canonical value and calls client once. Map client errors to bounded JSON and HTTP statuses. Responses must not contain a fixture Token, digest, image ID, alias, path or raw body.

- [ ] **Step 3: Implement model, migration, routes and views**

```python
@owner_required
@require_POST
def start(request):
    payload = strict_json_object(request, allowed={"target_version"})
    target = validate_release_version(payload.get("target_version"))
    task = get_updater_client().start(target)
    update_request = SystemUpdateRequest.objects.create(
        task_id=task.id,
        requested_by=request.user,
        target_version=target,
        result="active",
    )
    record_audit(request.user, "system_update.started", update_request, {
        "task_id": str(task.id), "target_version": target,
    })
    return JsonResponse(task.to_dict(), status=202)
```

On each Status response, lock the matching request row and write one terminal audit only once. Audit changes contain task ID, target version, terminal stage, rolled_back, cleanup and safe error code.

- [ ] **Step 4: Add Owner-only navigation**

Append a real `系统设置` item with `settings` icon only when `user_has_role(user, Role.OWNER)`; do not show a disabled placeholder to Finance.

```python
if user_has_role(request.user, Role.OWNER):
    items.append({
        "label": "系统设置",
        "href": reverse("system-update:index"),
        "icon": "settings",
    })
```

- [ ] **Step 5: Verify and commit**

Run: `pytest tests/system_update/test_models.py tests/system_update/test_views.py tests/accounts/test_roles.py -q`

```bash
git add apps/system_update/models.py apps/system_update/migrations/__init__.py apps/system_update/migrations/0001_initial.py apps/system_update/views.py apps/system_update/urls.py config/settings/base.py config/urls.py apps/core/context_processors.py tests/system_update/test_models.py tests/system_update/test_views.py
git diff --cached --check
git commit -m "feat: 代理系统升级请求"
```

### Task 9: 系统升级页面与断线恢复

**Files:**
- Create: `templates/system_update/index.html`
- Create: `static/js/system-update.js`
- Create: `static/js/system-update.test.js`
- Modify: `static/css/app.css`
- Modify: `tests/system_update/test_views.py`
- Create: `tests/e2e/system-update.spec.ts`

**Interfaces:**
- Consumes: Task 8 JSON routes and public View schema。
- Produces: responsive Owner page with check, exact confirmation, bounded polling and terminal display。

- [ ] **Step 1: Write failing pure JavaScript tests**

Export and test `nextRetryDelay(attempt)`, `statusPresentation(status)`, `canStart(status)`, and `pollDecision(status)`. Required retry delays are 2, 4, then capped 5 seconds; terminal/manual stops polling; active task suppresses Check; malformed payload fails closed without rendering raw values.

```javascript
test("retry delay is bounded", () => {
  assert.deepEqual([0, 1, 2, 3, 9].map(nextRetryDelay), [2000, 4000, 5000, 5000, 5000]);
});
```

- [ ] **Step 2: Run Node tests and verify RED**

Run: `npm run test:js`

- [ ] **Step 3: Implement pure presentation and polling logic**

Use an explicit allowlist from stage/error code to Chinese labels. Never set `innerHTML` with server data. Provide one timer, clear it on terminal state/pagehide, and prevent concurrent fetches.

```javascript
export function nextRetryDelay(attempt) {
  return [2000, 4000, 5000][Math.min(attempt, 2)];
}

export function pollDecision(status) {
  const stage = status?.task?.stage;
  if (["succeeded", "failed", "manual_intervention"].includes(stage)) {
    return { poll: false, delay: 0 };
  }
  return { poll: Boolean(stage), delay: 2000 };
}
```

- [ ] **Step 4: Write failing template/view assertions**

Assert the page has stable DOM IDs/data attributes, CSRF token, current/latest/version sections, fixed-height progress list, backup/rollback/cleanup states, native `<dialog>`, icon buttons with tooltips, and no visible internal command or Docker identity copy.

- [ ] **Step 5: Implement template and compact responsive styles**

Follow existing `page-header`, `panel`, `section-heading`, `button` and status patterns. Cards remain at the existing `6px` radius. Use Lucide `refresh-cw`, `download`, `shield-check`, `rotate-ccw`, `trash-2` and `alert-triangle`; do not add custom SVG.

```html
<section class="panel system-update-panel" data-system-update
         data-status-url="{% url 'system-update:status' %}"
         data-check-url="{% url 'system-update:check' %}"
         data-start-url="{% url 'system-update:start' %}">
  <input type="hidden" data-csrf value="{{ csrf_token }}">
  <button class="button button-secondary" data-check type="button">
    <i data-lucide="refresh-cw" aria-hidden="true"></i>检查更新
  </button>
  <dialog data-confirm-dialog aria-labelledby="update-confirm-title"></dialog>
</section>
```

- [ ] **Step 6: Add Playwright workflow**

Mock Django updater endpoints at the HTTP boundary and verify Owner navigation, exact confirmation, one Start request, 503/network recovery, page reload restoring task, terminal stop and mobile width without overlap.

```typescript
test("Owner confirms one exact version and polling resumes after restart", async ({ page }) => {
  await installSystemUpdateRoutes(page, { current: "v0.2.0", latest: "v0.2.1" });
  await page.goto("/system/update/");
  await page.getByRole("button", { name: "检查更新" }).click();
  await page.getByRole("button", { name: "升级到 v0.2.1" }).click();
  await expect(page.getByText("升级成功")).toBeVisible();
  expect(startRequests).toHaveLength(1);
});
```

- [ ] **Step 7: Verify and commit**

Run: `npm run test:js`

Run: `pytest tests/system_update/test_views.py -q`

Run: `npm run test:e2e -- tests/e2e/system-update.spec.ts`

```bash
git add templates/system_update/index.html static/js/system-update.js static/js/system-update.test.js static/css/app.css tests/system_update/test_views.py tests/e2e/system-update.spec.ts
git diff --cached --check
git commit -m "feat: 增加系统升级页面"
```

### Task 10: 生产镜像与三服务 Compose

**Files:**
- Modify: `Dockerfile`
- Modify: `.dockerignore`
- Modify: `compose.yml`
- Modify: `.env.example`
- Create: `scripts/deploy-dsm.sh`
- Create: `scripts/system-update-compose.test.sh`
- Modify: `tests/test_deployment.py`

**Interfaces:**
- Produces Docker targets `web` and `updater`。
- Produces DSM Compose services `db`, `web`, `updater` with exact socket/network/port boundary。

- [ ] **Step 1: Write failing static and rendered Compose tests**

Assert from rendered JSON:

- Web image default is `ghcr.io/s450586793/shunda-finance-web:${SHUNDA_WEB_IMAGE_TAG}` and has no build section in production Compose;
- updater image uses required immutable `SHUNDA_UPDATER_IMAGE_TAG`;
- Docker socket source/target appears exactly once and only under updater;
- updater has no published ports, Web remains `127.0.0.1:8000:8000`;
- updater mounts `${SHUNDA_APP_DIR}` at `/config` and `${SHUNDA_DATA_DIR}/updater-state` at `/state`;
- db/web/updater share one internal network, Web depends on healthy db, updater health is internal;
- only Web carries `SHUNDA_RELEASE_VERSION`, only Django/updater carry the internal Token;
- Web upgrade override cannot include db or updater.
- `.dockerignore` excludes `.env`, `.git`, `media`, `backups`, `exports`, PostgreSQL data, caches and local workflow scratch directories.

- [ ] **Step 2: Run the contract and verify RED**

Run: `bash scripts/system-update-compose.test.sh`

- [ ] **Step 3: Refactor Dockerfile into pinned targets**

Use shared Python build stage for Web and a pinned Docker CLI Alpine runtime for updater. The Web target receives release version/revision/created build args and applies matching OCI labels. The updater target installs only Python 3, CA certificates and curl beside Docker CLI/Compose; copy only `updater/`.

```dockerfile
FROM python:3.12.11-slim-bookworm AS web
ARG SHUNDA_RELEASE_VERSION
ARG SHUNDA_RELEASE_REVISION
ARG SHUNDA_RELEASE_CREATED
LABEL org.opencontainers.image.version=$SHUNDA_RELEASE_VERSION \
      org.opencontainers.image.revision=$SHUNDA_RELEASE_REVISION \
      org.opencontainers.image.created=$SHUNDA_RELEASE_CREATED
ENV SHUNDA_RELEASE_VERSION=$SHUNDA_RELEASE_VERSION

FROM docker:27.5.1-cli AS updater
RUN apk add --no-cache ca-certificates curl docker-cli-compose python3
COPY updater /app/updater
WORKDIR /app
ENTRYPOINT ["python3", "-m", "updater.main"]
```

Both targets run noninteractive commands. Web remains UID/GID 10001. updater runs as root because the DSM Docker socket is root-owned, drops all unnecessary Linux capabilities in Compose, and is the only service allowed the socket.

- [ ] **Step 4: Implement Compose and deployment script**

`.env.example` must require `SHUNDA_APP_DIR`, `SHUNDA_DATA_DIR`, `SHUNDA_WEB_IMAGE_TAG`, `SHUNDA_UPDATER_IMAGE_TAG`, `SHUNDA_UPDATER_TOKEN`, and existing production values. `deploy-dsm.sh` validates exact versions and >=32-byte Token, renders Compose, pulls Web/updater, starts the three services, runs migrations for first deployment, waits for database-aware Web and updater health, and never prints secrets.

```yaml
services:
  web:
    image: ghcr.io/s450586793/shunda-finance-web:${SHUNDA_WEB_IMAGE_TAG:?required}
    pull_policy: never
    env_file: .env
    environment:
      SHUNDA_UPDATER_URL: http://updater:8090
      SHUNDA_UPDATER_TOKEN: ${SHUNDA_UPDATER_TOKEN:?required}
    ports: ["127.0.0.1:8000:8000"]
  updater:
    image: ghcr.io/s450586793/shunda-finance-updater:${SHUNDA_UPDATER_IMAGE_TAG:?required}
    pull_policy: never
    environment:
      SHUNDA_UPDATER_TOKEN: ${SHUNDA_UPDATER_TOKEN:?required}
      SHUNDA_COMPOSE_PROJECT: shunda-finance
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ${SHUNDA_APP_DIR:?required}:/config
      - ${SHUNDA_DATA_DIR:?required}/updater-state:/state
```

The real Compose retains all existing db/Web environment, health, dependency and data-volume entries around this exact image/updater boundary. `deploy-dsm.sh` calls `docker compose pull web updater`, then `docker compose up -d db updater`, `docker compose run --rm --no-deps web python manage.py migrate`, and finally `docker compose up -d --no-deps web`.

- [ ] **Step 5: Verify and commit**

Run: `pytest tests/test_deployment.py -q`

Run: `bash scripts/system-update-compose.test.sh`

Run: `docker build --target web --build-arg SHUNDA_RELEASE_VERSION=v0.2.0 --build-arg SHUNDA_RELEASE_REVISION=$(git rev-parse HEAD) --build-arg SHUNDA_RELEASE_CREATED=2026-08-06T00:00:00Z .`

Run: `docker build --target updater .`

```bash
git add Dockerfile .dockerignore compose.yml .env.example scripts/deploy-dsm.sh scripts/system-update-compose.test.sh tests/test_deployment.py
git diff --cached --check
git commit -m "feat: 发布并编排升级服务"
```

### Task 11: GitHub Actions 不可变镜像发布

**Files:**
- Create: `.github/workflows/release-images.yml`
- Create: `scripts/release-images-contract.test.sh`
- Create: `README.md`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces public release workflow for `ghcr.io/s450586793/shunda-finance-web` and `ghcr.io/s450586793/shunda-finance-updater`。

- [ ] **Step 1: Write failing workflow contract tests**

Parse workflow YAML/text and assert:

- triggers include pull requests, `main`, exact `v*` tags and manual dispatch;
- only `^v[0-9]+\.[0-9]+\.[0-9]+$` is a release;
- test job runs pytest coverage, Node tests, Playwright, Compose contract and release contract before publish;
- publish matrix contains exactly Web/updater immutable release tags;
- a preflight `imagetools inspect` causes release failure if either version tag already exists;
- Web/updater build args and OCI labels use the same tag SHA/time;
- stable promotion depends on the complete publish matrix and promotes Web only;
- updater never gets `latest` or `stable`.

- [ ] **Step 2: Run contract and verify RED**

Run: `bash scripts/release-images-contract.test.sh`

- [ ] **Step 3: Implement workflow and concise public README**

Use `actions/checkout@v4`, `actions/setup-python@v5`, `actions/setup-node@v4`, `docker/setup-buildx-action@v3`, `docker/login-action@v3`, `docker/metadata-action@v5`, and `docker/build-push-action@v6`. Set permissions to `contents: read`, `packages: write`. Add `PyYAML~=6.0` to the development dependency group so the contract can parse workflow YAML rather than relying only on substring checks. README documents public-source secret exclusions, immutable releases and DSM image-only deployment without production values.

```yaml
on:
  pull_request:
  push:
    branches: [main]
    tags: ["v*"]
  workflow_dispatch:

permissions:
  contents: read
  packages: write

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install ".[dev]"
      - run: pytest --cov --cov-report=term-missing
      - run: npm ci && npm run test:js
      - run: bash scripts/system-update-compose.test.sh
      - run: bash scripts/release-images-contract.test.sh
  publish:
    if: startsWith(github.ref, 'refs/tags/v')
    needs: test
    strategy:
      matrix:
        target: [web, updater]
```

The complete workflow adds Playwright setup/tests, canonical tag validation, existing-tag preflight, Buildx login/build/push and a separate `promote-stable` job with `needs: publish` that targets Web only.

- [ ] **Step 4: Verify and commit**

Run: `bash scripts/release-images-contract.test.sh`

Run: `python -c 'import yaml; yaml.safe_load(open(".github/workflows/release-images.yml"))'`

```bash
git add .github/workflows/release-images.yml scripts/release-images-contract.test.sh README.md pyproject.toml
git diff --cached --check
git commit -m "ci: 发布顺达升级镜像"
```

### Task 12: DSM 运维、成功 smoke、回滚 smoke 与全量验证

**Files:**
- Create: `scripts/system-update-dsm-smoke.sh`
- Create: `scripts/system-update-dsm-smoke.test.sh`
- Modify: `docs/deployment-dsm.md`
- Create: `docs/system-update-runbook.md`
- Modify: `docs/acceptance-results.md`

**Interfaces:**
- Produces guarded DSM acceptance commands requiring `SHUNDA_CONFIRM_SYSTEM_UPDATE=yes` and separate `SHUNDA_CONFIRM_ROLLBACK_SMOKE=yes`。
- Documents first image deployment, Owner bootstrap, manual updater update, cleanup pending and manual intervention。

- [ ] **Step 1: Write failing fake-command smoke tests**

With fake `curl`, `docker` and `jq`, assert the script:

- exits before login/Docker calls without explicit confirmation;
- requires base URL, Owner username/password and exact expected target;
- uses a mode-`0600` cookie jar and deletes it;
- obtains CSRF/login without printing credentials or raw response;
- records db/updater IDs, images, `StartedAt` and volume identities before Start;
- requires exact target after Check, starts once and polls with a 10-minute wall-clock deadline;
- verifies task-specific nonempty DB/upload backups;
- verifies target Web version and public health;
- proves db/updater IDs, images, StartedAt and volumes did not change;
- accepts cleanup complete or prints one fixed cleanup-pending instruction;
- rollback mode waits for `checking_health`, stops only the target Web container once, requires `failed + rolled_back`, proves old Web healthy and both old/target images retained;
- never prints password, Cookie, CSRF, Token, digest, image ID, alias, raw JSON or command errors.

- [ ] **Step 2: Run smoke contract and verify RED**

Run: `bash scripts/system-update-dsm-smoke.test.sh`

- [ ] **Step 3: Implement guarded smoke scripts**

Use `set -euo pipefail`, `mktemp`, trap cleanup, `curl --fail --silent --show-error --max-time`, `jq -e`, fixed routes and bounded polling. Derive the project root from the script path. The rollback mutation may stop only the exact Compose `web` container after the public task reaches `checking_health`; any identity drift aborts before mutation.

```bash
#!/usr/bin/env bash
set -euo pipefail

[[ "${SHUNDA_CONFIRM_SYSTEM_UPDATE:-}" == "yes" ]] || {
  printf 'error: explicit confirmation is required\n' >&2
  exit 2
}
[[ "${SHUNDA_EXPECTED_TARGET:-}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || exit 2
cookie_jar="$(mktemp)"
trap 'rm -f "$cookie_jar"' EXIT
chmod 600 "$cookie_jar"
fetch_public_status() {
  curl --fail --silent --show-error --max-time 5 \
    --cookie "$cookie_jar" \
    "${SHUNDA_BASE_URL}/system/update/status/"
}
deadline=$((SECONDS + 600))
while (( SECONDS < deadline )); do
  status="$(fetch_public_status)"
  stage="$(jq -er '.task.stage' <<<"$status")"
  [[ "$stage" == "succeeded" ]] && break
  [[ "$stage" == "failed" || "$stage" == "manual_intervention" ]] && exit 1
  sleep 2
done
```

- [ ] **Step 4: Write deployment and recovery runbooks**

Document exact commands for:

1. public GitHub/GHCR creation and anonymous pull verification;
2. first `v0.2.0` image deployment after independent DB/uploads/config backup;
3. `bootstrap_owner_user` with password via stdin;
4. later manual updater tag change rebuilding updater only;
5. Web page upgrade and task recovery;
6. root-scoped `0600` state inspection for cleanup pending with exact non-force deletion;
7. manual intervention preserving state and all images;
8. initial old local-build image cleanup only after no container references;
9. preserving external `sd.ace-station.top:1111` routing.

- [ ] **Step 5: Run all local gates**

Run: `pytest --cov --cov-report=term-missing`

Run: `npm run test:js`

Run: `npm run test:e2e`

Run: `bash scripts/system-update-compose.test.sh`

Run: `bash scripts/release-images-contract.test.sh`

Run: `bash scripts/system-update-dsm-smoke.test.sh`

Run: `python manage.py check --deploy --settings=config.settings.prod` with test-safe required environment values.

Expected: all tests pass and total Python branch coverage remains at least 80%.

- [ ] **Step 6: Run first DSM rollout after `v0.2.0` is published**

Before mutation, back up `.env`, Compose, database and uploads to a timestamped directory; capture current container/image/volume identities. Confirm both GHCR packages are anonymously readable, then deploy the three-service Compose with immutable `v0.2.0` tags. Verify Web/updater health, database migrations, Owner login, invoice list and import page without changing finance records.

- [ ] **Step 7: Publish and validate `v0.2.1` through Web**

Create a documentation-only release commit so `v0.2.1` has a distinct Git SHA, publish it, then run rollback smoke first and successful smoke second. Confirm the rollback run retains both images; the successful run leaves Web at `v0.2.1`, keeps db/updater unchanged and removes only the original Web image after exact checks.

- [ ] **Step 8: Record non-sensitive acceptance and commit**

Record versions, UTC time, service health, backup existence and passed checks only. Do not record image IDs/digests, paths, tokens, cookies, usernames or financial data.

```bash
git add scripts/system-update-dsm-smoke.sh scripts/system-update-dsm-smoke.test.sh docs/deployment-dsm.md docs/system-update-runbook.md docs/acceptance-results.md
git diff --cached --check
git commit -m "docs: 完善 DSM 升级验收"
```

## Final Review Gate

- [ ] Compare every requirement in `docs/superpowers/specs/2026-08-06-shunda-web-managed-upgrades-design.md` with Tasks 1-12 and identify its implementation/test owner.
- [ ] Run the repository unfinished-marker scan over every changed file and resolve every marker, placeholder secret and skipped test.
- [ ] Run `git status --short` and verify only intentional files remain; leave the pre-existing `.workflow/` untouched.
- [ ] Render production Compose and prove Docker socket appears exactly once under updater, updater has no host port, and Web upgrade overrides cannot include db/updater.
- [ ] Inspect all Django/updater JSON and logs for Token、Cookie、password、database URL、digest、image ID、alias、internal path、raw command error or stack leakage.
- [ ] Confirm release failure cannot move Web stable before both immutable Web/updater images exist, and updater has no mutable tag.
- [ ] Confirm DSM success smoke and controlled rollback smoke both pass before declaring the feature complete.
