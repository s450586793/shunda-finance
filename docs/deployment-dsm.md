# DSM 镜像部署指南

## 适用范围

本文只覆盖首次把 DSM 生产环境从旧的本地构建 `app` 项目切换到 3 服务镜像部署：

- `db`
- `web`
- `updater`

外部访问地址继续保持 `http://sd.ace-station.top:1111`。本项目不引入 HTTPS，也不修改 DSM 现有外部转发目标。

## 预设环境变量

所有命令都使用占位符环境变量，不把路径、口令或 Token 写入文档正文：

```bash
export SHUNDA_BASE_URL="http://sd.ace-station.top:1111"
export SHUNDA_APP_DIR="<app-dir>"
export SHUNDA_DATA_DIR="<data-dir>"
export SHUNDA_BACKUP_ROOT="<backup-root>"
export SHUNDA_WEB_IMAGE_TAG="v0.2.0"
export SHUNDA_UPDATER_IMAGE_TAG="v0.2.0"
export SHUNDA_UPDATER_TOKEN="<32-byte-random-token>"
export SHUNDA_DEPLOY_MODE="initial-migration"
```

`SHUNDA_WEB_IMAGE_TAG` 和 `SHUNDA_UPDATER_IMAGE_TAG` 必须使用规范 `vX.Y.Z`。`SHUNDA_UPDATER_TOKEN` 必须至少 32 字节，并只保存在 DSM 的 `.env` 中。

## 先验证公开镜像可匿名拉取

在执行首个镜像部署前，先验证两个公开镜像都已经发布，并且匿名环境也能拉取：

```bash
export SHUNDA_EMPTY_DOCKER_CONFIG="$(mktemp -d)"
DOCKER_CONFIG="$SHUNDA_EMPTY_DOCKER_CONFIG" docker pull "ghcr.io/s450586793/shunda-finance-web:${SHUNDA_WEB_IMAGE_TAG}"
DOCKER_CONFIG="$SHUNDA_EMPTY_DOCKER_CONFIG" docker pull "ghcr.io/s450586793/shunda-finance-updater:${SHUNDA_UPDATER_IMAGE_TAG}"
```

任一镜像拉取失败时，不要修改 DSM 运行中的项目，先回到 GitHub/GHCR 发布流程排查。

## 先做独立备份

首次切换前必须保留一份独立于 updater 的备份，至少包含：

- 当前 `.env`
- 当前 `compose.yml`
- 当前 PostgreSQL 备份
- 当前 uploads 备份

建议命令：

```bash
export SHUNDA_BACKUP_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
export SHUNDA_BACKUP_DIR="${SHUNDA_BACKUP_ROOT}/${SHUNDA_BACKUP_STAMP}"
export SHUNDA_BACKUP_MANIFEST="${SHUNDA_BACKUP_DIR}/backup-manifest.env"
mkdir -p "$SHUNDA_BACKUP_DIR"
cp "${SHUNDA_APP_DIR}/.env" "${SHUNDA_BACKUP_DIR}/.env"
cp "${SHUNDA_APP_DIR}/compose.yml" "${SHUNDA_BACKUP_DIR}/compose.yml"
docker compose \
  --project-name app \
  --env-file "${SHUNDA_APP_DIR}/.env" \
  -f "${SHUNDA_APP_DIR}/compose.yml" \
  exec -T web /app/scripts/backup.sh > "${SHUNDA_BACKUP_MANIFEST}"
eval "$(
  python3 - "${SHUNDA_BACKUP_MANIFEST}" <<'PY'
import re
import shlex
import sys
from pathlib import Path

payload = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
values = {}
for line in payload:
    key, _, value = line.partition("=")
    if key in values or not value:
        raise SystemExit("invalid backup manifest")
    values[key] = value
if set(values) != {"DB_BACKUP", "UPLOADS_BACKUP"}:
    raise SystemExit("invalid backup manifest keys")
db_path = values["DB_BACKUP"]
uploads_path = values["UPLOADS_BACKUP"]
db_match = re.fullmatch(r"/data/backups/db-[0-9]{8}-[0-9]{6}\.dump", db_path)
uploads_match = re.fullmatch(r"/data/backups/uploads-[0-9]{8}-[0-9]{6}\.tar\.gz", uploads_path)
if db_match is None or uploads_match is None:
    raise SystemExit("invalid backup paths")
print(f'export SHUNDA_DB_BACKUP_PATH={shlex.quote(db_path)}')
print(f'export SHUNDA_UPLOADS_BACKUP_PATH={shlex.quote(uploads_path)}')
PY
)"
docker compose \
  --project-name app \
  --env-file "${SHUNDA_APP_DIR}/.env" \
  -f "${SHUNDA_APP_DIR}/compose.yml" \
  exec -T web test -s "${SHUNDA_DB_BACKUP_PATH}"
docker compose \
  --project-name app \
  --env-file "${SHUNDA_APP_DIR}/.env" \
  -f "${SHUNDA_APP_DIR}/compose.yml" \
  exec -T web test -s "${SHUNDA_UPLOADS_BACKUP_PATH}"
export SHUNDA_LEGACY_WEB_CONTAINER="$(docker compose \
  --project-name app \
  --env-file "${SHUNDA_APP_DIR}/.env" \
  -f "${SHUNDA_APP_DIR}/compose.yml" \
  ps -q web)"
docker cp "${SHUNDA_LEGACY_WEB_CONTAINER}:${SHUNDA_DB_BACKUP_PATH}" "${SHUNDA_BACKUP_DIR}/db.dump"
docker cp "${SHUNDA_LEGACY_WEB_CONTAINER}:${SHUNDA_UPLOADS_BACKUP_PATH}" "${SHUNDA_BACKUP_DIR}/uploads.tar.gz"
printf 'db_backup_verified=true\nuploads_backup_verified=true\n' > "${SHUNDA_BACKUP_DIR}/validation.txt"
```

