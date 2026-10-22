# 上游补丁

基线由 [manifest](../manifest.json) 固定。保留上游许可证和原有测试；每个补丁说明触发条件、原因及验证结果。

## 0001-align-live-capture-test-double.patch

- 类型：仅测试兼容修复，不改生产代码。
- 原因：`IdentityCheckingSupervisor.process_records` 将 `source_admitted` 设为必填且断言为 true；实际 `MemoryProcessingSupervisor.process_records` 默认 false，durable queue 不传该参数，保留来源权限检查。
- 原版复现：67 项定向测试中 66 通过、1 失败；独立调用替身复现 `TypeError: missing 1 required keyword-only argument: source_admitted`。
- 修复：替身采用与生产接口一致的默认 false，并断言未跳过来源授权；保留身份合并和队列完成断言。
- 复测：同一 67 项定向测试全部通过。尚不代表上游完整测试集通过。

不得通过给生产调用添加 `source_admitted=True` 来消除这个测试错误，这会改变权限校验行为。

## 0002-align-retired-permission-tests.patch

- 类型：仅测试合同更新，不改生产实现、数据或授权。
- 错误码证据：当前 `MemoryProcessingSupervisor._active_credentials` 和 `_check_backfill_context` 均使用 `model_profile_changed`；测试仍期待已不存在的旧名称。保留三个并发变更场景、一次凭证读取和未创建 bridge 的断言。
- 旧授权表证据：migration 054 明确保留可能已发送的标记后删除 `memory_task_continuous_permissions`。该表不能继续作为时间窗口豁免；改测旧表无法绕过当前窗口。完整历史的无写锁读取改用已有、明确的 `all` 窗口对应的 `None`，保留旧记录可调度、任务未变更及单次只读连接的断言。
- 复测：两个测试文件加 migration 054 回归共 25 项通过。曾因额外状态读取先于连接次数断言而产生一个测试观测错误，已调整观测顺序后复测。
- 扩大到整个迁移文件时仍有六个既有失败，涉及退役表、移除的迁移号和旧列结构，尚未修复；不能将这 25 项通过视为完整上游回归通过。

2026-10-22 上游新提交已更新错误码并移除旧窗口模块，正在独立评估新基线。此补丁只适用于当前固定提交，升级时不能直接沿用。
