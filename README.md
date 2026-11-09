# Cloud Memory Hub

部署在 NAS 上、连接不同设备与 Agent 的共享记忆服务。

**当前阶段：G1 复用与可行性验证。实验性管理入口已在本地和 NAS 容器验证，尚未发布完整产品。**

实验入口直接复用固定版本的 MemHub Core 和管理网页，增加 NAS 认证边界及加密凭证存储。跨设备自动采集、同步、离线缓存、删除传播等能力仍在开发，当前不应接入真实历史或用作唯一记忆存储。

当前开发版本使用独立治理卷、主治理日志版本 2、身份库版本 3 及独立 SDK 准入日志；已有实验实例须停服备份后显式迁移，不能直接用旧镜像回退。普通记忆忘记、彻底删除及旧业务数据恢复后的重放已有实现。新接纳任务在能够证明尚未获准调用 SDK 时，可在确认范围内取消：忘记保留可显式读取的完整原文，彻底删除清理目标正文；仅恢复旧队列后，启动会重放同一操作。

旧任务或已获准调用 SDK 的任务仍需完整副作用证明；生产力记录的忘记与物理删除、诊断文件/后台日志副本清理、完整备份恢复和离线规则传播也仍待完成。范围或证明不完整时明确拒绝执行，恢复未完成前不开放写入。当前证据与限制见[在途任务取消及恢复记录](docs/reviews/2026-11-10-inflight-execution.md)，迁移方式见[运行指南](docs/development-runtime.md)。

NAS 常驻空实验实例已升级至 `c0467f5`，停服备份、显式迁移、启动与重启验证通过。本地和 NAS 的六套安装探针、完整 Compose 验证及实际双镜像迁移／拒绝均通过；未启用外部模型、导入真实历史或修改日常 Agent 配置。这仍是 G1 实验验证，详细证据见上述记录。

后续 `c7a8d27` 增加原生写入观察并修复请求诊断缓存遗漏：本地与 NAS 各七项隔离安装验证通过，常驻实例仍保持 `c0467f5`。观察字段不等于完整副作用证明，见[本轮记录](docs/reviews/2026-11-10-write-effects.md)。

诊断后台写入现已接入维护等待：删除会等真实日志任务和线程结束，搜索保持并发，关闭失败可重试。Hub 全量 1107 通过、5 跳过；原生/Core 相关 119 项通过；`e527566` 的同一镜像在本地和 NAS 各 8 项隔离安装验收全部通过，常驻 `c0467f5` 未升级。既有日志文件正文仍需后续清理，见[诊断生命周期记录](docs/reviews/2026-11-10-diagnostic-quiescence.md)。

新增 Hub 严格诊断策略，debug 下也仅输出封闭元数据，SDK 默认兼容保留。最终本地 Hub 1111 通过／5 跳过，原生/Core 187 项通过，独立并发/异常审查与工程门禁通过；`c2242fa` 同一镜像在本地/NAS 各 9 项隔离安装验证及本地完整 Compose 检查通过，常驻 `c0467f5` 未升级。业务原文和记忆保留，旧物理日志仍待清理，见[验证记录](docs/reviews/2026-11-10-diagnostic-content.md)。

已有诊断文件的只读前置检查已实现：完整请求证明不补写 witness，原生记录按精确字节摘要分类，目录检查兼容实际共根。Hub 全量 1227 通过／5 跳过，工程与独立审查通过；`8588581` 同一镜像在本地/NAS 各 10 项隔离安装验证通过，常驻 `c0467f5` 未变。尚未接通文件物理清理或恢复重放，见[本步记录](docs/reviews/2026-11-10-diagnostic-inventory.md)。

