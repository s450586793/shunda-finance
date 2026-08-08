# System Update Runbook

## 适用范围

本文覆盖：

- 公开 GitHub 与 GHCR 发布前置检查
- 首次 `v0.2.0` 之后的 DSM success smoke
- 受控 rollback smoke
- updater 固定 tag 的受控更新
- Web UI 升级与失败恢复
- `cleanup pending` 与 `manual intervention`
- 旧本地构建 Web 镜像的最终清理

本文不包含真实口令、Token、Cookie、CSRF、digest、image ID 或内部 acceptance 路径。

## 1. 公开发布与匿名拉取验证

真实 GitHub 推送和 GHCR 发布只能在具备授权的环境完成。首次公开前，先在仓库外准备审查批准的敏感 anchor 清单，每行一个原始值，权限必须为 `0600`。当前工作树的 tracked 文件必须完全干净。

```bash
export SHUNDA_SOURCE_ROOT="$(git rev-parse --show-toplevel)"
export SHUNDA_PRIVATE_HISTORY_ANCHOR="eca17efcbb6a1c8137e3329fceac1c91ee7201c7"
export SHUNDA_PUBLIC_SENSITIVE_ANCHORS_FILE="<external-reviewed-anchor-file>"
test "$(stat -c '%a' "$SHUNDA_PUBLIC_SENSITIVE_ANCHORS_FILE")" = "600"
test -z "$(git -C "$SHUNDA_SOURCE_ROOT" status --short --untracked-files=no)"

export SHUNDA_PUBLIC_SNAPSHOT_DIR="$(mktemp -d)"
SHUNDA_PUBLIC_SENSITIVE_ANCHORS_FILE="$SHUNDA_PUBLIC_SENSITIVE_ANCHORS_FILE" \
  bash "$SHUNDA_SOURCE_ROOT/scripts/create-public-snapshot.sh" \
  "$SHUNDA_PUBLIC_SNAPSHOT_DIR"

test "$(git -C "$SHUNDA_PUBLIC_SNAPSHOT_DIR" branch --show-current)" = "main"
test "$(git -C "$SHUNDA_PUBLIC_SNAPSHOT_DIR" rev-list --count main)" = "1"
test "$(git -C "$SHUNDA_PUBLIC_SNAPSHOT_DIR" rev-list --parents -n 1 main | awk '{print NF}')" = "1"
```

在独立临时仓库中同时载入 private history anchor 与 clean root，证明旧 head 不是新 `main` 的祖先。返回值必须精确为 `1`，其他返回值都失败：

```bash
export SHUNDA_ANCESTRY_CHECK_DIR="$(mktemp -d)"
git -C "$SHUNDA_ANCESTRY_CHECK_DIR" init -q
git -C "$SHUNDA_ANCESTRY_CHECK_DIR" fetch -q --no-tags \
  "$SHUNDA_SOURCE_ROOT" \
  "$SHUNDA_PRIVATE_HISTORY_ANCHOR:refs/audit/private-history"
git -C "$SHUNDA_ANCESTRY_CHECK_DIR" fetch -q --no-tags \
  "$SHUNDA_PUBLIC_SNAPSHOT_DIR" \
  "main:refs/audit/public-main"
set +e
git -C "$SHUNDA_ANCESTRY_CHECK_DIR" merge-base --is-ancestor \
  refs/audit/private-history refs/audit/public-main
ancestry_status=$?
set -e
test "$ancestry_status" -eq 1
rm -rf "$SHUNDA_ANCESTRY_CHECK_DIR"
```

先在 GitHub UI 或受控 API 中创建 public 空仓库。远端存在任何 branch 或 tag 时，首次 publication 必须停止。只允许从 snapshot 仓库推送一个 clean-root ref：

```bash
export SHUNDA_PUBLIC_REMOTE="https://github.com/s450586793/shunda-finance.git"
test -z "$(git ls-remote --heads --tags "$SHUNDA_PUBLIC_REMOTE")"
git -C "$SHUNDA_PUBLIC_SNAPSHOT_DIR" remote add origin "$SHUNDA_PUBLIC_REMOTE"
git -C "$SHUNDA_PUBLIC_SNAPSHOT_DIR" push --set-upstream \
  origin main:refs/heads/main
```

