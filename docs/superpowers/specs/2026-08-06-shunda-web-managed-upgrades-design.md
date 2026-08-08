# 顺达财务系统 Web 手动升级设计

## 背景

顺达财务系统当前在 DSM 上通过单个 Compose 项目运行 PostgreSQL 和 Django Web，更新依赖在宿主机检出源码并执行本地构建。目标是把源码发布到公开 GitHub，由 GitHub Actions 构建公开 GHCR 镜像，并让老板可以在系统内手动完成 Web 升级。升级必须先备份，失败自动恢复旧 Web 镜像，成功后只删除本次被替换的旧镜像。

现有外部访问地址 `http://sd.ace-station.top:1111` 保持不变。允许升级期间出现约 10-60 秒的短暂停机。

## 目标

- 公开发布源码和可部署镜像，DSM 只拉取镜像，不再本地编译。
- 提供仅老板角色可见、可操作的版本检查和手动升级页面。
- 升级前完整备份 PostgreSQL 和上传附件。
- 只升级 `web`，不替换 PostgreSQL 或 updater。
- 新版本不可用时自动恢复旧 Web 镜像。
- 成功后精准删除本次旧 Web 镜像，不影响其他项目或镜像。
- 浏览器、普通日志和审计记录不泄露内部凭据或 Docker 身份。

## 非目标

- 不提供自动升级或定时升级。
- 不追求零停机或双 Web 实例切流。
- 不通过 Web 页面升级 updater、PostgreSQL 或 DSM 本身。
- 不自动反向执行数据库迁移或自动恢复生产数据库。
- 不提供任意 Docker 命令、镜像名、Compose 路径或宿主文件路径输入。

## 发布架构

源码仓库使用公开 GitHub `s450586793/shunda-finance`。公开仓库和镜像层不得包含 `.env`、生产数据、附件、备份、Token、Cookie、私钥或账号口令。

规范的 `vX.Y.Z` tag 触发 GitHub Actions：

1. 运行 Python、JavaScript、Compose 和发布契约测试。
2. 构建 `ghcr.io/s450586793/shunda-finance-web:vX.Y.Z`。
3. 构建 `ghcr.io/s450586793/shunda-finance-updater:vX.Y.Z`。
4. 两个不可变镜像都发布成功后，才把 Web 的 `stable` 移动到该版本。

不可变版本标签不得覆盖。updater 不发布或使用 `stable`；DSM 始终通过 `SHUNDA_UPDATER_IMAGE_TAG=vX.Y.Z` 固定其版本，并由运维人员手动更新。

Web 镜像写入以下 OCI 标签，并同时把版本作为只读运行环境变量提供给 Django：

- `org.opencontainers.image.version=vX.Y.Z`
- `org.opencontainers.image.revision=<git-sha>`
- `org.opencontainers.image.created=<UTC-RFC3339>`

## DSM Compose 边界

Compose 项目包含 3 个服务：

- `db`：PostgreSQL 16，保持现有数据卷和健康检查。
- `web`：Django/Gunicorn，保留现有上传、导出、备份卷和对外转发关系。
- `updater`：独立升级执行器，仅在 Compose 内部网络监听。

只有 updater 可以挂载：

- `/var/run/docker.sock`；
- 生产 `compose.yml`；
- 生产 `.env`；
- 持久化升级状态目录。

Web 不挂 Docker socket。updater 不发布宿主机端口，不读取业务数据库，不访问上传内容。Django 仅通过内部网络和至少 32 个随机字节的 `SHUNDA_UPDATER_TOKEN` 调用 updater。该 Token 只保存在 DSM 权限为 `0600` 的 `.env` 中。

Web 使用公开 GHCR 镜像和不可变 `SHUNDA_WEB_IMAGE_TAG`。updater 在目标版本完成健康和稳定检查后，才原子更新 `.env` 中这一项；更新失败必须恢复旧 Web 镜像，并把版本项恢复为旧值。

## 组件与接口

### Django system update app

新增独立 `apps.system_update`，负责：

- 仅老板角色可访问的 HTML 页面；
- CSRF 保护的检查、启动和状态接口；
- 有界超时、严格响应解析的 updater client；
- 将升级请求人与 task ID 关联；
- 幂等写入开始、成功、失败或需人工处理的审计记录。

页面和 Django JSON 只展示：当前版本、目标版本、发布时间、任务 ID、阶段、开始/结束时间、备份是否完成、是否回滚、清理状态和受控错误码/中文说明。

### Updater

updater 是与 Django 解耦的小型 Python 服务，拥有以下固定内部 API：

