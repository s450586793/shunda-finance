# Task 12 Acceptance Results

验收日期：2026-08-07（Asia/Shanghai）
状态：`PENDING_EXTERNAL_VALIDATION`

## 本地 gate

以下本地 gate 已通过：

- `pytest --cov --cov-report=term-missing`
  - `886 passed`
  - `Total coverage: 91.04%`
- `npm run test:js`
  - `17 passed`
- `npm run test:e2e`
  - `5 passed`
- `bash scripts/system-update-compose.test.sh`
  - passed
- `bash scripts/release-images-contract.test.sh`
  - passed
- `bash scripts/system-update-dsm-smoke.test.sh`
  - passed
- `python manage.py check --deploy --settings=config.settings.prod`
  - exit `0`
  - remaining warnings: `security.W004`, `security.W008`
  - these warnings are expected for the current HTTP-only deployment boundary and do not indicate a local command failure

## Task 12 Fix Round 1 复核

本轮仅复核 Critical / Important 修复，不处理已知 Minor 项：

- `bash scripts/system-update-dsm-smoke.test.sh`
  - passed
- `bash -n scripts/system-update-dsm-smoke.sh scripts/system-update-dsm-smoke.test.sh`
  - passed
- `bash scripts/system-update-compose.test.sh`
  - passed
- `bash scripts/release-images-contract.test.sh`
  - passed

## Task 12 Fix Round 2 复核

本轮继续只复核 Critical / Important 修复，不处理已知 Minor 项：

- `bash scripts/system-update-dsm-smoke.test.sh`
  - passed
- `bash -n scripts/system-update-dsm-smoke.sh scripts/system-update-dsm-smoke.test.sh`
  - passed
- `bash scripts/system-update-compose.test.sh`
  - passed
- `bash scripts/release-images-contract.test.sh`
  - passed

## Task 12 Fix Round 3 复核

本轮继续只复核 Critical / Important 修复，不处理已知 Minor 项：

- `env PATH="<worktree>/.venv/bin:$PATH" pytest tests/updater/test_manual_cleanup.py -q`
  - `13 passed`
- `bash scripts/system-update-dsm-smoke.test.sh`
  - passed
- `bash -n scripts/system-update-dsm-smoke.sh scripts/system-update-dsm-smoke.test.sh`
  - passed
- `bash scripts/system-update-compose.test.sh`
  - passed
- `bash scripts/release-images-contract.test.sh`
  - passed

## Task 12 Fix Round 4 复核

本轮修复 root-only cleanup orchestration、updater ENTRYPOINT 覆盖和 CLI 导入异常边界：

- `env PATH="<worktree>/.venv/bin:$PATH" pytest tests/updater/test_manual_cleanup.py -q`
  - `16 passed`
- `env PATH="<worktree>/.venv/bin:$PATH" pytest tests/updater -q`
  - `272 passed`
- `bash scripts/system-update-manual-cleanup.test.sh`
  - passed
- `bash scripts/system-update-dsm-smoke.test.sh`
  - passed
- `bash -n scripts/system-update-manual-cleanup.sh scripts/system-update-manual-cleanup.test.sh scripts/system-update-dsm-smoke.sh scripts/system-update-dsm-smoke.test.sh`
  - passed
- `bash scripts/system-update-compose.test.sh`
  - passed
- `bash scripts/release-images-contract.test.sh`
  - passed
- `ruff check updater/manual_cleanup.py tests/updater/test_manual_cleanup.py`
  - passed
- `git diff --check`
  - passed

## Task 12 Fix Round 5 复核

本轮收紧 root authorization 与 Docker daemon endpoint 边界：

- `bash scripts/system-update-manual-cleanup.test.sh`
  - passed
  - success / orchestration 场景通过 `sudo -n` 以真实 EUID 0 执行
  - fake `id` 返回 0 时，非 root 直接执行仍在 Docker 前失败
  - 非空 `DOCKER_HOST`、`DOCKER_CONTEXT`、`DOCKER_TLS_VERIFY`、`DOCKER_CERT_PATH` 均在 Docker 前失败
  - 所有允许的 Docker 调用均固定使用 `--host unix:///var/run/docker.sock`
- `bash -n scripts/system-update-manual-cleanup.sh scripts/system-update-manual-cleanup.test.sh`
  - passed
- `bash scripts/system-update-dsm-smoke.test.sh`
  - passed
- `bash scripts/system-update-compose.test.sh`
  - passed
- `bash scripts/release-images-contract.test.sh`
  - passed
- `git diff --check`
  - passed

## 非敏感结论

本提交只能确认以下事实：

- DSM smoke 脚本已具备本地 fake-command contract，覆盖 success、rollback、cleanup pending、双确认、10 分钟 deadline、单次 Start、单次 rollback mutation、备份存在性与 db/updater identity 不变约束。
- root-only cleanup 脚本已具备本地 fake-command contract，覆盖 readonly `EUID` 授权、Docker endpoint 环境拒绝、固定本地 Unix socket、确认前无 Docker、固定 Compose target、显式 Python ENTRYPOINT、updater-only restart/health、失败状态保留和 raw error/xtrace 脱敏。
- 首次镜像部署、Owner stdin bootstrap、updater 手工换 tag、Web UI 升级/恢复、cleanup pending、manual intervention、旧本地镜像清理流程已文档化。
- 本地 fake contracts 未写入真实 DSM 凭据、Owner 口令、Token、Cookie、CSRF、digest 或 image ID。是否满足 public-source privacy gate，必须以 parentless snapshot、外部 anchor inventory、fresh clone reachable-object、source archive、build context 与 image layer 扫描的实际结果为准；本文件不提前声明 clean。

## 仍待外部验证

以下步骤未在本机执行，必须在有授权且连接真实 DSM/GitHub/GHCR 的外部环境继续验证：

- 创建或连接公开 GitHub 仓库 `s450586793/shunda-finance`
- 发布真实 `v0.2.0` 与 `v0.2.1`
- 验证 GHCR 匿名拉取
- 执行 DSM 首次镜像切换
- 执行真实 rollback smoke
- 执行真实 success smoke
- 验证旧本地构建 Web 镜像的最终清理

在这些外部步骤完成前，本文件不声明真实 rollout 成功。