创建任何 tag 前，必须从未登录环境 fresh clone，并逐一扫描所有 reachable commit 的 tracked path、文件内容、XLS/XLSX/PDF 和 source archive。随后构建两个本地镜像并扫描 `docker save` 输出；任一命令失败都不得打 tag：

```bash
export SHUNDA_PUBLIC_VERIFY_DIR="$(mktemp -d)"
git -c credential.helper= clone --no-tags "$SHUNDA_PUBLIC_REMOTE" \
  "$SHUNDA_PUBLIC_VERIFY_DIR"
test "$(git -C "$SHUNDA_PUBLIC_VERIFY_DIR" rev-list --count --all)" = "1"
test "$(git -C "$SHUNDA_PUBLIC_VERIFY_DIR" rev-list --parents -n 1 main | awk '{print NF}')" = "1"
git -C "$SHUNDA_PUBLIC_VERIFY_DIR" rev-list --objects --all >/dev/null

for commit in $(git -C "$SHUNDA_PUBLIC_VERIFY_DIR" rev-list --all); do
  commit_tree="$(mktemp -d)"
  git -C "$SHUNDA_PUBLIC_VERIFY_DIR" archive --format=tar "$commit" | \
    tar -xf - -C "$commit_tree"
  python3 "$SHUNDA_PUBLIC_VERIFY_DIR/scripts/scan-public-tree.py" \
    "$commit_tree" "$SHUNDA_PUBLIC_SENSITIVE_ANCHORS_FILE"
  rm -rf "$commit_tree"
done

release_revision="$(git -C "$SHUNDA_PUBLIC_VERIFY_DIR" rev-parse main)"
release_created="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
docker buildx build --load --target web \
  --build-arg SHUNDA_RELEASE_VERSION=v0.2.0 \
  --build-arg SHUNDA_RELEASE_REVISION="$release_revision" \
  --build-arg SHUNDA_RELEASE_CREATED="$release_created" \
  -t shunda-public-scan-web:v0.2.0 "$SHUNDA_PUBLIC_VERIFY_DIR"
docker buildx build --load --target updater \
  -t shunda-public-scan-updater:v0.2.0 "$SHUNDA_PUBLIC_VERIFY_DIR"
image_scan_dir="$(mktemp -d)"
docker save --output "$image_scan_dir/images.tar" \
  shunda-public-scan-web:v0.2.0 shunda-public-scan-updater:v0.2.0
python3 "$SHUNDA_PUBLIC_VERIFY_DIR/scripts/scan-public-tree.py" \
  "$image_scan_dir" "$SHUNDA_PUBLIC_SENSITIVE_ANCHORS_FILE"
rm -rf "$image_scan_dir"
```

以上验证全部通过后，才从 clean snapshot 的 `main` 创建首个 release tag，并只推该 tag：

```bash
git -C "$SHUNDA_PUBLIC_SNAPSHOT_DIR" tag v0.2.0 main
git -C "$SHUNDA_PUBLIC_SNAPSHOT_DIR" push \
  origin refs/tags/v0.2.0:refs/tags/v0.2.0

export SHUNDA_EMPTY_DOCKER_CONFIG="$(mktemp -d)"
DOCKER_CONFIG="$SHUNDA_EMPTY_DOCKER_CONFIG" docker pull ghcr.io/s450586793/shunda-finance-web:v0.2.0
DOCKER_CONFIG="$SHUNDA_EMPTY_DOCKER_CONFIG" docker pull ghcr.io/s450586793/shunda-finance-updater:v0.2.0
```

要求：

- 禁止 push 当前工作分支、`SHUNDA_PRIVATE_HISTORY_ANCHOR` 的任何 ancestor、旧本地 tag、remote-tracking ref、notes 或 replacement refs。
- 禁止 `--all`、`--mirror`，也禁止从原工作树向 public remote 推送任何 ref。
- 后续 release commit 与 tag 只能在 public `main` 的 clean-root ancestry 上创建，并重复 fresh clone、reachable-object、archive、build context 与 image 扫描。
- `git ls-remote` 必须在未登录浏览器和未注入 GitHub credential 的环境里可读。
- 匿名 GHCR pull 必须使用独立空目录 `DOCKER_CONFIG`，不能复用已有登录态。
- 如果任一验证失败，不要继续 DSM 变更。