后续已实现独立的 SDK 两域文件执行与恢复组件，以及停服只读预览命令；新增签名执行格式、精确记录授权、逐文件替换和原日志处理器重绑定。最终 Hub 1397 通过／5 跳过，原生 192 项、Core 39 项通过；`cb2c9ca` 同一镜像在本地/NAS 各 11 套隔离安装探针通过，98 个安装生产文件与源码一致，常驻 `c0467f5` 未变。产品物理确认与完整恢复路由仍未启用。范围和证据见 [ADR-0043](docs/adr/0043-diagnostic-physical-execution.md) 和[本步记录](docs/reviews/2026-11-10-diagnostic-file-execution.md)。

后续修复现有普通服务的恢复故障隔离：已完成删除的重试失败后，禁止新 SDK 调用及原生 Core 正文输出，完整恢复成功后才重新开放；保留正常 worker、读取和管理员维护。`913f343` 完整 Hub 1424 项通过、5 项可选后端跳过，同一镜像本地/NAS 各 11 项安装探针通过，常驻服务未升级；见 [ADR-0044](docs/adr/0044-governance-runtime-recovery-fence.md) 与[验证记录](docs/reviews/2026-11-10-runtime-recovery-fence.md)。

当前继续接入联合文件恢复：真实父删除与多份 repair 的授权合并检查，文件 pass 冻结贡献者，旧临时文件共同证明后才处理；旧 memory-only 删除可进入只读维护预览。`bcef154` 完整 Hub 1507 项通过、5 项跳过，同一镜像本地/NAS 各 11 项安装探针通过，常驻服务未升级；普通服务尚未启用新执行格式，见 [ADR-0045](docs/adr/0045-diagnostic-recovery-graph.md) 和[验证记录](docs/reviews/2026-11-10-diagnostic-recovery.md)。

后续已统一业务与恢复状态读取，并接入真实终端审阅/签名组件及 Core 写者维护。`e537cba` 完整 Hub 1585 项通过、5 项跳过，同一镜像本地/NAS 各 12 项安装验收通过，102 个生产代码文件逐项核对一致，常驻服务未升级。Core 历史正文、Docker 日志覆盖和完整运行协调仍待接通，普通服务继续拒绝新执行格式；见 [ADR-0046](docs/adr/0046-governance-runtime-and-output-coverage.md) 和[验证记录](docs/reviews/2026-11-10-runtime-binding-and-core-maintenance.md)。

最新增量已接入 Core 四域文件分类、签名覆盖要求及停服激活：提高要求后，旧 SDK 回执重新待验证；全严格元数据文件可恢复验证，无授权 Core 正文保留现场并拒绝执行。`913f746` 完整 Hub 1759 项通过、5 项跳过，同镜像本地/NAS 各 12 套安装探针及两端真实旧镜像拒绝验证通过，104 个生产文件核对一致，常驻服务保持健康且未变。Core 正文许可、Docker 宿主覆盖与普通运行协调仍待完成；见 [ADR-0047](docs/adr/0047-core-file-coverage-and-physical-authority.md) 和[本步记录](docs/reviews/2026-11-10-core-file-coverage.md)。

已增加版本化的设备身份与上传接口：独立凭证、项目授权、轮换、撤销、来源注册和持久接纳。上传直接复用上游处理队列，并与回执一起保存完整已过滤原文；旧接纳格式需要显式迁移，见 [ADR-0017](docs/adr/0017-accepted-original-archive.md)。接纳回执不等于完成提炼、检索或客户端同步。身份兼容性见 [ADR-0004](docs/adr/0004-remote-device-access.md)，上传合同见 [远程接纳接口](docs/remote-ingest.md)。

本地连接组件已提供凭证过滤、持久投递队列、单次发送、持续补传、授权读取 CLI，以及显式授权的 Claude 原生文字／工具记录采集。Claude 每轮在线召回已在隔离的实际 CLI 中验证；日常客户端安装、离线读取缓存与其他适配器仍待接通。配置与限制见 [连接组件说明](docs/local-connector.md)、[来源采集](docs/source-capture.md)、[授权读取接口](docs/remote-read.md) 和 [Claude 召回](docs/agent-recall.md)。