- `GET /health`：进程健康；
- `POST /v1/check`：拉取并检查 Web `stable`；
- `GET /v1/status`：返回脱敏的持久任务状态；
- `POST /v1/update`：只接受上一次检查得到的准确 `target_version`。

除 `/health` 外，所有请求必须携带恒定时间比较的 Bearer Token。请求体限制大小、拒绝未知字段和额外 JSON 值。updater 只管理固定 Compose project 的 `web` 服务；服务名、仓库、Compose 路径、环境文件和健康 URL均来自启动配置，不接受浏览器传入。

updater 的 Docker 适配层只允许预定义 argv，不通过 Shell 拼接用户输入。原始 Docker 输出和异常不会直接返回 Django 或浏览器。

### Owner bootstrap

新增与现有财务 bootstrap 一致的幂等老板账户命令。密码只允许从标准输入或环境读取，复用现有强度校验和审计模式，不在命令行参数或日志中出现。

## 升级状态与持久化

updater 使用权限为 `0600` 的 JSON 文件原子持久化检查结果和单个任务。任务阶段至少包括：

- `checking`
- `backing_up`
- `pulling`
- `stopping_web`
- `migrating`
- `starting_web`
- `checking_health`
- `stabilizing`
- `persisting_version`
- `cleaning`
- `rolling_back`
- `succeeded`
- `failed`
- `manual_intervention`

状态文件私有部分记录准确仓库、目标 digest、旧镜像 ID、旧标签、任务级 rollback alias、备份文件和内部错误；公共 View 永远不定义这些字段。

同一时间只允许一个检查或升级改变状态。活跃任务或 `manual_intervention` 会拒绝新升级。updater 重启时检查实际 Web 容器和持久任务：目标已健康且进入持久化/清理阶段时继续收尾；其他非终态优先用已验证的任务级 alias 恢复旧 Web；身份不完整或实际状态含糊时进入 `manual_intervention`。

## 版本检查

检查更新执行以下固定步骤：

1. 检查当前 Web 容器来自固定 GHCR 仓库，并读取 OCI version、digest 和 image ID。
2. 匿名拉取 `stable`，解析其不可变 digest 和 OCI version。
3. 要求当前版本与目标版本均为规范 `vX.Y.Z`。
4. 只在目标 SemVer 高于当前版本时报告可升级。
5. 缓存准确目标版本与 digest，并设置短有效期。

启动请求必须与未过期的检查结果完全一致。stable 在检查后发生变化时，启动失败并要求重新检查，不跟随新的可变目标。

## 成功流程

一次升级严格按以下顺序执行：

1. 保存任务并取得单任务锁。
2. 通过固定的 `docker compose exec -T web /app/scripts/backup.sh` 生成同一时间戳的数据库 dump 和附件归档；任一文件缺失或为空即停止。
3. 拉取目标 `repo@sha256:digest`，核对仓库、digest、OCI version 和平台。
4. 检查当前 Web 容器和镜像身份，为旧镜像创建带 task UUID 的本地 rollback alias。
5. 停止 Web，保持 db 和 updater 不变。
6. 通过任务级 Compose override 和 `pull_policy: never`，使用目标 digest 运行 `python manage.py migrate`。
7. 使用同一目标 digest 启动 Web。
8. 重复检查 Web `/health/`；该接口必须执行数据库 `SELECT 1`，不能只证明进程存活。
9. 连续稳定观察后，原子更新 `.env` 的 `SHUNDA_WEB_IMAGE_TAG` 为准确版本，并验证重新渲染的 Compose 仍只改变 Web 镜像。
10. 删除旧镜像前再次验证旧 image ID、标签、digest、OCI version 和无容器引用。
11. 只删除任务记录的准确旧版本标签、rollback alias 和 image ID，不使用 force。
12. 标记 `succeeded`。删除失败则业务仍成功，但清理状态为 `pending`。

## 失败与回滚

- 备份或拉取失败：不停止 Web，不执行迁移。
- 停止 Web 后任一步失败：使用任务级 rollback alias 和 `pull_policy: never` 恢复旧 Web，并检查数据库感知的健康接口。
- rollback 成功：任务标记 `failed` 和 `rolled_back=true`，保留目标镜像、旧镜像和备份以便排查。
- rollback 失败或镜像身份无法验证：进入 `manual_intervention`，阻止新任务，不删除任何相关镜像。
- 数据库迁移不自动反向执行。所有 release migration 必须向后兼容旧 Web；破坏性迁移不得发布到 `stable`。
- 浏览器在 Web 短暂不可达时以有上限的退避重试状态接口，恢复后继续显示同一 task，不重复启动。

## 精准清理规则

允许删除的对象只能来自本次任务持久状态，并同时满足：

