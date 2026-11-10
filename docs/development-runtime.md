# 实验容器运行指南

状态：用于开发与发行候选试部署，尚未完成正式发行验收。管理核心和网页复用 MemHub；普通业务的受控备份、空目标导入与同实例历史回滚已实现，并通过 NAS 的 11 项正常恢复及 7 项真实进程中断检查，操作与范围见[备份与恢复](backup-and-recovery.md)。下文保留旧实例迁移和诊断文件治理的独立边界，不以业务恢复通过代替全部旧日志治理；网络边界见 [ADR-0002](adr/0002-nas-runtime-boundary.md)，历史治理证据见[在途任务取消及恢复记录](reviews/2026-11-10-inflight-execution.md)。

## 构建与首次运行

诊断恢复的只读维护入口为 `diagnostic-preview --writers-stopped --operation-id UUID`：先停止该实例所有写者，使用原 data/config/governance 卷与原私有配置运行命令。它持有现有存储租约，在普通服务启动之前读取实际已提交的 purge 和精确文件候选；返回元数据，不输出日志正文、不签确认、不执行删除。失败保留现场。新增 execution schema2 目前仅供隔离组件验证，普通服务明确拒绝该格式，不能在常驻实例自行迁移；该诊断文件治理的完整确认/恢复入口尚未交付，见 [ADR-0043](adr/0043-diagnostic-physical-execution.md)。

已有完整绑定的隔离 schema2 实例可显式运行 `activate-file-coverage --writers-stopped`，将文件覆盖要求提高到 SDK 和 Core 的四个范围。命令沿用该实例的三个目录、私有配置及互斥锁，只增加覆盖要求，不完成迁移、删除或业务恢复。成功响应中的 `pending` 表示待处理操作数，`changed=false` 表示已经启用；它们都不是日志正文已清除的证明。旧 schema1、绑定缺口或无效 witness 保持原状并拒绝；若提高要求后的复核失败，保留现场并可重试同一命令。提高后不能直接使用不认识该能力的旧程序；不要手工删除能力行以回退。详见 [ADR-0047](adr/0047-core-file-coverage-and-physical-authority.md)。普通服务的 schema2 限制仍保持。

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

新增严格诊断策略的开发镜像在创建服务前接管该进程的 Python 日志输出。debug 仍可使用，但日志只保存步骤、耗时、计数、规范请求标识和安全错误定位，不保存提示、响应或任意异常正文。SDK 独立使用仍有 legacy 默认。直接调用 create_hub 也会分离宿主的自定义日志处理器，保留对象且不关闭文件；应在专用进程运行 Hub。活跃旧日志资源会导致策略启用明确拒绝，应先结束旧资源后重试。见 [ADR-0041](adr/0041-strict-diagnostic-content-policy.md)。

已有诊断文件的只读范围检查见 [ADR-0042](adr/0042-diagnostic-file-governance.md)。开发者可运行 `make probe-diagnostic-inventory` 验证真实接纳请求、原生共根日志及前后数据不变；该命令仅创建独立合成实例，不检查或清理日常数据。当前未接通管理员文件删除入口、持久清理进度或旧日志恢复重放，不因解析器返回 remove 就认为文件已经删除。

此策略不清理升级前已存在的日志文件或 Docker 日志；回退旧镜像也会恢复旧诊断输出行为。正式实例升级仍需完成日志历史治理与整体恢复验收，不能只因新日志没有正文就认为旧副本已删除。

