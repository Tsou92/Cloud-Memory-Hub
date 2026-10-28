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

Apple Silicon 开发主机可为 Compose 命令设置 `DOCKER_DEFAULT_PLATFORM=linux/amd64`。首次初始化创建管理员凭证、加密密钥、实例身份和独立治理记录；重复初始化会拒绝覆盖，不作为重启步骤。

Compose 支持用 `CMH_IMAGE` 指定本次已验证的镜像标签，默认仍为 `cloud-memory-hub:development`。NAS 验证使用独立的提交标签，并设置独立 Compose 项目名；不得复用已有服务的数据卷。构建时传入 `--build-arg CMH_REVISION=<提交编号>`，可在镜像标签中追踪来源。`CMH_IMAGE` 同时作用于初始化、服务和禁网模型检查，避免混用版本。

打开 `http://localhost:8800`，输入初始化产生的管理员凭证。可在自己的终端查看：

```sh
docker compose exec hub cat /config/admin-token
```

不要把该输出粘贴到公开日志、Issue 或聊天。网页通过受限会话访问；容器重启后重新登录。模型配置界面来自上游，真实模型的测试／激活会访问相应提供商，当前验证使用未激活的合成配置。

## 数据与停止

- `hub_data` 保存 Core 数据、接纳的处理任务及索引；`hub_config` 保存管理员凭证、加密密钥、加密的模型凭证数据库，以及设备身份／项目授权、来源、事件回执及完整已过滤原文库。配置卷现在也包含长期历史正文，必须纳入与数据卷一致的私有备份和治理范围。设备权限协议见 [ADR-0004](adr/0004-remote-device-access.md)，上传及回滚语义见 [ADR-0006](adr/0006-remote-ingest.md)。
- `hub_governance` 保存不含正文的治理意图，独立挂载至 `/governance`；普通业务快照恢复不能覆盖或删除它。自定义 `CMH_GOVERNANCE_DIR` 时，目录不能位于 data/config 内，也不能包含它们。机制与限制见 [ADR-0011](adr/0011-governance-authority-and-offline-sync.md)。
- 普通停止使用 `docker compose down`；不加 `--volumes`。保留卷后再次启动会复用配置和数据。
- 配置密钥丢失会导致加密凭证无法读取；当前还没有密钥轮换、灾难恢复或完整备份功能，不应依赖此实验版本承载唯一数据。
- 初始化中断后不要直接删除既有凭证重试；先检查配置卷并保留副本，避免覆盖已经使用的密钥。

## 已有实验实例的治理迁移

本轮镜像要求设备权威库版本 2，旧实例不会在普通启动时自动迁移。先停止 Hub，确认旧容器已经退出，并完整备份现有 config/data；保存原镜像标签、Compose 配置及校验值。然后使用本轮镜像与包含治理卷的新 Compose 文件执行：

```sh
docker compose stop hub
docker compose ps -a hub
# 确认停服并完成私有备份后
docker compose run --rm setup migrate-governance --writers-stopped
docker compose up -d --no-build hub
docker compose ps
```

`--writers-stopped` 只是已停服的显式声明；命令不会代替操作者停止其他挂载相同卷的容器。应使用同一个 Compose 项目名及既有 config/data 卷。迁移保留实例 ID、设备凭证和授权，不执行真实历史导入或启用模型。重复执行只核验既有绑定；不能修复缺失或不可信的治理记录。缺失身份库、密钥错误、记录损坏和水位倒退都应保持停服并恢复可信副本。

版本 2 身份库不能交给旧镜像运行。治理操作已经存在时，不允许通过回滚身份库、丢弃治理卷或恢复旧镜像来重新开放。当前仍未开放完整忘记／彻底删除：检测到待执行的治理意图会暂停整个实例；上游不满足 Hub 删除语义的直接删除入口返回 409。这里只验证迁移和故障保护，不把暂停服务当作产品治理完成。

## 已接纳原文迁移

接纳扩展 `cmh_ingest_schema=2` 与设备权威库版本 2 是两个独立版本。新的归档镜像拒绝普通启动旧接纳格式；先完成上面的治理迁移（若需要），保持停服，完整备份 data/config/governance 三卷、镜像标签及部署配置，再执行：

```sh
docker compose run --rm setup migrate-originals --writers-stopped
docker compose up -d --no-build hub
docker compose ps
```

