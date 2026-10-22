# 上游补丁

基线由 [manifest](../manifest.json) 固定。保留上游许可证和原有测试；每个补丁说明触发条件、原因及验证结果。

## 0001-align-live-capture-test-double.patch

- 类型：仅测试兼容修复，不改生产代码。
- 原因：`IdentityCheckingSupervisor.process_records` 将 `source_admitted` 设为必填且断言为 true；实际 `MemoryProcessingSupervisor.process_records` 默认 false，durable queue 不传该参数，保留来源权限检查。
- 原版复现：67 项定向测试中 66 通过、1 失败；独立调用替身复现 `TypeError: missing 1 required keyword-only argument: source_admitted`。
- 修复：替身采用与生产接口一致的默认 false，并断言未跳过来源授权；保留身份合并和队列完成断言。
- 复测：同一 67 项定向测试全部通过。尚不代表上游完整测试集通过。

不得通过给生产调用添加 `source_admitted=True` 来消除这个测试错误，这会改变权限校验行为。