- 仓库是固定的顺达 Web GHCR 仓库；
- alias 精确匹配 task UUID；
- image ID、digest、version 与升级前记录一致；
- 没有运行中或已停止容器引用；
- 不属于 db、updater、其他 Compose project 或构建缓存。

禁止 `docker image prune`、名称模糊匹配、猜测旧版本和 `--force`。`cleanup_pending` 的人工流程沿用相同校验，并从 root-owned `0600` 私有状态文件读取身份，不把身份粘贴到浏览器或普通日志。

## 页面设计

主导航新增“系统设置”，只对老板显示。升级页面使用现有紧凑工作台风格，包含：

- 当前版本和构建时间；
- 最新版本和发布时间；
- “检查更新”命令；
- 准确显示目标版本的二次确认对话框；
- 固定高度的阶段进度和最终结果；
- 备份、回滚、清理 3 个状态项；
- `cleanup_pending` 或 `manual_intervention` 的安全操作提示。

财务角色访问 URL 返回 403，不只是在导航中隐藏。页面不显示功能说明、内部命令、Docker 标识或 Token。

## 安全与错误处理

- Owner 权限在 Django 服务端逐请求验证；superuser 不自动等同老板业务角色。
- 所有变更操作仅接受 POST，并启用 CSRF。
- Django 到 updater 的连接设置连接、响应和总超时。
- updater Token 使用恒定时间比较，认证失败统一返回 401。
- 公共错误使用固定错误码和受控中文消息，不包含原始 HTTP/Docker/文件错误。
- 状态文件和 `.env` 权限为 `0600`，状态目录为 `0700`。
- 所有版本、UUID、digest、仓库和路径均按白名单格式验证。
- 审计记录只包含操作者、task ID、目标版本、结果和安全错误码。

## 部署与首次切换

首次上线不通过 Web 自更新完成，因为旧系统尚无 updater。流程为：

1. 创建 GitHub public repository 并推送源码。
2. 发布首个验证版本，确认两个 GHCR package 可匿名拉取。
3. 备份当前生产数据库、附件、`.env` 和 Compose 文件。
4. 把 DSM Compose 改为 3 服务镜像部署，写入随机内部 Token 和两个不可变初始 tag。
5. 拉取镜像并启动 db、web、updater；执行迁移和健康检查。
6. 验证 `sd.ace-station.top:1111` 的登录、导入和台账读取。
7. 保留首次切换前的旧本地构建镜像，直到完整 smoke 通过后再按引用关系单独清理。

后续 Web release 由老板页面执行。updater 本身升级必须在 DSM 手动修改固定 tag，并只重建 updater。

## 测试与验收

### 自动化

- 类型、SemVer、脱敏 View 和状态终态测试。
- 原子状态文件、权限、损坏文件和恢复测试。
- 固定 Docker/Compose argv、输入白名单、备份、digest override、回滚 alias、健康检查和精准清理测试。
- 成功、每个阶段失败、回滚失败、清理待处理和重启恢复状态机测试。
- updater Token、请求边界、并发冲突和错误脱敏测试。
- Django Owner/Finance/anonymous 权限、CSRF、client 超时、页面重连和审计幂等测试。
- Compose 契约：socket 只出现一次且仅在 updater，updater 无 host port，Web 升级范围不包含 db/updater。
- GitHub Actions 契约：测试通过后才发布不可变镜像，两个镜像均成功后才移动 Web stable。
- 全量 `pytest` 覆盖率不低于 80%，Node tests、Playwright 关键流程和生产静态资源构建通过。

### DSM 真实 smoke

发布验证版本后，显式确认才执行生产 smoke：

- 记录 db、web、updater 容器和镜像基线；
- 确认备份文件生成且非空；
- 从 Web 发起升级并等待终态；
- 确认 Web 版本更新，db/updater 容器未替换，数据卷未改变；
- 确认旧 Web 镜像只在成功后按准确身份删除；
- 用受控的目标 Web 健康失败验证自动回滚，新旧镜像均保留；
- 验证外部 `sd.ace-station.top:1111` 仍可登录和读取财务台账。

真实失败 smoke 只能使用专门的测试 release，不通过破坏生产数据库或修改真实财务数据制造失败。

## 完成标准

- 设计中的所有公开接口、状态阶段和安全边界均有测试所有者。
- GitHub release 和两个 GHCR package 发布成功且可匿名拉取。
- DSM 3 服务 Compose 健康，外部地址保持可用。
- Owner 可完成一次真实成功升级，并在受控失败 release 上证明自动回滚。
- 任何日志、浏览器响应、提交或镜像层均未包含生产秘密和 Docker 私有身份。