该命令要求三个存储租约均可取得，逐条从旧队列冻结输入恢复 Event，与旧摘要、身份和重新生成的 WriteRequest 比对，所有记录通过后一次提交。旧队列已回收、表示有歧义或内容不符时返回 `original_migration_source_unproven` 并回滚整个迁移，不能填空正文后继续。重复运行只验证现有归档。详细兼容边界见 [ADR-0017](adr/0017-accepted-original-archive.md)。

没有下迁移；旧镜像会拒绝新格式。升级后出现问题应保持停服并诊断，不能直接切旧镜像，更不能修改版本号绕过检查。恢复升级前完整备份也必须保留并验证当前独立治理权威，不能用旧备份覆盖新的忘记／删除记录。完整恢复产品流程仍待实现。

## 原文关联扩展

类型化关联新增独立 `cmh_lineage_schema=1` 及两个只含关系的表，随启动在原有配置库中创建，不改变接纳 schema 2。既有原文 v1 实例仍须先执行上面的显式迁移。未知版本或半套关系表拒绝启动；不删表重建。常规写入与 unknown 恢复在关联保存成功后才完成；旧 completed 任务以原回执和 SDK 成功结果分批补核对，不重发模型。完整备份须继续覆盖三个卷。旧归档版本可忽略新增表运行，但不展示或新增关联；重新启用新版后补核对，不能把旧版读取当作功能等价回退。

SDK 和 Core 都从 manifest 固定源码及补丁构建，Hub 锁文件将两者指向隔离检出目录；本地使用 editable，Docker 使用自足 wheel，不依赖宿主源码。SDK 构建约束固定 setuptools／wheel，运行依赖版本保持原锁定集合。安装探针必须核对实际 site-packages 并验证 variant 持久化，不能以源码存在证明镜像已带补丁。

## NAS 目标与当前边界

目标设备是 DXP4800 Plus、Pentium Gold 8505、约 16 GB 内存。用户提供的观测中可用内存约 7.8 GiB；该数值随其他容器负载变化。当前容器限制为 3 GiB 内存、2 CPU，提炼任务并发为 1；这只是初始保护配置，不能替代真实模型、历史导入和索引负载测试。

已知 Docker 数据位于双 NVMe RAID0，主数据卷为四盘 RAID5。计划将运行数据和历史备份版本放在不同卷，实际宿主路径、权限、容量告警和恢复流程需在部署阶段验证。RAID0 中任一盘故障会影响该阵列，RAID5 也不能代替版本备份。

当前 Compose 只发布本机回环端口。远程入口必须配置明确的 HTTPS Origin（`CMH_PUBLIC_ORIGIN`）并经过认证边界；还未提供或验证生产反向代理配置。Tailscale、绿联转发和节点小宝均待实际验证。容器地址不能作为 NAS 外部可达地址使用。

SSH 能连接不等于可以操作 Docker。部署前在同一账户执行 `docker info` 和 `docker compose version`；如果 Docker socket 返回权限错误，应先由 NAS 管理员配置账户权限。不能通过放宽 socket 的全局访问权限来绕过。当前核对结果见 [NAS 运行记录](reviews/2026-10-23-nas-preflight.md)。

当前 `serve` 在启动 Core 前独占 data/config/governance 根目录的 `.cmh-storage.lock`；`init` 和 `migrate-governance` 也参与同一协议。`storage_in_use` 表示另一个参与协议的进程仍持有至少一个目录，命令拒绝继续；应先正常停止原服务并观察进程退出，不能删除锁文件来解锁。进程退出后 OS 自动释放锁，空文件保留。根目录和锁文件须为当前运行用户所有、私有权限，拒绝软硬链接及其他不安全形态。详见 [进程互斥设计](adr/0013-runtime-storage-exclusion.md)。

旧镜像及直接调用 SDK 的程序不识别上述锁。第一次升级仍须显式停服核对；迁移继续要求 `--writers-stopped`。Compose 的 setup 服务也挂载同一 data/config/governance 三个卷；自定义维护容器必须使用相同挂载，不能锁住镜像内另一个空目录后宣称原业务存储已独占。当前进程锁不等于在线队列已经排空，完整治理执行仍需单独完成暂停、在途结果核对和持久清理。

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