- `hub_data` 保存 Core 数据、接纳的处理任务及索引；`hub_config` 保存管理员凭证、加密密钥、加密的模型凭证数据库，以及设备身份／项目授权、来源、事件回执及完整已过滤原文库。配置卷现在也包含长期历史正文，必须纳入与数据卷一致的私有备份和治理范围。设备权限协议见 [ADR-0004](adr/0004-remote-device-access.md)，上传及回滚语义见 [ADR-0006](adr/0006-remote-ingest.md)。
- `hub_governance` 保存不含正文的治理意图、执行记录及独立 SDK 准入日志，独立挂载至 `/governance`；普通业务快照恢复不能覆盖或删除它。自定义 `CMH_GOVERNANCE_DIR` 时，目录不能位于 data/config 内，也不能包含它们。机制与限制见 [ADR-0011](adr/0011-governance-authority-and-offline-sync.md) 和 [ADR-0038](adr/0038-durable-sdk-admission.md)。
- 普通停止使用 `docker compose down`；不加 `--volumes`。保留卷后再次启动会复用配置和数据。
- 配置密钥丢失会导致加密凭证无法读取。候选 Compose 增加独立 `hub_backups` 卷与容器内调度，默认关闭；配置方法见[备份与恢复](backup-and-recovery.md)。新镜像默认 `supervise`，显式 `serve` 仍可用但不执行定时备份。已有自定义 Compose 需要增加 `/backups` 可写私有挂载。全量归档导入、保留当前安全授权与治理的同实例历史回滚、损坏归档拒绝和一次真实进程中断恢复已有 NAS 合成数据证据；断电、硬盘故障、全部中断位置及最终发行包仍未验，不能据此承诺任意灾难恢复。
- 初始化中断后不要直接删除既有凭证重试；先检查配置卷并保留副本，避免覆盖已经使用的密钥。

## 已有实验实例的治理迁移

当前开发版本要求设备权威库版本 3、主治理日志版本 2 及独立准入日志，旧实例不会在普通启动时自动迁移。先停止 Hub，确认旧容器已经退出，并完整备份现有 config/data/governance 三卷；保存原镜像标签、Compose 配置及校验值。然后使用本轮镜像与包含治理卷的新 Compose 文件执行：

```sh
docker compose stop hub
docker compose ps -a hub
# 确认停服并完成私有备份后
docker compose run --rm setup migrate-governance --writers-stopped
docker compose up -d --no-build hub
docker compose ps
```

`--writers-stopped` 只是已停服的显式声明；命令不会代替操作者停止其他挂载相同卷的容器。应使用同一个 Compose 项目名及既有 config/data 卷。迁移保留实例 ID、设备凭证和授权，不执行真实历史导入或启用模型。重复执行只核验既有绑定；不能修复缺失或不可信的治理记录。缺失身份库、密钥错误、记录损坏和水位倒退都应保持停服并恢复可信副本。

版本 3 身份库与版本 2 主治理日志不能交给旧镜像运行。治理操作已经存在时，不允许通过回滚身份库、丢弃治理卷或恢复旧镜像来重新开放。普通记忆治理已接入执行与启动恢复；恢复完成前不开放服务写入，无法证明安全的旧／已许可任务或未支持范围继续拒绝。上游不满足 Hub 删除语义的直接删除入口仍返回 409，暂停或拒绝不计作治理完成。

准入升级仍使用同一个显式迁移命令：先建立 `governance/admissions.sqlite3`，将其身份绑定在主治理日志中，再原子升级身份库与外部见证。历史治理目标、设备凭证和实例身份不变。新版普通启动不会自动创建缺失准入日志；已经绑定的文件丢失时须保留现场，不能新建空库替代。新接纳任务使用内部 `cmh2` 身份，旧回执保留 `cmh` 身份，HTTP 事件格式及回执接口不变。见 [ADR-0038](adr/0038-durable-sdk-admission.md)。

开发镜像 `c0467f5` 已在本地及 NAS 的独立临时卷中通过六套安装探针、完整 Compose 验证及实际双镜像验证：未迁移时新版拒绝启动；重复迁移保持日志、意图及身份绑定不变；新版重启可完整读取原事件。只恢复旧身份库、保留新 Core／治理日志时，旧 `cde427c` 镜像的实际启动命令以 `governance_version_unsupported` 拒绝，Core／治理文件摘要不变。

NAS 常驻空实验实例已停服备份 config/data/governance 三卷及 Compose／环境配置，显式迁移两次验证幂等，并升级到 `cloud-memory-hub:dev-c0467f5`。身份库版本 3、主治理日志版本 2 和准入双重绑定核验通过；实例身份、凭证、挂载、资源限制及回环端口保持不变，启动与再次重启后均运行健康。未启用外部模型、导入真实历史或调整 SSH；这不代表完整产品交付。证据与未完成项见[验证记录](reviews/2026-11-10-inflight-execution.md)。