## 2. Web UI 升级前准备

```bash
export SHUNDA_BASE_URL="http://sd.ace-station.top:1111"
export SHUNDA_APP_DIR="<app-dir>"
export SHUNDA_DATA_DIR="<data-dir>"
export SHUNDA_EXPECTED_TARGET="v0.2.1"
export SHUNDA_OWNER_USERNAME="<owner-username>"
read -rsp "Owner password: " SHUNDA_OWNER_PASSWORD && printf '\n'
```

升级前检查：

- 当前 `db`、`web`、`updater` 都来自 `shunda-finance` 项目
- `db`、uploads、exports、backups 仍是原有生产数据
- 外部地址仍为 `http://sd.ace-station.top:1111`
- updater 当前固定 tag 不是可变 tag

## 3. 受控 rollback smoke

先验证失败 release 会自动回滚，且只停止目标 `web` 容器：

```bash
env \
  SHUNDA_BASE_URL="$SHUNDA_BASE_URL" \
  SHUNDA_APP_DIR="$SHUNDA_APP_DIR" \
  SHUNDA_OWNER_USERNAME="$SHUNDA_OWNER_USERNAME" \
  SHUNDA_OWNER_PASSWORD="$SHUNDA_OWNER_PASSWORD" \
  SHUNDA_EXPECTED_TARGET="$SHUNDA_EXPECTED_TARGET" \
  SHUNDA_SMOKE_MODE="rollback" \
  SHUNDA_CONFIRM_SYSTEM_UPDATE="yes" \
  SHUNDA_CONFIRM_ROLLBACK_SMOKE="yes" \
  bash scripts/system-update-dsm-smoke.sh
```

预期结果：

- 页面任务进入 `checking_health` 后，脚本只会精确停止一次目标 `web` 容器
- 终态为 `failed` 且 `rolled_back=true`
- 外部 `http://sd.ace-station.top:1111/health/` 恢复为旧 Web 健康
- 旧 Web 版本镜像和目标版本镜像都被保留
- `db` 与 `updater` 容器 identity、`StartedAt`、volume 不变

如果该 smoke 未通过，不要继续 success smoke。

## 4. 受控 success smoke

rollback smoke 通过后，再验证真实升级成功流程：

```bash
env \
  SHUNDA_BASE_URL="$SHUNDA_BASE_URL" \
  SHUNDA_APP_DIR="$SHUNDA_APP_DIR" \
  SHUNDA_OWNER_USERNAME="$SHUNDA_OWNER_USERNAME" \
  SHUNDA_OWNER_PASSWORD="$SHUNDA_OWNER_PASSWORD" \
  SHUNDA_EXPECTED_TARGET="$SHUNDA_EXPECTED_TARGET" \
  SHUNDA_CONFIRM_SYSTEM_UPDATE="yes" \
  bash scripts/system-update-dsm-smoke.sh
```

预期结果：

- `/system/update/check/` 返回的 `latest_version` 与 `SHUNDA_EXPECTED_TARGET` 完全一致
- `/system/update/start/` 只触发一次
- 10 分钟内到达 `succeeded`
- 任务级数据库备份和 uploads 备份都非空
- `current_version` 变为目标版本
- `db` 与 `updater` 的 container ID、image、`StartedAt`、volume identity 不变
- cleanup 为 `complete`，或仅输出一条固定的 `cleanup pending` 指令

## 5. Web UI 升级与任务恢复

Owner 在浏览器执行正常升级时：

1. 打开 `http://sd.ace-station.top:1111/system/update/`
2. 点击“检查更新”
3. 确认页面显示的目标版本与计划发布版本完全一致
4. 在确认对话框中只确认该精确版本
5. 等待终态

如果页面因短暂 Web 重启而刷新失败：

1. 不要重复点击“开始升级”
2. 重新打开 `http://sd.ace-station.top:1111/system/update/`
3. 继续观察同一 task 的状态
4. 若状态已终止，再按页面提示处理 `cleanup pending` 或 `manual intervention`

## 6. updater 固定 tag 的受控更新

updater 不通过 Web 页面自更新。新的 updater release 已发布并可匿名拉取后，由 root 在仓库目录中调用受控脚本：

