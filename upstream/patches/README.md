# 上游补丁

基线由 [manifest](../manifest.json) 固定。保留上游许可证和原有测试；每个补丁说明触发条件、原因及验证结果。

0001／0002 只适用于历史提交 `0244ebe38ce0288a44a8b6fc7f209ab2bf6c3766`，文件保留用于复查旧结果，不自动应用到新版本。当前 SDK 权威存储基线应用 0003／0004；升级依据见 [ADR-0003](../../docs/adr/0003-sdk-authority-baseline.md)。

## 0004-show-sdk-library-on-first-open.patch

- 适用：`b7d9f0e0f417eae30b192882f848b432484c20a6`；类型：页面兼容修复及三项回归。
- 触发：0003 修正状态后，默认简化记忆页仍显示旧地图，而其读取钩子在 SDK 模式下已关闭；页面因此永久显示正在读取。新版库组件存在但被隐藏在旧管理视图后面。
- 修复：SDK 简化入口默认显示现有 `SdkMemoryLibrary`，提供记忆库／问记忆／历史对话标签；旧查询输入不在 SDK 列表下冒充有效功能。复用现有 SDK 问答和原文组件，只在对应页面激活读取。旧库模式及完整模式的原有问答保持可用。
- 复现与复测：新三项测试在原版全部失败；修复后连同 SDK 列表、原记忆空间和旧库治理共 109 项通过。首次改法遮住了完整模式的问答，经原有测试发现后调整；最终保留原问答断言，不删除或放宽它。
- 容器门禁：`make check-web` 在固定 Linux amd64／Node 镜像中构建并运行上述组件及 SDK 原文回归，不读取日常客户端配置。

## 0003-report-fresh-sdk-authority.patch

- 适用：`b7d9f0e0f417eae30b192882f848b432484c20a6`；类型：一行运行时兼容修复。
- 触发：全新数据库已有 `memory_authority_state.authority=sdk`，且按照上游设计不创建旧迁移表。`/api/v2/memory-authority` 却只看旧 cutover 记录，返回 legacy；网页因此访问被服务端拒绝的 v1 记忆接口。
- 修复：调用已有 `SdkDataPlaneMigrationStore.authority()`，复用其新状态优先、旧迁移记录回退的规则；不创建虚假迁移标记，不改变数据库、迁移和权限。
- 复现：新隔离容器网页显示 `sdk_memory_authority_required`。扩展 Hub 真实 Core 集成测试后，在原版上明确断言失败（期望 sdk，实际 legacy），修复后状态、空记忆列表及首页计数均通过。
- 防止遗漏：容器启动前后都验证上述实际 API。完整原版上游测试通过不足以发现该问题，因为其 `conftest.py` 默认把应用用例放到旧库结构，只有显式 `fresh_sdk` 用例使用新结构。
- 本地依赖以 editable 方式引用经验证源码，避免 uv 复用旧 wheel 让补丁没有生效；Docker 仍以 `--no-editable` 构建自足安装包。

## 0001-align-live-capture-test-double.patch

- 类型：仅测试兼容修复，不改生产代码。
- 原因：`IdentityCheckingSupervisor.process_records` 将 `source_admitted` 设为必填且断言为 true；实际 `MemoryProcessingSupervisor.process_records` 默认 false，durable queue 不传该参数，保留来源权限检查。
- 原版复现：67 项定向测试中 66 通过、1 失败；独立调用替身复现 `TypeError: missing 1 required keyword-only argument: source_admitted`。
- 修复：替身采用与生产接口一致的默认 false，并断言未跳过来源授权；保留身份合并和队列完成断言。
- 复测：同一 67 项定向测试全部通过。尚不代表上游完整测试集通过。

在该旧提交中，不得通过给生产调用添加 `source_admitted=True` 来消除这个测试错误，这会改变权限校验行为。新基线的队列已经调整为接纳后调用，必须按新合同验证，不能沿用此旧断言。

