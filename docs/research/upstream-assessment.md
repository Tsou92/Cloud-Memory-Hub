# Hy-Memory / MemHub 复用评估

2026-11-10 本地投递终态设计：NAS 安装验证证明已遗忘内容被正确拒绝，但 Mac 网络 outbox 队首阻塞后续事件。继续扩展既有 LocalOutbox、HTTP 传输和 capture 原子事务；上游 `memory_write_jobs` 是服务端模型处理队列，不替代跨设备上传及本地身份绑定。新增本地拒绝索引和显式格式升级，无新上游补丁、无 NAS 业务库迁移；边界见 [ADR-0055](../adr/0055-terminal-upload-rejections.md)。

2026-11-10 显式记忆投影：同安装镜像的禁网合成重现证明 HY 正常执行场景分类和工具消息过滤，但 Hub 把 remember 文字当作工具输出，导致过滤后空输入。只修正 Hub 新接纳时的 assistant / AGENT 角色映射，继续复用原提炼和存储流程；旧队列和回执不改、不自动重放。依据见 [ADR-0054](../adr/0054-explicit-memory-input-role.md)。

2026-11-10 模型配置修复：原 `model_probe._models_request` 仅判断已填模型，网页没有候选列表入口；复用既有 HTTP、URL 校验、凭证库和表单，增加只读有界模型列表 API 与选择控件。真实合成请求证明 64 token 探针会截断完整可用的 Anthropic 模型，因此调整预算和纯 JSON 提示，保留严格响应验证。兼容网关的列表认证重试仅限同地址 GET，不改生成协议。设计见 [ADR-0053](../adr/0053-model-catalog-and-probe-budget.md)。

2026-11-10 Anthropic 接入：补丁 0026 复用固定上游 LLMProvider 的限流、重试、JSON 和工具调用返回合同，以及 Core 已有 Messages 请求、模型配置/激活、用量审计和网页表单。原 SDK 仅有 OpenAI 生成后端，Core/UI 明确拒绝 Anthropic，因此增加协议转换和开放既有路径；不新增代理、提炼引擎、数据库或配置页面。默认继续使用内置本地向量；探测与生成共用严格响应解析。设计、兼容修复和未验证边界见 [ADR-0052](../adr/0052-anthropic-memory-provider.md)。

2026-11-10 MVP 每轮共享：继续直接复用固定上游 `one_memory.live_capture._messages` 对 UserPromptSubmit 和 Stop 的可见文字提取，不调用会依赖本机 EvidenceStore 或回退扫描 transcript 的完整 capture_hook。远程上传沿用 AgentSettings、prepare_event、LocalOutbox 和 send_pending；下一轮使用已有 history-list／ReadReply 与有限上下文呈现，无需 NAS 模型提炼。新增的工作目录绑定与稳定远程事件身份属于 Hub 的连接合同，不新建采集数据库、搜索引擎或上游补丁。设计见 [ADR-0050](../adr/0050-mvp-visible-turn-hooks.md)，实际测试结果另行记录，源码复用不作为宿主通过的证据。

2026-11-10 四域覆盖继续复用既有 23 个补丁，无新增上游补丁。Core 字节判别复用 `StrictDiagnosticFormatter` 与 `project_core_diagnostic`，真实样例由原生 `start_logging_to_file`、`DiagnosticLog.write/close` 产生；文件维护复用补丁 0025 的实际写者暂停和处理器租约。上游未提供 Hub 的父操作授权、版本化覆盖证明或无来源副本许可，因此这些边界保留在 Hub；无来源 Core 正文暂不执行物理删除，见 [ADR-0047](../adr/0047-core-file-coverage-and-physical-authority.md)。

2026-11-10 当前补充：补丁 0025 复用 SDK 已有文件 lease、Core 的 RotatingFileHandler 和独立诊断队列，增加真正的暂停/排空/失败重试及受管工厂接入；不新建日志后端。实际源码的 flush 不阻止新入队、configure/reset 忽略关闭失败、drop 行旁路 strict 投影，因此需要原生小补丁。旧默认行为及相关 SDK/Core 回归保留，修前反例、交叉审查和当前验证见[记录](../reviews/2026-11-10-runtime-binding-and-core-maintenance.md)。这不是 Core 历史正文或 Docker 日志已完成清理的证据。

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