```bash
export SHUNDA_NEW_UPDATER_TAG="v0.2.1"
SHUNDA_UPDATER_SCRIPT="$(pwd -P)/scripts/system-update-updater.sh"
/usr/bin/sudo -n /usr/bin/env -i \
  SHUNDA_CONFIRM_UPDATER_UPDATE=yes \
  SHUNDA_UPDATER_IMAGE_TAG="$SHUNDA_NEW_UPDATER_TAG" \
  /bin/sh "$SHUNDA_UPDATER_SCRIPT"
```

执行前必须确认：

- 新 tag 精确使用规范 `vMAJOR.MINOR.PATCH`，且不是当前 updater tag
- 固定部署目录和 `compose.yml` 仍由 root 控制，`.env` 是 root-owned、mode `0600` 的普通文件而非 symlink
- 未设置任何 Docker/Compose endpoint、TLS、plugin 或 config 环境覆盖，包括 `DOCKER_HOST`、`DOCKER_CONTEXT`、`DOCKER_TLS`、`DOCKER_TLS_VERIFY`、`DOCKER_CERT_PATH`、`DOCKER_CONFIG`、`DOCKER_CLI_PLUGIN_EXTRA_DIRS` 和 `COMPOSE_FILE`
- `/bin/sh`、Python、Docker、coreutils 和 Compose plugin 仍来自 root 控制且 group/other 不可写的 system path；不要改回 PATH lookup 或用户 HOME 下的 plugin
- 当前 updater 健康，且 `db`、`web` 没有待处理的重启或重建

脚本先拉取并检查目标镜像，再私下记录 `db`、`web` 和旧 updater identity。只有这些检查全部通过后，才会用 same-directory、mode `0600`、file `fsync`、atomic replace 和 parent-directory `fsync` 更新 `.env` 中唯一的 `SHUNDA_UPDATER_IMAGE_TAG`；其他原始字节保持不变。随后只执行 `up -d --no-deps updater`，并证明新 updater 的 Compose project、service、tag、image identity 和 health 正确，且 `db`、`web` fingerprint 未变化。脚本不会删除旧 updater 镜像。

脚本在 private scratch 中创建 root-owned、mode `0700` 的空 HOME、Docker config 和 XDG 目录，并只允许经过验证的 system Compose plugin directory。成功时只输出 `updater update completed`。任何 mutation 后失败或收到受控 signal 时，脚本会原子恢复 `.env` 的原始字节和 mode，只重建旧 updater，并重新证明旧 image 健康及 `db`、`web` 未变化。classifier 证明原文件未变，或完整恢复证明成功后，仍只有 trusted cleanup 返回成功并证明 private scratch path 已不存在，才算清理完成且不保留现场。classifier、恢复证明或 intended private cleanup 任一失败都会 fail closed，只输出 `updater update requires manual intervention`；cleanup 删除失败还可能保留剩余的 root-only、mode `0700/0600` evidence 和 `cleanup-failed` marker，不得视为更新已完成。如果恢复证明也失败，停止新的升级操作并按第 8 节处理，不要把 `.env`、container/image identity、private evidence path 或原始 Docker 错误复制到公开记录。

## 7. cleanup pending

脚本或页面出现 `cleanup pending` 时，只允许 root 在项目仓库中调用受控脚本：

```bash
sudo env SHUNDA_CONFIRM_MANUAL_CLEANUP=yes \
  bash scripts/system-update-manual-cleanup.sh
```

不要直接拼接或逐条执行 `docker compose` cleanup 命令。受控脚本不接受浏览器或用户提供的 Docker target，并固定使用：

- Compose project：`shunda-finance`
- app path：`/volume4/docker/docker/shunda-finance/app`
- env path：`/volume4/docker/docker/shunda-finance/app/.env`
- compose path：`/volume4/docker/docker/shunda-finance/app/compose.yml`

脚本会按固定顺序执行以下流程：