## 0002-align-retired-permission-tests.patch

- 类型：仅测试合同更新，不改生产实现、数据或授权。
- 错误码证据：当前 `MemoryProcessingSupervisor._active_credentials` 和 `_check_backfill_context` 均使用 `model_profile_changed`；测试仍期待已不存在的旧名称。保留三个并发变更场景、一次凭证读取和未创建 bridge 的断言。
- 旧授权表证据：migration 054 明确保留可能已发送的标记后删除 `memory_task_continuous_permissions`。该表不能继续作为时间窗口豁免；改测旧表无法绕过当前窗口。完整历史的无写锁读取改用已有、明确的 `all` 窗口对应的 `None`，保留旧记录可调度、任务未变更及单次只读连接的断言。
- 复测：两个测试文件加 migration 054 回归共 25 项通过。曾因额外状态读取先于连接次数断言而产生一个测试观测错误，已调整观测顺序后复测。
- 扩大到整个迁移文件时仍有六个既有失败，涉及退役表、移除的迁移号和旧列结构，尚未修复；不能将这 25 项通过视为完整上游回归通过。

2026-10-22 上游新基线已更新错误码并移除旧窗口模块，其完整 `core-pr` 回归通过；此旧补丁停止应用。

## 0008-typed-output-provenance.patch

- 固定 SDK client 在选择生产力 writer 后，将实际 working／coding variant 附加到返回结果及原有 write_outcome。字段为追加兼容，不改变分类、提炼或存储路径。
- Core 增加默认关闭的输出观察回调，在 SDK 返回后、completed 提交前调用；观察失败沿用 unknown，不另建重试或状态机。
- 实际执行测试发现两个 drain 入口把已退役的 evidence_store 传给 SDK 成功后的 release_sdk_staging，导致 RetiredAuthorityAccess；改用已有 memory_write_evidence_store，保留原 staging 清理。
- Hub 必须同时从固定上游源码构建 SDK 和 Core，不能只应用源码补丁却继续安装未带补丁的 PyPI SDK。
- 保留原队列／bridge／SDK 路由回归；Hub 实际 drain 测试包含提交失败、恢复、类型冲突和重启。详细结果见[关联验证](../../docs/reviews/2026-10-28-output-lineage.md)。

## 0017-content-free-logs.patch

此为历史过滤实现，不能构成完整无正文保证：后续已复现自由键、标识后缀、伪占位符及异常路径泄漏。Hub 严格策略见 0023／ADR-0041；0017 继续保留为 SDK legacy 行为的一部分。

- 实测上游在 INFO 级别把完整用户消息（`[add] messages detail`）、检索查询（`[search] target/query`、`[read/hybrid] query`）写入按天滚动、保留 15 天的文本日志；JSONL 管线日志的 `parsed` 字段（EXTRACT、S1_L1_UPSERT、RECONCILE_APPLY、READ_*）也含正文。这些副本无法按记录精确删除，与彻底删除冲突（ADR-0032）。
- 复用上游已有的 `MEMORY_LOG_LLM_CONTENT` 开关与 `redact_content`：开关关闭（MemHub 非 debug 级别的默认）时，文本日志位置只记录长度和截断摘要；`parsed` 保留 JSON 结构、ID、计数和枚举字段，只替换自由文本值。debug 级别仍保留完整内容供诊断。
- 不改变日志级别、行格式、文件位置或任何业务路径。实测脚本在接纳、提炼、检索、删除全流程后扫描日志目录，确认无合成标记。

## 0021-sdk-write-effect-observation.patch