## 2026-10-31 普通聊天合并输入增量

补丁 0013 在原生 `MemoryReconciler.reconcile` 的链头合并、去重和实际提示构造之后冻结候选；复用已有有界快照与持久结果，不复制检索、合并算法或修改模型调用。`Extractor.extract` 的 existing_tags 和 basic_profile_fields 仅为保留参数，固定源码未使用它们构造实际提示；中英文与配置组合实测用于防止凭形参虚构依赖。快照保存可变 MemoryNode 的当时 payload，尚不等于已证明生产版本来源，见 [ADR-0025](../adr/0025-chat-reconcile-evidence.md)。

## 2026-11-02 普通记忆生产版本增量

补丁 0014 复用 record_origin 的摘要/校验、MemoryNode 和 VectorStoreBase 的共同 payload，以及原生 writer 已有的旧节点读取。上游 custom.request_id 在 UPDATE 后仍保留创建请求，不能充当版本生产者，因此增加独立可空标记和三个后端共有的未标记语义更新失效规则。没有新增存储、模型或检索算法；对应本地真实后端、写入/重开和上游回归见 [ADR-0026](../adr/0026-chat-memory-origins.md) 与[验证记录](../reviews/2026-11-02-memory-origins.md)。

Hub 后续接线复用已有归档、产出索引与图预算，以及上游 `ChatMessage.from_dict`、`WriteRequest.get_flat_content` 和 `strip_tool_messages`；只增加归档表及来源适配，没有重做原始对话解析或提炼。实测 Core 的 `_hy_agent_id` 包含来源／日期／工作区，保持该既有隔离语义，验证范围见 [接线记录](../reviews/2026-10-30-chat-lineage.md)。

2026-11-03 普通记忆合并接线沿用补丁 0013/0014 的快照与版本摘要、原生 MemoryNode 格式和已有 Hub 原文/产出索引；抽取聊天归档的共有 SQL，复用回执缓存、历史关系与预算，未更改上游算法。上游缺少 Hub 项目/回执概念，故新增内部归档及图适配，见 [ADR-0027](../adr/0027-chat-reconcile-lineage.md) 和[验证记录](../reviews/2026-11-03-memory-lineage.md)。

补丁 0015 复用 agent/tools/basic_profile.py 的单次用户级查询、清洗、排序、差异、迁移及互斥；复用已有有界快照、来源字段及 SDK 持久结果。原画像旁路只在过程日志中给出新节点，缺少完整结果和部分失败证据，因此增加可选捕获与独立 profile 摘要域。不复制画像算法、不增加查询或模型调用，保留原返回合同和用户级范围。上游画像 meta、画像 API、payload-only、模型及持久回执回归见 [ADR-0028](../adr/0028-basic-profile-evidence.md) 与[验证记录](../reviews/2026-11-03-profile-evidence.md)。

画像 Hub 接线继续使用补丁 0015、原生 MemoryNode/from_dict、profile 来源摘要、画像清洗/链头/完整 KV 纯函数和向量库读取；抽取已有普通记忆证据校验与图遍历作为共享模块。只新增 Hub 作用域相关归档/索引及关系，不改变上游算法或旧产出摘要。对应 [ADR-0029](../adr/0029-basic-profile-lineage.md) 和[记录](../reviews/2026-11-03-profile-lineage.md)。

补丁 0016 复用 writer 的会话滚动摘要、图投影请求句柄和 SDK 终态回执流程。原会话摘要未保存任务句柄，可能在写入回执完成后继续写入；新增请求级句柄与共同收尾，保留并发并在取消后等待实际完成。不引入新任务队列、存储或摘要算法。上游 episode、写入结果、executor、图投影和并发门控回归，以及 Hub 准入和真实 SDK 重开探针见 [ADR-0030](../adr/0030-derived-task-lifecycle.md) 与[验证记录](../reviews/2026-11-04-derived-lifecycle.md)。

补丁 0018 复用 Core DurableWriteDrain 的领取、续租、执行及输出观察，扩展原锁和共享 worker 集合以支持暂停领取并等待完整任务结束。原 SDK 准入只覆盖分段调用，无法识别领取后尚未进入 SDK 或等待产出观察的任务；直接停止并等待主循环又会与维护回调中的 resume 自锁，故需最小生命周期适配。新增默认不启用的 claim-ready 回调，不改变来源权限或未知任务判定，也不新增队列。上游持久队列 187 项通过，补丁及真实并发测试范围见 [ADR-0034](../adr/0034-inflight-ingest-and-content-reimport.md)。