## 已接纳原文迁移

接纳扩展 `cmh_ingest_schema=2` 与设备权威库版本 3 是两个独立版本。新的归档镜像拒绝普通启动旧接纳格式；先完成上面的治理迁移（若需要），保持停服，完整备份 data/config/governance 三卷、镜像标签及部署配置，再执行：

```sh
docker compose run --rm setup migrate-originals --writers-stopped
docker compose up -d --no-build hub
docker compose ps
```

该命令要求三个存储租约均可取得，逐条从旧队列冻结输入恢复 Event，与旧摘要、身份和重新生成的 WriteRequest 比对，所有记录通过后一次提交。旧队列已回收、表示有歧义或内容不符时返回 `original_migration_source_unproven` 并回滚整个迁移，不能填空正文后继续。重复运行只验证现有归档。详细兼容边界见 [ADR-0017](adr/0017-accepted-original-archive.md)。

没有下迁移；旧镜像会拒绝新格式。升级后出现问题应保持停服并诊断，不能直接切旧镜像，更不能修改版本号绕过检查。恢复升级前完整备份也必须保留并验证当前独立治理权威，不能用旧备份覆盖新的忘记／删除记录。现在可按[受控恢复指南](backup-and-recovery.md)执行同实例历史回滚或空目标导入；所选版本必须满足该指南的身份、密钥、治理链和未完成写入检查，不支持直接混搭或覆盖三个旧卷。

## 原文关联扩展

类型化关联新增独立 `cmh_lineage_schema=1` 及两个只含关系的表，随启动在原有配置库中创建，不改变接纳 schema 2。既有原文 v1 实例仍须先执行上面的显式迁移。未知版本或半套关系表拒绝启动；不删表重建。常规写入与 unknown 恢复在关联保存成功后才完成；旧 completed 任务以原回执和 SDK 成功结果分批补核对，不重发模型。完整备份须继续覆盖三个卷。该关联扩展本身允许旧归档代码忽略新增表，但当前身份库、治理日志与准入绑定的版本要求仍然适用；不能据此使用旧镜像回退。

SDK 和 Core 都从 manifest 固定源码及补丁构建，Hub 锁文件将两者指向隔离检出目录；本地使用 editable，Docker 使用自足 wheel，不依赖宿主源码。SDK 构建约束固定 setuptools／wheel，运行依赖版本保持原锁定集合。安装探针必须核对实际 site-packages 并验证 variant 持久化，不能以源码存在证明镜像已带补丁。

## NAS 目标与当前边界

目标设备是 DXP4800 Plus、Pentium Gold 8505、约 16 GB 内存。用户提供的观测中可用内存约 7.8 GiB；该数值随其他容器负载变化。当前容器限制为 3 GiB 内存、2 CPU，提炼任务并发为 1；这只是初始保护配置，不能替代真实模型、历史导入和索引负载测试。

