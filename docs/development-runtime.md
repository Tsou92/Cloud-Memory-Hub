# 实验容器运行指南

状态：用于开发验证，尚未达到完整产品或 NAS 正式部署的交付条件。管理核心和网页复用 MemHub；网络边界见 [ADR-0002](adr/0002-nas-runtime-boundary.md)，未验证项见[运行记录](reviews/2026-10-22-runtime.md)。

## 构建与首次运行

需要 Docker 与 Compose，以及可访问 GitHub、锁定依赖和 Hugging Face 模型文件的构建网络。目标为 Linux amd64；其他主机架构运行该镜像需要 Docker 的模拟支持。镜像包含第三方许可证和上游来源清单，以及构建时按固定摘要校验的内置检索模型；首次运行无需下载它。源码检出及依赖安装不会访问本机 Agent 历史。

在仓库根目录执行：

```sh
docker build --platform linux/amd64 --tag cloud-memory-hub:development .
docker compose run --rm setup
docker compose up -d --no-build hub
docker compose ps
```

Apple Silicon 开发主机可为 Compose 命令设置 `DOCKER_DEFAULT_PLATFORM=linux/amd64`。首次初始化创建管理员凭证与加密密钥；重复初始化会拒绝覆盖，不作为重启步骤。

Compose 支持用 `CMH_IMAGE` 指定本次已验证的镜像标签，默认仍为 `cloud-memory-hub:development`。NAS 验证使用独立的提交标签，并设置独立 Compose 项目名；不得复用已有服务的数据卷。构建时传入 `--build-arg CMH_REVISION=<提交编号>`，可在镜像标签中追踪来源。`CMH_IMAGE` 同时作用于初始化、服务和禁网模型检查，避免混用版本。

打开 `http://localhost:8800`，输入初始化产生的管理员凭证。可在自己的终端查看：

```sh
docker compose exec hub cat /config/admin-token
```

不要把该输出粘贴到公开日志、Issue 或聊天。网页通过受限会话访问；容器重启后重新登录。模型配置界面来自上游，真实模型的测试／激活会访问相应提供商，当前验证使用未激活的合成配置。

## 数据与停止

- `hub_data` 保存 Core 数据、接纳的处理任务及索引；`hub_config` 保存管理员凭证、加密密钥、加密的模型凭证数据库，以及设备身份／项目授权、来源和事件回执库。设备权限协议见 [ADR-0004](adr/0004-remote-device-access.md)，上传及回滚语义见 [ADR-0006](adr/0006-remote-ingest.md)。
- 普通停止使用 `docker compose down`；不加 `--volumes`。保留卷后再次启动会复用配置和数据。
- 配置密钥丢失会导致加密凭证无法读取；当前还没有密钥轮换、灾难恢复或完整备份功能，不应依赖此实验版本承载唯一数据。
- 初始化中断后不要直接删除既有凭证重试；先检查配置卷并保留副本，避免覆盖已经使用的密钥。

## NAS 目标与当前边界

部署前应核对宿主可用资源及其他容器负载。当前容器限制为 3 GiB 内存、2 CPU，提炼任务并发为 1；这只是初始保护配置，不能替代真实模型、历史导入和索引负载测试。

运行数据与备份应使用独立的持久化位置，并在部署前核对存储冗余方案。计划将运行数据和历史备份版本放在不同卷，实际宿主路径、权限、容量告警和恢复流程需在部署阶段验证。磁盘阵列不能代替版本备份。

当前 Compose 只发布本机回环端口。远程入口必须配置明确的 HTTPS Origin（`CMH_PUBLIC_ORIGIN`）并经过认证边界；还未提供或验证生产反向代理配置。不同远程接入方式均待实际验证。容器地址不能作为 NAS 外部可达地址使用。

SSH 能连接不等于可以操作 Docker。部署前在同一账户执行 `docker info` 和 `docker compose version`；如果 Docker socket 返回权限错误，应先由 NAS 管理员配置账户权限。不能通过放宽 socket 的全局访问权限来绕过。当前核对结果见 [NAS 运行记录](reviews/2026-10-23-nas-preflight.md)。

## 可复现验证

```sh
make check
make check-runtime
make check-upstream
make check-web
make check-container
```

`check-upstream` 需要 Node 可用，复用上游隔离执行器运行 Core 的 PR 测试集，排除性能和真实外部服务测试。模型缺失会先按上游清单获取并校验，不以跳过模型测试冒充完整验证。

`check-web` 使用固定 Node 镜像构建网页并测试首启、SDK 记忆库／历史和原有记忆空间。它针对本次兼容补丁，不等于整个网页测试集或完整产品验收。

容器检查在当前 Docker 端点创建随机命名的独立 Compose 项目和临时卷，先在禁网、只读容器中验证模型摘要及真实向量生成，再验证初始化、身份／跨源拒绝、网页登录会话、模型配置密文存储、重启后的配置和密钥可读性。设备探针另验证零权限签发、项目授权、凭证轮换、重启持久性，以及撤销后再次重启仍然失效，随后仅清理该次测试资源。它不访问用户模型、真实 Agent 或生产卷。

来源采集探针补充合成 Claude 格式文件 → 已安装 capture/send CLI → 真实 HTTP/Core 队列及重启验证；Mac 上实际 Claude CLI 使用单独的 `make check-claude` 隔离探针。结果和边界见[采集验证记录](reviews/2026-10-24-claude-native.md)。

读取探针在停止临时 Hub 后，用真实 SDK 导入合成夹具，再启动服务，经已安装 CLI／实际 HTTP 验证项目检索、原始引用、重启持久性和撤销后拒绝。离线夹具导入不算远程事件提炼成功；详细证据见[授权读取记录](reviews/2026-10-24-scoped-retrieval.md)。

远程验证可在目标 NAS 已加载镜像后，设置 `DOCKER_HOST=ssh://nas` 和相应的 `CMH_IMAGE`，直接运行 `uv run --locked python -m scripts.smoke_container`，复用同一测试。先用相同环境执行 `docker info` 确认目标端点；必须具备 NAS Docker 权限，并将远程结果与本地结果分开记录。此处的 `nas` 是开发主机 SSH 配置中的连接别名。

上传探针使用已安装的连接库过滤合成事件，再通过容器内真实 HTTP 写入 Core 队列。第一次故意不确认本地回执，重启后重放同一事件，核对只有一个任务，再撤销来源并重启检查。此验证不依赖 SSH 端口转发，不激活模型，也不等于两个真实 Agent 的同步或一分钟可召回验收。

认证测试不代表浏览器全流程、六入口、跨设备同步或外部模型已经通过；以验证记录的证据范围为准。