项目默认提供跨 Agent 的任务接续，同时保留原始记录追溯能力。首批目标入口为 Claude Code CLI、Codex 桌面版、OpenCode 终端、Hermes 终端、Cherry Studio 客户端和 Open Design 客户端；通过通用接口持续扩展其他 Agent。

## 开发原则

- 需求、复用调查、架构决策、实现、测试、审查和发布均进入 Git。
- **优先复用 Hy-Memory / MemHub 已有代码及测试，不重复开发已有能力。**
- 全自动读写、已有对话刷新等能力必须在实际客户端验收，不能用 MCP 已连接代替。
- 源码仓库不保存真实对话、记忆库、备份、密钥或个人机器配置。

## 项目导航

| 内容 | 入口 |
| --- | --- |
| 已确认产品需求 | [需求说明](docs/requirements.md) |
| 开发流程与质量门槛 | [工程规范](docs/engineering.md) |
| 上游代码复用清单及差距 | [Hy-Memory / MemHub 评估](docs/research/upstream-assessment.md) |
| 架构方向与备选方案 | [架构草案](docs/architecture/overview.md) |
| 决策记录 | [ADR-0001：复用优先](docs/adr/0001-reuse-first.md) |
| 容器运行与配置 | [实验运行指南](docs/development-runtime.md) · [ADR-0002：NAS 边界](docs/adr/0002-nas-runtime-boundary.md) |
| 上游升级与数据边界 | [ADR-0003：SDK 权威存储基线](docs/adr/0003-sdk-authority-baseline.md) |
| 测试与需求追踪 | [验证计划](docs/verification.md) |
| 分阶段交付 | [里程碑](docs/roadmap.md) |
| 本轮工程评审状态 | [评审记录](docs/reviews/2026-10-21-foundation.md) |
| 运行时验证进展 | [容器与上游验证记录](docs/reviews/2026-10-22-runtime.md) |
| NAS 与设备身份验证 | [NAS 运行记录](docs/reviews/2026-10-23-nas-preflight.md) · [设备身份记录](docs/reviews/2026-10-23-device-access.md) |
| 连接与授权读取验证 | [自动补传](docs/reviews/2026-10-24-connector-transport.md) · [记忆与来源读取](docs/reviews/2026-10-24-scoped-retrieval.md) |
| 实际客户端合同验证 | [Claude Code CLI 采集](docs/reviews/2026-10-24-claude-native.md) · [在线召回](docs/reviews/2026-10-25-claude-recall.md) |
| 治理与恢复验证 | [运行时隔离](docs/reviews/2026-10-25-governance-activation.md) · [NAS 迁移与关联缺口](docs/reviews/2026-10-26-governance-nas-and-lineage.md) |
| 治理范围预览 | [管理员观察接口](docs/governance-preview.md) · [验证记录](docs/reviews/2026-10-26-governance-preview.md) |
| 维护与服务互斥 | [存储租约设计](docs/adr/0013-runtime-storage-exclusion.md) · [安装与 NAS 验证](docs/reviews/2026-10-26-storage-exclusion.md) |
| SDK 共享准入 | [设计与边界](docs/adr/0014-shared-sdk-admission.md) · [安装与 NAS 验证](docs/reviews/2026-10-26-sdk-admission.md) |
| 未知写入恢复 | [精确结果核对](docs/adr/0015-sdk-unknown-write-reconciliation.md) · [验证与限制](docs/reviews/2026-10-27-sdk-reconciliation.md) |
| 工作／编程记忆读取 | [只读适配](docs/adr/0016-productivity-read-adapter.md) · [验证与限制](docs/reviews/2026-10-27-productivity-read.md) |
| 接纳原文长期保留 | [归档与迁移设计](docs/adr/0017-accepted-original-archive.md) · [验证记录](docs/reviews/2026-10-28-original-archive.md) |
| 写入结果与原文关联 | [类型化关系设计](docs/adr/0018-typed-output-provenance.md) · [验证记录](docs/reviews/2026-10-28-output-lineage.md) |
| 工作记忆治理观察 | [范围合并设计](docs/adr/0019-productivity-governance-inventory.md) · [验证记录](docs/reviews/2026-10-29-productivity-governance.md) |
| 实际历史输入追溯 | [输入捕获与兼容性](docs/adr/0020-exact-productivity-context.md) · [验证记录](docs/reviews/2026-10-29-input-context.md) |
| 编程链治理观察 | [轮次与线程关系](docs/adr/0021-coding-chain-inventory.md) · [验证记录](docs/reviews/2026-10-29-chain-inventory.md) |
| 合并决策输入留存 | [不可变输入快照](docs/adr/0022-reconcile-input-snapshots.md) · [验证记录](docs/reviews/2026-10-29-reconcile-inputs.md) |
| 原生记录生产来源 | [来源标记与关系设计](docs/adr/0023-reconcile-input-lineage.md) · [验证记录](docs/reviews/2026-10-30-record-origins.md) |
| 合并输入来源图 | [输入归档与关系验证](docs/reviews/2026-10-30-reconcile-graph.md) |
| 普通聊天历史输入 | [捕获设计](docs/adr/0024-chat-input-evidence.md) · [SDK 验证](docs/reviews/2026-10-30-chat-history.md) · [Hub 来源接线](docs/reviews/2026-10-30-chat-lineage.md) |
| 普通聊天合并输入 | [实际候选快照与兼容设计](docs/adr/0025-chat-reconcile-evidence.md) · [NAS 验证](docs/reviews/2026-10-31-chat-reconcile.md) |
| 普通记忆生产版本 | [设计](docs/adr/0026-chat-memory-origins.md) · [验证记录](docs/reviews/2026-11-02-memory-origins.md) |
| 普通记忆合并来源 | [关系与兼容设计](docs/adr/0027-chat-reconcile-lineage.md) · [安装与 NAS 验证](docs/reviews/2026-11-03-memory-lineage.md)；完整输入与 SDK 副作用覆盖仍待完成 |
| 基础画像输入与写入证据 | [原生旁路设计](docs/adr/0028-basic-profile-evidence.md) · [SDK 验证](docs/reviews/2026-11-03-profile-evidence.md) · [Hub 来源接线](docs/adr/0029-basic-profile-lineage.md) · [接线验证](docs/reviews/2026-11-03-profile-lineage.md) |
| 忘记与彻底删除执行 | [状态机、清理清单与已知缺口](docs/adr/0032-governance-execution.md) |
| 删除后的来源屏蔽 | [稳定来源规则与旧操作兼容](docs/adr/0033-durable-source-suppression.md) |
| 内容重导入与任务协调 | [设计与剩余清扫工作](docs/adr/0034-inflight-ingest-and-content-reimport.md) · [分步验证](docs/reviews/2026-11-09-content-quiescence.md) |
| 删除中断与缓存恢复 | [请求身份与重放设计](docs/adr/0035-purge-cache-recovery.md) · [验证记录](docs/reviews/2026-11-09-purge-cache-recovery.md) |
| 治理请求重试与实体副本 | [请求一致性设计](docs/adr/0036-governance-request-identity.md) · [恢复检查与验证](docs/reviews/2026-11-09-governance-retry-and-entities.md) |
| 删除后继续写入 | [Chroma 序号与删除重放设计](docs/adr/0037-chroma-purge-sequence.md) · [验证记录](docs/reviews/2026-11-09-chroma-purge-sequence.md) |
| 持久准入与在途任务取消 | [准入证明与恢复设计](docs/adr/0038-durable-sdk-admission.md) · [执行、安装与验证边界](docs/reviews/2026-11-10-inflight-execution.md) |
| 原生写入副作用 | [请求级观察与后续持久证书](docs/adr/0039-sdk-write-effect-observation.md) · [诊断副本恢复与验证](docs/reviews/2026-11-10-write-effects.md)；观察字段不授予删除权限 |
| 诊断后台写入 | [暂停、排空与关闭设计](docs/adr/0040-diagnostic-write-quiescence.md) · [回归及安装记录](docs/reviews/2026-11-10-diagnostic-quiescence.md)；不代替已有文件清理 |
| 诊断正文策略 | [Hub 严格元数据与 SDK 兼容](docs/adr/0041-strict-diagnostic-content-policy.md) · [真实出口及并发验证](docs/reviews/2026-11-10-diagnostic-content.md)；旧物理日志仍待治理 |
| 旧诊断文件范围 | [只读证明、精确记录与恢复合同](docs/adr/0042-diagnostic-file-governance.md) · [本步验证](docs/reviews/2026-11-10-diagnostic-inventory.md)；尚未执行物理清理 |
| 诊断物理执行组件 | [持久状态、同步文件替换与停服维护](docs/adr/0043-diagnostic-physical-execution.md) · [验证记录](docs/reviews/2026-11-10-diagnostic-file-execution.md)；产品执行路由未启用 |
| 恢复故障隔离 | [SDK 许可与 Core 输出屏障](docs/adr/0044-governance-runtime-recovery-fence.md) · [验证记录](docs/reviews/2026-11-10-runtime-recovery-fence.md) |
| 联合文件恢复 | [真实父子图与冻结贡献者](docs/adr/0045-diagnostic-recovery-graph.md) · [验证记录](docs/reviews/2026-11-10-diagnostic-recovery.md) |
| 执行视图、终端审阅与 Core 维护 | [完整接线设计](docs/adr/0046-governance-runtime-and-output-coverage.md) · [同镜像安装与 NAS 验证](docs/reviews/2026-11-10-runtime-binding-and-core-maintenance.md)；完整输出覆盖与正式入口仍待接通 |
| Core 文件覆盖与停服激活 | [覆盖版本与授权边界](docs/adr/0047-core-file-coverage-and-physical-authority.md) · [本步验证](docs/reviews/2026-11-10-core-file-coverage.md)；无来源正文和 Docker 输出仍待治理 |
| 派生写入生命周期 | [请求级收尾与取消](docs/adr/0030-derived-task-lifecycle.md) · [验证记录](docs/reviews/2026-11-04-derived-lifecycle.md) · [派生写入覆盖边界](docs/adr/0031-derived-writer-coverage.md) |
| 贡献与安全 | [贡献指南](CONTRIBUTING.md) · [安全说明](SECURITY.md) |

## 本地质量检查

工程检查需要 Git、Python 3.12–3.14，以及 uv。运行时使用 Python 3.12、uv 0.12.5 和上游约束版本；各自使用锁文件。

```sh
uv sync --locked
make check
make check-runtime
make check-upstream
# 可选：本机已安装 Claude CLI 时，使用隔离配置和本地模型替身
make check-claude
make check-claude-recall
# 以下两项需要 Docker
make check-web
# 构建 Linux amd64 并在独立临时卷中验证
make check-container
```

检查包括格式、静态检查、工程门禁测试、文档链接、禁止入库的数据路径和源码凭证扫描。当前通过这些检查**不代表产品功能已通过验收**。

## GitHub 与发行状态

目前为本地 Git 仓库，尚未创建远程仓库、配置远程分支保护或发布产品。
仓库提供 GitHub Actions 工作流、Issue / PR 模板和依赖更新配置；上传后才能验证远程执行。

项目自身的许可证尚未选定。公开发布前应确定仓库可见性、许可证及第三方声明。
上游代码引入时保留原始许可证、NOTICE、来源提交和修改记录；当前尚未复制上游业务源码。