已知 Docker 数据位于双 NVMe RAID0，主数据卷为四盘 RAID5。可通过 `CMH_BACKUP_PATH` 将备份放在不同磁盘的私有目录；实际宿主路径、权限和容量告警仍须逐次部署核对。限定恢复流程已在 NAS 独立卷完成[演练](backup-and-recovery.md#nas-恢复演练记录)，不代表宿主磁盘故障已测。RAID0 中任一盘故障会影响该阵列，RAID5 也不能代替版本备份。

当前 Compose 只发布本机回环端口。远程入口必须配置明确的 HTTPS Origin（`CMH_PUBLIC_ORIGIN`）并经过认证边界；还未提供或验证通用生产反向代理配置。Tailscale HTTPS 已通过[两台 Mac 的 MVP 验收](reviews/2026-11-10-two-host-mvp.md)，绿联转发和节点小宝仍待实际验证。容器地址不能作为 NAS 外部可达地址使用。

SSH 能连接不等于可以操作 Docker。部署前在同一账户执行 `docker info` 和 `docker compose version`；如果 Docker socket 返回权限错误，应先由 NAS 管理员配置账户权限。不能通过放宽 socket 的全局访问权限来绕过。当前核对结果见 [NAS 运行记录](reviews/2026-10-23-nas-preflight.md)。

当前 `serve` 在启动 Core 前独占 data/config/governance 根目录的 `.cmh-storage.lock`；`init` 和 `migrate-governance` 也参与同一协议。`storage_in_use` 表示另一个参与协议的进程仍持有至少一个目录，命令拒绝继续；应先正常停止原服务并观察进程退出，不能删除锁文件来解锁。进程退出后 OS 自动释放锁，空文件保留。根目录和锁文件须为当前运行用户所有、私有权限，拒绝软硬链接及其他不安全形态。详见 [进程互斥设计](adr/0013-runtime-storage-exclusion.md)。

不识别此协议的旧镜像及直接调用 SDK 的程序不受上述锁约束。第一次升级仍须显式停服核对；迁移继续要求 `--writers-stopped`。Compose 的 setup 服务也挂载同一 data/config/governance 三个卷；自定义维护容器必须使用相同挂载，不能锁住镜像内另一个空目录后宣称原业务存储已独占。进程锁不等于在线队列已经排空；当前治理执行另行暂停处理任务、核验准入证明并清理确认范围，旧／已许可任务的完整副作用覆盖仍待完成。

## 可复现验证

```sh
make check
make check-runtime
make probe-inflight-governance
make check-upstream
make check-web
make check-container
```

`check-upstream` 需要 Node 可用，复用上游隔离执行器运行 Core 的 PR 测试集，排除性能和真实外部服务测试。模型缺失会先按上游清单获取并校验，不以跳过模型测试冒充完整验证。

`check-web` 使用固定 Node 镜像构建网页并测试首启、SDK 记忆库／历史和原有记忆空间。它针对本次兼容补丁，不等于整个网页测试集或完整产品验收。

`probe-inflight-governance` 在四个独立进程中验证真实 HTTP 处理入口、生产启动恢复及 SQLite 队列：forget 保留完整原文，purge 清除目标正文，仅恢复旧 Core 后同一签名操作自动重放。模型就绪和 embedding 使用合成条件，不调用外部模型；安装包验证另以脚本的 `--installed` 参数检查模块来源。这不替代真实客户端或网络入口验收。

容器检查在当前 Docker 端点创建随机命名的独立 Compose 项目和临时卷，先在禁网、只读容器中验证模型摘要及真实向量生成，再验证初始化、身份／跨源拒绝、网页登录会话、模型配置密文存储、重启后的配置和密钥可读性。设备探针另验证零权限签发、项目授权、凭证轮换、重启持久性，以及撤销后再次重启仍然失效，随后仅清理该次测试资源。它不访问用户模型、真实 Agent 或生产卷。

来源采集探针补充合成 Claude 格式文件 → 已安装 capture/send CLI → 真实 HTTP/Core 队列及重启验证；Mac 上实际 Claude CLI 使用单独的 `make check-claude` 隔离探针。结果和边界见[采集验证记录](reviews/2026-10-24-claude-native.md)。

读取探针在停止临时 Hub 后，用真实 SDK 导入合成夹具，再启动服务，经已安装 CLI／实际 HTTP 验证项目检索、原始引用、重启持久性和撤销后拒绝。离线夹具导入不算远程事件提炼成功；详细证据见[授权读取记录](reviews/2026-10-24-scoped-retrieval.md)。

远程验证可在目标 NAS 已加载镜像后，设置 `DOCKER_HOST=ssh://nas` 和相应的 `CMH_IMAGE`，直接运行 `uv run --locked python -m scripts.smoke_container`，复用同一测试。先用相同环境执行 `docker info` 确认目标端点；必须具备 NAS Docker 权限，并将远程结果与本地结果分开记录。此处的 `nas` 是开发主机 SSH 配置中的连接别名。

上传探针使用已安装的连接库过滤合成事件，再通过容器内真实 HTTP 写入 Core 队列。第一次故意不确认本地回执，重启后重放同一事件，核对只有一个任务，再撤销来源并重启检查。此验证不依赖 SSH 端口转发，不激活模型，也不等于两个真实 Agent 的同步或一分钟可召回验收。

认证测试不代表浏览器全流程、六入口、跨设备同步或外部模型已经通过；以验证记录的证据范围为准。

稳定来源屏蔽更新见 [ADR-0033](adr/0033-durable-source-suppression.md)：新忘记／彻底删除操作在独立治理记录中保存来源摘要，原文清除后仍拒绝同来源修订。数据库格式不变。旧忘记可沿用保留原文的屏蔽依据；旧彻底删除若已丢失来源对应关系，所属项目的新上传返回 `governance_source_rules_unavailable`，该项目的 SDK 写入也拒绝。不要删除治理记录来解除保护，也不要回退到缺少此修复的镜像后继续上传；需要原来源资料才能补齐旧规则，自动修复工具尚未实现。升级仍须停服备份三个卷。

实际历史输入扩展保存在配置卷的 `cmh_context_schema=1`／`cmh_event_contexts`，与原输出关联原子提交，不改变 lineage v1。升级前继续停服备份三个卷；该扩展表本身可被旧版忽略，但不豁免当前身份库及治理日志的版本门禁。旧结果中的缺失字段不能自动补成空依赖；只从既有 SDK 结果核对，不重跑模型。`make probe-input-context` 验证真实 writer 配合合成决策的跨进程恢复、来源依赖和链未覆盖时的拒绝，详见[记录与限制](reviews/2026-10-29-input-context.md)。

## 治理恢复更新（2026-11-09）

普通记忆的已完成 forget 在启动时重新应用原签名范围的抑制，防止恢复旧业务数据后重新进入 SDK 提炼输入。目标项目存在未建模实体副本时拒绝恢复；含生产力记录的 forget 也明确拒绝，因为其完整内部输入抑制尚未实现。旧未提交计划仍可丢弃重验，不能把仅有计划签名当作日志意图已提交。请求身份、实际回归及尚未完成的范围见[本轮记录](reviews/2026-11-09-governance-retry-and-entities.md)。

[ADR-0037](adr/0037-chroma-purge-sequence.md) 约束 Chroma 日志清理：目标正文和向量日志需要清除，但无正文的真实 DELETE 墓碑留给原生消费/回收流程，不能手工清空队列或重置水位。`governance_chroma_sequence_invalid` 表示当前序号完整性无法证明；应保留现场和最新治理目录，停止继续写入，先诊断或恢复一致的业务副本。不能通过改水位、重复提交空写入、丢弃治理记录或切回旧镜像来解除保护。该检测不证明任意混搭备份整体一致，也不会自动补回旧版本可能遗漏的写入；是否已安装验证以对应提交的记录为准。

## 在途任务取消与恢复（2026-11-10）

完整准入证明仍为 `ENROLLED` 的新任务可在已确认范围内取消，`unknown` 或 `hy_invoked` 标记本身不是未调用证明。forget 保留 Hub 原文的显式读取，purge 仅清理本操作目标任务的终态正文。已有终态清理证书不能授权取消后来恢复出的非终态任务；旧任务不补签出生证明。

启动会先丢弃未提交到主日志的孤立计划，再完成待执行操作并重放已完成操作。仅恢复旧 Core 时仍使用原签名范围，不追加意图，也不清理后来无关任务。已完成 purge 的队列正文即使已变成严格清理标记，仍须重新压缩并核验，以覆盖旧页尚留正文的恢复点；历史操作较多时会重复全库压缩，当前没有恢复耗时上限承诺。

代码、恢复测试与安装证据见[本轮验证记录](reviews/2026-11-10-inflight-execution.md)。这些历史治理能力仍不覆盖完整生产力治理、任意混搭备份恢复、离线删除传播或全部 SDK 副作用。发行候选已另行提供限定的[受控备份恢复](backup-and-recovery.md)，两者按各自范围验收；当前发布进度见[发布计划](release-plan.md)。