补丁 0019 复用 `MemoryTaskRunner.request_for`、原输入清洗和 imported-content 判路由，并在 `HyMemoryBridge` 两个既有生成入口及两个构造点增加可选的调用前许可。固定上游只有调用标记与事后结果，无法证明任务从未进入 SDK；因此需要 Hub 独立出生登记与持久见证。最终 SDK 实参由同一构造器产生，登记指纹与实际调用共用；未启用回调时保留原行为。独立入口 `import_sdk_*_exact`、update/delete 不属于这两个生成门，不能因此宣称所有变更都被覆盖。设计与兼容边界见 [ADR-0038](../adr/0038-durable-sdk-admission.md)。

补丁 0020 复用 Core 状态机与 `main_database` 原子写事务，只新增受信治理协调器可调用的精确比较取消与单任务终态正文清理；原 `settle`／unknown 查询规则不放宽，不增加 HTTP 接口。独立证据、签名范围与调用互斥由 Hub 提供，API 本身不宣称具备这些证明。2026-11-10 已接入治理 execute/resume，复用有界候选校验和原文验证，以签名 job/request/正文摘要限定调用；旧请求定位与终态恢复也共享这些预算工具，未另建队列或无副作用推断。真实成功和保护性拒绝的边界见[在途执行记录](../reviews/2026-11-10-inflight-execution.md)。

2026-11-10 副作用调查确认成功结果不覆盖 shadow-only UPDATE、链折叠、画像旁路及生产力 SQLite 先写后向量失败的残留。补丁 0021 首先复用 Chroma 五个变更入口、history 共同插入、SQLite 请求日志/episode 和原 SDK 生命周期，增加请求级有界观察。无需复制每个 writer 分支或引入第二个存储；上游没有完整持久覆盖证书，因此这一观察不直接授权删除。诊断缓存清理复用原 `Purger` 的精确请求定位，补齐原生两张日志表；没有重建日志后端。具体合同和剩余持久证据见 [ADR-0039](../adr/0039-sdk-write-effect-observation.md)。

同日后台生命周期调查实际复现：原 `drain_pipeline_log_tasks` 只扫描当前 loop 的任务，超时仅告警，外层任务取消后实际 JSONL/SQLite 线程仍可能落盘。0022 继续使用原日志后端和线程池，仅补充每 cache 的真实完成登记、暂停租约、严格排空和关闭重试；不重建日志队列。两个原生搜索入口复用已有共享 SDK 准入，保持并发。Hub 复用原 Chroma 只读预检并将其放到初始化前，保留无执行记录时的按需启动；设计、负对照及已验证边界见 [ADR-0040](../adr/0040-diagnostic-write-quiescence.md) 和[本轮记录](../reviews/2026-11-10-diagnostic-quiescence.md)。

后续内容治理只读调查（尚未实现）：0017 的非 debug 投影仍可能通过任意字典键、reason/error/source/owner、标识后缀字符串及伪造 redacted 占位符输出正文；text add 的 INFO 消息、异常格式化和直接 JSONL 调用也存在未过滤位置。固定 Hub/Core 没有直接返回 SDK pipeline 完整正文的 HTTP 接口，桌面流水线面板只需步骤/时间/计数/请求标识；SDK MemoryInspector 的原始接口则需保留默认兼容。可评估 Hub 专用的版本化封闭元数据投影，使 cache、文件与所有 Hub 输出处理器共用固定内容策略，独立于 debug 级别；SDK 自有、root/第三方及 Uvicorn 处理器分别安装，不能只给 root 加过滤器。

原生格式的合成实验另确认：JSONL 必须按字节 LF 划分，通用 Unicode splitlines 会误拆合法正文；直接替换文件后，旧 FileHandler 会继续向已断开的 inode 写入，需在排空后持处理器锁关闭/重开。独立读取 request 可在首个 user_id 标签下包含另一项目节点，不能以该字段或 memory_ids 交集授权删除整请求。未来文件清理须复用已有签名请求定位，同时区分完整回执与输入快照所属请求，保留同文件中未选记录字节，并覆盖仅日志恢复；这些观察尚不构成完整文件治理实现。

