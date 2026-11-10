# Cloud Memory Hub

把记忆放在自己的 NAS，让不同 Mac、不同 Agent 接着同一份项目进展工作。Cloud Memory Hub 复用 Hy-Memory / MemHub 的记忆核心与管理网页，通过 Docker 部署，并提供通用 MCP、CLI 和可安装 Skill。

- **多设备共享记忆**：各设备使用独立凭证访问同一个项目，保存的更新可直接读取；配置模型后可提炼和检索长期记忆。
- **通用 Agent 接入**：MCP 提供读写工具，Skill 约定调用时机；支持 MCP 或本机 CLI 的 Agent 可按接口接入，自动触发能力按宿主分别验证。
- **任务交接**：用 `handoff_save` 保存背景、进展与下一步，在另一端用返回的完整 ID 调用 `handoff_load` 接续任务；不要求不同 Agent 使用相同的原生会话格式。

源码：[Tsou92/Cloud-Memory-Hub](https://github.com/Tsou92/Cloud-Memory-Hub) · [Mac 接入](docs/client-installation.md) · [备份与恢复](docs/backup-and-recovery.md) · [发布计划](docs/release-plan.md)

## 当前状态与支持范围

**当前是面向自行部署用户的发行候选开发阶段，产品版本为 `0.1.0.dev0`，尚未发布正式版本标签或可下载的正式 Release。** 默认接入正在改为“NAS 生成一次性链接 → 电脑一条命令或交给 Agent 安装 → 验证 MCP”，无须另装图形 App。`346938a` 已通过完整 CI、三系统安装检查及真实 NAS／两台 Mac 的 Claude 读写；首页引导修正的最终制品、Codex 桌面新 HTTP 路径和生产升级仍待验，见[新接入验证](docs/reviews/2026-11-10-one-link-onboarding.md)。局部检查通过不等于已可正式发布。

| 入口 | 当前能力 | 验收边界 |
| --- | --- | --- |
| NAS 服务 | Linux amd64，Docker / Compose；项目隔离、共享记忆、原文与交接 | 已有真实 NAS 运行与两台 Mac 读写证据；其他架构未验 |
| 一次性链接＋远程 MCP | NAS 直接提供七个工具；电脑只添加连接和 Skill | 三系统安装 CI、真实 NAS 与两台 Mac 的 Claude 原生双向原文／交接通过；其他宿主的新路径仍待验 |
| Mac 候选客户端 | arm64、Python 3.12；独立安装、配对、MCP 配置导出、后台补传与状态 | preview.4 两台 Mac 安装、双向原文／交接及单端撤销通过；独立空卷实例真实网页登录、下载配对和客户端导入已通过 |
| Claude Code CLI | MCP / Skill 与指定项目的逐轮 hooks | preview.4 与真实 CLI 2.1.321 完成 2 轮采集、4 轮同会话召回；使用本地合成 Hub 和模型替身，不代表 NAS／外部模型联调 |
| Codex 桌面 | MCP / Skill / CLI 工作流 | 已在真实桌面对话中验证原生 MCP 读写；每轮自动触发未验 |
| 其他 Agent | 通用 MCP 或 Skill + CLI 接口 | OpenCode、Hermes、Cherry Studio、Open Design 等仍需逐一宿主验收 |
| 管理网页 | 项目、记忆、原文、来源、带引用的问答、普通忘记、设备配对、备份设置 | 独立实例真实配对、模型候选及连接测试、备份、项目问答和忘记已验证；新手引导、来源查看及窄屏复验通过；最终 NAS 制品仍待验 |

NAS 已验证两版本自动备份及重启后计划保留，并完成受控恢复的 **11 项正常恢复检查与 7 项真实进程中断检查**：空三卷导入、历史回滚保留较新忘记／设备撤销、损坏归档拒绝及保护版本恢复。此证据使用独立合成数据，不包含 NAS 断电、硬盘故障或全部中断位置，见[演练记录](docs/backup-and-recovery.md#nas-恢复演练记录)。

完整历史自动导入、所有 Agent 的自动采集、离线记忆读取缓存、离线删除规则传播和旧日志全域治理仍在后续范围。当前候选应先用独立项目和合成数据试用，保留原数据及备份；正式发行仍需完成最终 NAS 制品验收，并让包含授权修复的最终提交通过完整远程 CI，详见[客户端实测与边界](docs/reviews/2026-11-10-self-service-clients.md)。

## 从源码在 NAS 试部署

准备 Git、Docker 和 Compose，以及可下载固定上游、Python / Node 依赖与内置检索模型的构建网络。下面使用新的 Compose 项目试运行，不连接已有实例的数据卷：

```sh
git clone https://github.com/Tsou92/Cloud-Memory-Hub.git
cd Cloud-Memory-Hub
export COMPOSE_PROJECT_NAME=cloud-memory-hub-trial
docker build --platform linux/amd64 --build-arg CMH_REVISION="$(git rev-parse HEAD)" --tag cloud-memory-hub:development .
docker compose run --rm setup
docker compose up -d --no-build hub
docker compose ps
```

首次 `setup` 创建实例身份和管理员凭证，只执行一次；后续启动复用原卷。若已有同名试验项目，请使用新的项目名。完整前置条件、旧实例迁移和排障见[容器运行指南](docs/development-runtime.md)，不要把首次安装命令用于覆盖旧实例。

Compose 默认只监听 NAS 的 `127.0.0.1:8800`。跨设备使用需先为该服务配置 HTTPS 入口，并将 `.env` 中的 `CMH_PUBLIC_ORIGIN` 设置为实际 Origin 后重新创建 Hub 容器；两机 MVP 实测使用 Tailscale HTTPS。`localhost` 是当前设备本机，不能作为另一台 Mac 访问 NAS 的地址。

在自己的 NAS 终端查看管理员凭证，再到实际入口登录：

```sh
docker compose exec hub cat /config/admin-token
```

凭证只在本地查看，不放入聊天、Issue 或 Git。登录后按页面配置模型、创建项目，再进入「连接 Agent」生成一次性链接，为每台设备分别配对；JSON 下载保留在高级接入中。模型 Key 填在管理网页中；原文更新和任务交接可先使用，长期记忆提炼与语义搜索依赖模型配置及处理结果。停止服务可用 `docker compose down`，保留卷；备份计划默认关闭，启用方法见[备份与恢复](docs/backup-and-recovery.md)。

## Mac 候选安装与 Agent 接入

默认流程见[设备与 Agent 自助接入](docs/client-installation.md#默认一次性链接接入)：复制管理页面生成的命令或安装话术，电脑无需下载完整记忆核心。以下包安装方式保留用于本地采集与离线补传，不是默认远程 MCP 的前置条件。

公开客户端包尚未发布。已有对应 arm64 候选包时，先校验摘要，解压后执行 `sh install.sh`，再按[Mac 自助接入](docs/client-installation.md)导入网页下载的配对文件。安装器使用新的私有目录，不覆盖原客户端；每台 Mac 使用自己的设备身份和队列。

现在需要从源码试用，可按 [Agent 接入说明](docs/agent-access.md#mac-客户端安装)安装 Python 3.12 客户端；源码安装依赖固定上游与本机目录位置。维护者构建与发布候选包的方式见[发行制品说明](docs/release-artifacts.md)。Mac 只运行客户端，无需本地 Docker 或模型。

完成配对后，导出通用 MCP JSON 或 Codex TOML 配置，并安装公共 [Skill](packages/agent-skill/cloud-memory-hub/SKILL.md)。更新配置后重新连接 MCP 或开始新会话。用一条合成记忆验证 A 端保存、B 端读取，再验证 `handoff_save` / `handoff_load`；仅显示 MCP 已连接不能代替读写验收。后台补传负责已入队内容，不会自动扫描所有历史对话。

## 验证证据与文档

| 内容 | 入口 |
| --- | --- |
| 两机共享记忆与交接 | [preview.4 两机与 Claude CLI](docs/reviews/2026-11-10-self-service-clients.md) · [两台 Mac MVP](docs/reviews/2026-11-10-two-host-mvp.md) · [自动共享](docs/reviews/2026-11-10-visible-turn-mvp.md) |
| 长期记忆与下一轮召回 | [显式记忆验收](docs/reviews/2026-11-10-explicit-memory.md) · [同会话与重连](docs/reviews/2026-11-10-next-turn-and-reconnect.md) · [队列拒绝修复](docs/reviews/2026-11-10-terminal-upload-rejections.md) |
| 网页与候选客户端 | [首次网页使用](docs/reviews/2026-11-10-browser-first-use.md) · [控制台记录](docs/reviews/2026-11-10-project-console.md) · [Mac 安装及待验项](docs/reviews/2026-11-10-self-service-clients.md) · [模型配置](docs/reviews/2026-11-10-model-catalog.md) |
| 备份与恢复 | [操作及 NAS 演练](docs/backup-and-recovery.md) · [自动备份记录](docs/reviews/2026-11-10-managed-backup.md) |
| 需求与支持边界 | [需求说明](docs/requirements.md) · [发布计划](docs/release-plan.md) · [六入口能力清单](docs/research/g1-client-capabilities.md) |
| 工程与来源 | [工程规范](docs/engineering.md) · [上游复用清单](docs/research/upstream-assessment.md) · [架构](docs/architecture/overview.md) |
| 质量与发行 | [验证计划及历史记录](docs/verification.md) · [发行制品](docs/release-artifacts.md) · [贡献指南](CONTRIBUTING.md) · [安全说明](SECURITY.md) |

## 开发原则与检查

优先复用 Hy-Memory / MemHub 已有实现和测试，增量补丁、需求、设计、验证与审查进入 Git。自动读写和已有会话刷新必须在真实宿主验收；源码不保存真实对话、记忆库、备份、密钥或个人配置。

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

## 许可证

项目自有源码采用 Apache-2.0，见 [LICENSE](LICENSE)、[NOTICE](NOTICE) 和[许可决策](docs/adr/0061-project-license.md)。构建时按固定提交获取上游并应用已登记补丁，发行保留上游及第三方许可证、NOTICE 与来源清单。

<details>
<summary>历史工程验证与兼容设计索引</summary>

以下文档保留各自日期、版本与验证范围，不代表当前发行包已全部验收。完整追踪见[验证计划](docs/verification.md)；旧实例操作仍以当前[运行指南](docs/development-runtime.md)和[受控恢复指南](docs/backup-and-recovery.md)为准。

| 内容 | 入口 |
| --- | --- |
| 已确认产品需求 | [需求说明](docs/requirements.md) |
| 开发流程与质量门槛 | [工程规范](docs/engineering.md) |
| 上游代码复用清单及差距 | [Hy-Memory / MemHub 评估](docs/research/upstream-assessment.md) |
| 架构方向与备选方案 | [架构草案](docs/architecture/overview.md) |
| 决策记录 | [ADR-0001：复用优先](docs/adr/0001-reuse-first.md) |
| 容器运行与配置 | [实验运行指南](docs/development-runtime.md) · [ADR-0002：NAS 边界](docs/adr/0002-nas-runtime-boundary.md) |
| 上游升级与数据边界 | [ADR-0003：SDK 权威存储基线](docs/adr/0003-sdk-authority-baseline.md) |
| Mac 安装与配对 | [自助接入指南](docs/client-installation.md) |
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

其他历史接口与能力记录：

- [0004-remote-device-access](docs/adr/0004-remote-device-access.md)
- [agent-access](docs/agent-access.md)
- [agent-recall](docs/agent-recall.md)
- [local-connector](docs/local-connector.md)
- [remote-ingest](docs/remote-ingest.md)
- [remote-read](docs/remote-read.md)
- [source-capture](docs/source-capture.md)

</details>