1. 要求当前 effective user 为 root，且 `SHUNDA_CONFIRM_MANUAL_CLEANUP` 必须精确等于 `yes`；任一前置条件不满足时不会调用 Docker
2. 只停止 `updater`，再正向确认该 service 已停止；`db` 与 `web` 始终保持不变
3. 通过 `docker compose run --rm --no-deps --entrypoint python3 updater -m updater.manual_cleanup` 执行一次精确 cleanup，显式覆盖 updater 镜像的默认 ENTRYPOINT
4. 无论 stop、验证或 cleanup 成功还是失败，都会尝试只重启 `updater`，并正向等待其 health 恢复为 `healthy`
5. cleanup 的非零结果不会被成功的 restart/health 掩盖；restart 或 health 失败也会返回非零

root-only cleanup 必须同时满足以下 identity 约束；任一项无法被正向证明时，都只能转入人工处理：

- 记录中的 `original.repository`、`target.repository` 都固定为 `ghcr.io/s450586793/shunda-finance-web`
- task ID 必须是合法 UUID，且 `original.rollback_alias` 必须精确等于 `shunda-finance-rollback-web:<task-id>`
- `original.version`、`target.version` 都必须是规范 `vX.Y.Z`
- `original.digest`、`target.digest`、`original.image_id`、`target.image_id` 都必须是规范 `sha256:...`
- `original.tags` 只能是 `ghcr.io/s450586793/shunda-finance-web:<original.version>`
- `target.tags` 只能是 `ghcr.io/s450586793/shunda-finance-web:<target.version>`
- `original` 与 `target` 的 version、digest、image ID 必须彼此不同
- `/config/.env` 中的 `SHUNDA_WEB_IMAGE_TAG` 必须仍然精确等于 `target.version`
- `docker image inspect` 必须证明 `original.image_id` 仍然是记录中的同一 identity，且当前全部 tags 只允许出现在 `original.tags + rollback_alias`
- 删除前后都必须证明 `ancestor=<original.image_id>` 没有任何 container 引用
- 只有当记录 tag、rollback alias 和最终裸 image ID 都被逐项证明仍指向同一 `original.image_id` 时，才允许依次执行非强制删除：tag -> alias -> image ID
- 如果记录 tag 缺失，必须额外用 `docker image inspect --format '{{json .Id}}' <tag>` 正向证明它现在指向别的 image ID，而不是默认放行

禁止：

- `docker image prune`
- 模糊匹配名称
- 删除 `db`、`updater` 镜像
- `docker image rm --force`

如果脚本以非零状态退出，stderr 只会返回 `cleanup requires manual intervention`。此时即使 updater 已成功重启并恢复健康，也必须保留当前状态文件、旧/新 Web 镜像和容器现场，转入人工处理；不要打印、复制或粘贴任何 identity、traceback 或原始命令报错到工单、聊天或 acceptance 记录。

## 8. manual intervention

如果任务进入 `manual_intervention`：

- 停止发起新升级
- 保留 `.env`、`compose.yml`、updater 状态文件、任务备份、旧/新 Web 镜像
- 不要删除镜像，不要修改数据库，不要清空状态目录
- 只记录 task ID、阶段、版本、时间和安全错误码

建议先执行：

```bash
docker compose \
  --project-name shunda-finance \
  --env-file "${SHUNDA_APP_DIR}/.env" \
  -f "${SHUNDA_APP_DIR}/compose.yml" \
  ps

curl --fail --silent --show-error --max-time 5 "${SHUNDA_BASE_URL}/health/"
```

若 Web 已恢复旧版本且健康，保留现场并进入人工排查；若外部健康仍失败，优先使用独立备份恢复，而不是继续尝试新 release。

## 9. 初次旧本地构建 Web 镜像的最终清理

只有在以下条件同时满足后，才允许清理首次切换前的旧本地构建 Web 镜像：

- `v0.2.0` 首次镜像部署已完成
- rollback smoke 与 success smoke 都已通过
- `docker ps -a --format '{{.Image}}'` 确认没有容器再引用旧本地镜像

示例：

```bash
export LEGACY_LOCAL_WEB_IMAGE_REF="<legacy-local-web-image-ref>"
docker ps -a --format '{{.Image}}' | grep -F "$LEGACY_LOCAL_WEB_IMAGE_REF" && {
  printf 'legacy local image is still referenced; keep it\n' >&2
  exit 1
}
docker image rm "$LEGACY_LOCAL_WEB_IMAGE_REF"
```

这个步骤和 `cleanup pending` 不同；它只针对首次切换前保留下来的旧本地构建镜像。