0023 实施上述新输出策略：复用原 wrapper、直接 SQLite/JSONL 入口、SDK 三个 handlers、Core 结构化诊断与文件处理器，在固定入口使用同一封闭元数据投影；不新增日志存储，不改业务正文/模型调用。原有内容开关和 0017 不能防自由字段及异常泄漏，故需要独立且不可降级的 Hub opt-in。SDK legacy 默认、原日志测试及 Core HTTP 日志兼容仍保留。新增真实文件/缓存、handler 并发、活动资源拒绝、初始化中途模块、异常及旧正文恢复负例；详情见 [ADR-0041](../adr/0041-strict-diagnostic-content-policy.md) 和[验证记录](../reviews/2026-11-10-diagnostic-content.md)。已有物理文件的定向清理仍是独立未完成项。

旧文件检查继续复用固定源码的 PipelineLogWriter、SingleLineFormatter、JsonLineFormatter 和 0023 的封闭投影知识；使用这些实际 emitter 生成合成正反例。上游没有治理范围内的具体记录授权与文件恢复协调，因此 Hub 增加只读 record/domain inventory，复用现有 inflight 请求证书并提取无 witness 更新的证明接口。不新增日志后端或上游补丁，不把 request 字段语法当逐行来源签名。物理记录的精确摘要、覆盖预算与后续恢复合同见 [ADR-0042](../adr/0042-diagnostic-file-governance.md)；当前前置接口不执行文件删除。

0024 继续复用固定日志 factory、原 handler 和轮转状态。真实实验证明：文件替换不更新已打开流，标准 handler 关闭后又可能自动打开旧路径；固定上游没有受控的同步维护与失败重绑定合同。因此只新增共享租约、失败封闭状态及流接管，原有 formatter/filters/level 保留。Hub 复用已有只读目录扫描、主签名意图、准入证明与存储租约，增加独立持久执行组件和 staging 恢复，不重建日志服务。原生 192 项、Core 39 项回归通过；范围与兼容性见 [ADR-0043](../adr/0043-diagnostic-physical-execution.md)。产品物理确认与完整恢复路由仍待接通。

## 2026-11-10 Core 历史文件合同只读调查

以下为固定上游加 0025 后的源码映射，未据此执行 Core 历史正文清理，也不是新增测试通过的证据。下一步沿 [ADR-0046](../adr/0046-governance-runtime-and-output-coverage.md) 扩展现有文件引擎，不另建清理后端。

| 实际入口 | 可复用与必须补齐的边界 |
| --- | --- |
| `one_memory/log_file.py` 的严格处理器 | 与 SDK 共用 `StrictDiagnosticFormatter`，可提取复用现有封闭字段、枚举、源码位置与安全栈校验；单凭 `content_omitted=true` 不能证明无正文。 |
| `hy_memory/utils/diagnostic_policy.py` 的 `project_core_diagnostic` | 结构化 Core 使用独立的 `schema_version/timestamp/service/component/event/outcome` 合同，需要独立封闭分类器；旧 details 中的任意短字符串不自动视为安全。 |
| `one_memory/diagnostic_log.py` 的旧 JSONL | 原物理行摘要必须以原字节计算；审阅及跨项目冲突检查复用原始/内嵌 JSON 解码视图，未找到其他项目标记不等于来源已证明。 |
| Core API 请求事件与旧 `core.log` | 真实事件可只有 trace/instance 标识，没有 SDK request ID；旧文本模板没有请求槽，且允许自由文本、多行和异常栈。不能伪造 SDK 请求身份，也不能按时间戳猜测续行归属；需要明确的无请求物理授权及有界分帧/审阅语义。 |
| `data/logs` 下的两个 Core 文件族 | `core.log`、`diagnostic-core.jsonl` 及各自 `.1/.2/.3` 与现有 SDK 日期轮转不同。扫描白名单、文件键、manifest、完成验证须共同版本化扩展，旧两域 verified 不升级为全部输出已覆盖。 |

可继续原样复用无符号链接目录遍历、文件身份快照、总预算、原字节保留、临时文件证明、同步替换、原处理器重绑定，以及本轮实际 Core 写者维护。新合同的重点是分类/分帧、物理授权、覆盖域版本和父操作归属；跨项目混合旧内容仍按未确认范围拒绝。