要求：

- 旧 `app` 项目的 PostgreSQL 只能保留这一份生产实例，切换前必须确认不存在第二个数据库容器或第二份数据目录。
- 切换完成前不要删除旧的本地构建 Web 镜像。
- `validation.txt` 只记录非敏感校验结果；不要把容器内部备份路径、真实用户名、Token 或业务数据抄入人工记录。

## 首次镜像部署

确认公开镜像和独立备份都完成后，使用仓库内脚本执行首次切换：

```bash
env \
  SHUNDA_APP_DIR="$SHUNDA_APP_DIR" \
  SHUNDA_DATA_DIR="$SHUNDA_DATA_DIR" \
  SHUNDA_WEB_IMAGE_TAG="$SHUNDA_WEB_IMAGE_TAG" \
  SHUNDA_UPDATER_IMAGE_TAG="$SHUNDA_UPDATER_IMAGE_TAG" \
  SHUNDA_UPDATER_TOKEN="$SHUNDA_UPDATER_TOKEN" \
  SHUNDA_DEPLOY_MODE="$SHUNDA_DEPLOY_MODE" \
  bash scripts/deploy-dsm.sh
```

`scripts/deploy-dsm.sh` 的首次切换模式会：

- 停掉旧 `app` 项目的 `db` 和 `web`
- 启动目标 `shunda-finance` 项目的 `db`、`web`、`updater`
- 复用现有 PostgreSQL、uploads、exports、backups 数据
- 执行迁移并等待 `web`、`updater` 健康

如果脚本在停止旧项目之后失败，不要手动清理镜像；先按 [system-update-runbook.md](system-update-runbook.md) 的恢复章节回退。

## 首次 Owner 启动

首次镜像切换完成后，使用标准输入初始化 Owner 账户：

```bash
export BOOTSTRAP_OWNER_USERNAME="<owner-username>"
read -rsp "Owner password: " SHUNDA_OWNER_PASSWORD && printf '\n'
printf '%s\n' "$SHUNDA_OWNER_PASSWORD" | docker compose \
  --project-name shunda-finance \
  --env-file "${SHUNDA_APP_DIR}/.env" \
  -f "${SHUNDA_APP_DIR}/compose.yml" \
  exec -T \
  -e BOOTSTRAP_OWNER_USERNAME="${BOOTSTRAP_OWNER_USERNAME}" \
  web python manage.py bootstrap_owner_user \
    --username "${BOOTSTRAP_OWNER_USERNAME}" \
    --password-stdin
unset SHUNDA_OWNER_PASSWORD
```

该命令必须通过 stdin 传递口令；不要把密码写进命令参数、`.env`、文档或 shell 历史。

## 首次切换后的最小验证

完成 `v0.2.0` 首次镜像部署后，至少验证：

- `http://sd.ace-station.top:1111/accounts/login/` 可访问
- Owner 能登录并打开 `http://sd.ace-station.top:1111/system/update/`
- 导入中心、发票台账和健康检查页面仍可读取
- 旧本地构建 Web 镜像仍被保留，直到 DSM smoke 全部完成

真实 DSM smoke、cleanup pending、manual intervention、后续 Web 升级与 updater 受控换 tag 流程见 [system-update-runbook.md](system-update-runbook.md)。
