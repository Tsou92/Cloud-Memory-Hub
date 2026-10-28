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