- 复用 Chroma 五个原生变更入口、HistoryStore 的共同 insert、SqliteCache 请求日志和 episode 状态，以及 SDK 既有写入结果和派生任务生命周期。请求内观察尝试过的定位信息，保存为追加字段 `write_effects`，不复制正文。
- batch 从原实现的有效节点列表观察，不提前消耗调用者迭代器；异常、取消及部分写入保留 attempted 项。`returned` 仅表示原函数正常返回，包括返回 false 的情形，不是成功落盘证明。
- 身份、selector、条目数和实际 JSON 字节有界；上下文按请求隔离，结果返回深拷贝，旧回执重放不补造观察。仅在新请求中增加字段，不改变已有回执和数据库版本。
- 始终标记 `observation_only=true`、`all_effects_proven=false`；图、生产力、诊断文件和未收尾后台日志显式说明缺口。缓存被恢复、硬退出或所有持久副作用的完整证明不由此补丁解决，不能据该字段放行治理。见 [ADR-0039](../../docs/adr/0039-sdk-write-effect-observation.md)。

## 0022-diagnostic-write-quiescence.patch

- 复用原 pipeline wrapper、JSONL writer、SQLite cache、executor 与 runtime。新增每 cache 的诊断工作组，在派发前登记，分别等待 owner loop 的实际任务、原生线程 future 和结果消费；取消等待者不等于后台 I/O 已结束。
- 维护先暂停全部关联工作组，再严格等待实际完成；超时或取消保留未完成任务并恢复本次暂停租约。暂停期间新日志返回未接纳、不缓存正文；成功关闭后永久封闭工作组。
- Core 两个搜索入口加入共享准入，保留读取并发；每桥关闭等待搜索。SDK/runtime 与 Core 引用链在关闭成功后释放，失败保留对象以供重试，不再先删除注册条目或吞掉关闭错误。
- 不改数据库、删除选择器、签名或一次性许可；`write_effects` 仍是观察。未启用 Hub 准入时普通 SDK 路径保留原行为，但诊断关闭失败现在明确报告。详见 [ADR-0040](../../docs/adr/0040-diagnostic-write-quiescence.md) 和[验证记录](../../docs/reviews/2026-11-10-diagnostic-quiescence.md)。既有文件正文和其他后台域仍待后续治理。

## 0023-strict-diagnostic-content.patch

- Hub 显式启用进程级 strict-v1，普通 SDK 保持 legacy 默认。复用原 wrapper、JSONL、SQLite、Core 诊断 writer 和 Python logging，只增加共用封闭投影、受控 formatter 与资源激活前置。
- 自由正文、任意键及身份字符串不进入诊断；保留规范请求 UUID、步骤、数值计数、耗时和闭集状态。debug 与内容策略独立，环境变量不能降级。诊断缓存、直接文件调用、memory-operation、metrics 合并及 Core 独立文件出口分别保护；不投影业务原文、模型实参或持久结果。
- 旧活动资源必须先结束；handler 创建、挂载与严格激活协调，不能让后台旧 I/O 或新挂载的旧 formatter 在激活后漏出正文。Hub 接管进程日志配置，调用者 handler 分离但保留对象，不关闭其文件。
- 兼容、失败、性能及真实场景验收见 [ADR-0041](../../docs/adr/0041-strict-diagnostic-content-policy.md)。没有在本补丁清理旧物理日志、Docker 旧 stdout 或独立读取副本，不据此宣布彻底删除完成。

## 0024-diagnostic-file-handlers.patch

- 复用现有日志 factory、handler、formatter、轮转与 strict policy 锁，新增同步维护租约、受控关闭和按原配置接管新流。Hub 文件执行器负责精确授权、持久 staging 与真实字节检查；上游不增加治理数据库或删除选择算法。
- 原生 FileHandler 在文件替换后仍持有旧 inode，关闭后 emit 又可能自动重开，故仅替换文件或普通 close 不足以保证恢复。补丁让失败的受控处理器保持封闭，重试后绑定当前文件；未知调用者文件流与自定义轮转明确拒绝。
- 原生新增与相关回归 192 项、Core 39 项通过。合同与完整执行边界见 [ADR-0043](../../docs/adr/0043-diagnostic-physical-execution.md)；不据此宣称 Core 旧日志、Docker 历史输出或全部 purge 已清理。
