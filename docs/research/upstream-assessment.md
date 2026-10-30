# Hy-Memory / MemHub 复用评估

初评日期：2026-10-21。本文件保留当时的源码阅读结论和路径；后续实测见[运行记录](../reviews/2026-10-22-runtime.md)。2026-10-22 已完成[新基线评估](2026-10-22-upstream-candidate.md)并采用 SDK 权威存储版本；旧治理／session 表的描述不能继续当作现有能力。真实客户端验收仍未完成。

## 1. 基线与结论

本次初评基线为 [Tencent-Hunyuan/Hy-Memory 历史提交](https://github.com/Tencent-Hunyuan/Hy-Memory/tree/0244ebe38ce0288a44a8b6fc7f209ab2bf6c3766)。当前基线见 [manifest](../../upstream/manifest.json) 和 [ADR-0003](../adr/0003-sdk-authority-baseline.md)。

上游不仅提供记忆 SDK，还包含 MemHub 产品：Core、来源导入、采集和召回桥接、治理、任务队列、网页界面及恢复能力。首选方向是扩展 MemHub，并保留上游测试；只采用 SDK 后重写其他现有模块不符合复用要求。

初评版本必须分开看：当时根目录 SDK 源码为 `2.0.5.dev3`，MemHub 为 `1.0.4`，其 Core 固定依赖 `hy-memory==2.0.4`。现已通过独立测试切换至 Core／SDK `2.0.5.dev3` 的一致组合；不能将不同提交的源码和依赖混用。

上游采用 [Apache-2.0](https://github.com/Tencent-Hunyuan/Hy-Memory/blob/0244ebe38ce0288a44a8b6fc7f209ab2bf6c3766/LICENSE)。引入源码时保留许可、适用的 NOTICE、第三方声明和修改记录。Cloud Memory Hub 自身许可证尚未选定。

## 2. 可复用清单

以下路径相对于上游仓库；“新增”仅描述待验证的缺口，不表示现在已经获证必须重新实现整个模块。

| 能力 | 上游证据／入口 | 首选处理 | 需验证或补充 |
| --- | --- | --- | --- |
| 记忆提炼、检索与持久化 | SDK；`memhub/apps/core/src/one_memory/hy_memory_bridge.py` 及相关生命周期、写入核验模块 | 保留 Core 对 SDK 的封装 | 固定 SDK 与主分支兼容性；真实提炼质量 |
| 原始证据与历史导入 | `memhub/apps/core/src/one_memory/imports/`；source-adapter 合同 | 复用来源适配及游标机制 | 两台设备身份、项目映射、全量导入负载 |
| 自动采集 | `memhub/packages/host-bridge/src/capture-hooks.ts` | 扩展现有安装器和事件处理 | 各客户端实际版本、已有配置共存、远程连接 |
| 召回与上下文注入 | `recall-installer.ts`、`context_auto_recall`、`context_search`；host-context 合同 | 复用召回协议、预算与配置保护 | 六个实际入口的下一轮自动注入 |
| 记忆治理与纠错 | `memory_governance`、`memory_visibility`、`mutation_receipts` | 扩展现有治理过程 | 跨设备冲突、遗忘与彻底删除的持久传播 |
| 持久任务调度 | `memory_queue`、`memory_tasks`；架构中的 `memory_write_jobs` | 保留队列与单写入者约束 | 历史导入与实时任务争用、重试和时效 |
| 权限、模型配置及用量 | `auth.py`、`model_profiles`、`model_request_audit`、`model_request_budget` | 复用并审查信任边界 | 从回环接口扩展为多设备 API 的认证和撤销 |
| 管理界面 | `memhub/apps/web/` | 扩展现有页面和组件 | 设备状态、多设备冲突、同步失败及删除状态 |
| 恢复 | `memhub/apps/core/src/one_memory/managed_restore.py` | 复用源验证、保护副本和数据库切换逻辑 | 定时备份、全量导出、派生索引一致性、删除防复活 |
| 变更通知 | `change_outbox.py` | 评估现有类型和语义 | 文件明确标注自动生产暂停，不能当成现成同步引擎 |
| 测试与发行 | 上游 host-bridge 测试、Core 测试、运行时与发行目录 | 保留可运行测试与构建链 | 真实通过记录、NAS 部署制品及更新兼容性 |

源码索引：[Core](https://github.com/Tencent-Hunyuan/Hy-Memory/tree/0244ebe38ce0288a44a8b6fc7f209ab2bf6c3766/memhub/apps/core/src/one_memory)、[Host Bridge](https://github.com/Tencent-Hunyuan/Hy-Memory/tree/0244ebe38ce0288a44a8b6fc7f209ab2bf6c3766/memhub/packages/host-bridge/src)、[MemHub 产品说明](https://github.com/Tencent-Hunyuan/Hy-Memory/blob/0244ebe38ce0288a44a8b6fc7f209ab2bf6c3766/memhub/README.md)。目录中有文件或测试不等于该入口已启用或测试已通过。

## 3. 会影响方案的已知差距

1. **本地产品不等于多设备服务。** [当前架构](https://github.com/Tencent-Hunyuan/Hy-Memory/blob/0244ebe38ce0288a44a8b6fc7f209ab2bf6c3766/memhub/docs/ARCHITECTURE.md)围绕一套本地运行时和数据目录，Core 是 SQLite 的唯一业务写入者。不能简单暴露原回环接口或通过 NAS 共享数据库文件来完成本项目。
2. **当前来源合同是文字证据。** [source-adapter-v1](https://github.com/Tencent-Hunyuan/Hy-Memory/blob/0244ebe38ce0288a44a8b6fc7f209ab2bf6c3766/memhub/docs/contracts/source-adapter-v1.md)默认排除工具参数、结果、推理内容、文件正文和命令输出。图片、PDF 解析及关键操作证据需要合同扩展和隐私验证，不能宣称现成满足。
3. **实时性需要测量。** 架构描述了 10 分钟事件时间窗口、同来源 FIFO 和历史导入屏障。事件时间窗口不等于必然等待 10 分钟；但是否延迟发布、历史积压是否阻塞实时请求必须实测，不能直接承诺 1 分钟。
4. **召回链的测试不等于宿主采纳。** [host-context-transport-v1](https://github.com/Tencent-Hunyuan/Hy-Memory/blob/0244ebe38ce0288a44a8b6fc7f209ab2bf6c3766/memhub/docs/contracts/host-context-transport-v1.md)区分检索、注入和真实宿主行为。MCP 可以提供读取工具，但不会自动保证每轮调用。
5. **不能覆盖用户现有集成。** capture-hooks 已列出 Claude Code、Codex、Cursor 的命令钩子及 OpenCode 插件路径；Hermes/OpenClaw 使用单一 memory-provider 槽位，该安装路径没有直接接管。必须先探测、共存或提供可撤销适配。
6. **恢复不是删除协议。** managed_restore 的存在不能证明跨设备、离线队列、索引、历史备份与删除规则已统一，需要专门验证防复活和恢复过程。

不得因为上游残留目录中有旧 MySQL 方案，就认为当前应引入 MySQL 或 PostgreSQL；以当前运行组合、架构说明和测试为准。

## 4. 客户端证据矩阵

“文档支持”或“代码存在”均不是本项目真机验收结论。

| 入口 | 已有证据 | 本阶段结论 |
| --- | --- | --- |
| Claude Code CLI | 上游采集／召回安装器；[官方 hooks](https://code.claude.com/docs/en/hooks) 描述 UserPromptSubmit 和 Stop | 优先复用；需验证采集、下一轮注入与配置恢复 |
| Codex 桌面版 | 上游 Codex hooks；本机随客户端提供的 CLI 帮助含 hooks；[官方 hooks](https://learn.chatgpt.com/docs/hooks) 支持 UserPromptSubmit 上下文 | 不据 CLI 推断桌面全链路；必须桌面现有对话验证 |
| OpenCode 终端 | 上游插件安装路径；[官方插件文档](https://opencode.ai/docs/plugins/) 提供消息与会话事件 | 优先复用；验证注入时机、版本及卸载恢复 |
| Hermes 终端 | 上游 Hermes 相关安装测试文件；[官方 MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp) | MCP 与 memory-provider 共存、自动采集／召回待实测 |
| Cherry Studio 桌面版 | 本机安装元数据 2.1.7；[官方 MCP 文档](https://docs.cherryai.com.cn/advanced-basic/extensions/mcp.md) | 工具接入有依据；自动采集与每轮注入仍待验证 |
| Open Design 桌面版 | 本机安装元数据 0.24.1，bundle `io.open-design.desktop`；存在插件运行包 | 尚未确定官方扩展合同；不按同名网上项目推定能力 |
| 未来 Agent | 上游已有 Cursor、WorkBuddy 等路径；本项目保留通用合同 | 用户所称 Workbody 与 WorkBuddy 是否同一产品待接入时确认 |

如某入口只能按需 MCP 检索，能力状态必须明确显示“部分支持”，不得将首批全自动需求静默降级。

## 5. 下一次验证产物

建立隔离的上游基线，记录版本和原始测试结果；选取真实桌面和 CLI 的最短闭环，使用合成数据验证采集、发布、下一轮注入；给出每项复用的测试路径、必要补丁和无法复用的证据。完成后再确定上游以受控分叉、包依赖或局部封装落地，避免过早复制整套仓库。

## 2026-10-30 原生来源标记增量

补丁 0011 继续复用固定提交的 `coding/store.py` 追加列迁移与 SQL upsert、两类 reconciler 及 writer；原实现没有同正文原子写入的生产 request_id／内容摘要，故增加这两个可空字段及共享核验函数，不另建记忆存储。复用上游 store、reconciler、fact_reconciler、writer、路由和提示词语言测试；本项目新增多轮来源与旧写入检测测试。理由、兼容性及验证边界见 [ADR-0023](../adr/0023-reconcile-input-lineage.md) 和[记录](../reviews/2026-10-30-record-origins.md)。

## 2026-10-30 普通聊天历史输入增量

补丁 0012 复用 `pipelines/writer.py::_get_recent_history` 的原解析／窗口及姓名渲染，并通过 client 原持久结果透传快照。原函数只返回文本；原生 L1 也没有生产请求标记，故保存实际贡献节点的 ID／正文／身份，保留空标记，后续由 Hub 精确产出索引核验来源。没有新模型调用、历史选择算法或存储后端。保留 payload-only、L1 读取边界、写入缓存／结果／延迟及 reconcile 回归；完整范围和接线边界见 [ADR-0024](../adr/0024-chat-input-evidence.md)。

Hub 后续接线复用已有归档、产出索引与图预算，以及上游 `ChatMessage.from_dict`、`WriteRequest.get_flat_content` 和 `strip_tool_messages`；只增加归档表及来源适配，没有重做原始对话解析或提炼。实测 Core 的 `_hy_agent_id` 包含来源／日期／工作区，保持该既有隔离语义，验证范围见 [接线记录](../reviews/2026-10-30-chat-lineage.md)。
