# ADR-0036：治理重试的请求身份与真实完成状态

日期：2026-11-09。状态：已实现，专项/全量测试、独立审查、本地及 NAS 安装验证通过，已包含在 cde427c。关联 FR-10、FR-11；补充 ADR-0032 的重试语义，不代替 ADR-0034 的存量队列清扫。

## 已复现问题

隔离真实 Hub/SDK/HTTP 验证了两个既有缺陷：同 operation_id 更换 action 或选择集合时返回原操作的 completed，新请求实际上没有执行；计划已写但日志追加前失败后，重试恢复会丢弃孤立计划，却仍返回 completed、数量 0，目标保持 active。跨项目冲突已有拒绝，不能据此推断完整请求幂等。

旧 Intent 只保存派生后的物理范围和规则，没有初始选择与 expected_view，不能从完整范围反推原始请求。不得将当前重试参数补签成历史事实。

## 复用和决策

1. 继续复用现有治理日志、ExecutionStore 的签名与原子计划保存，不增加请求数据库或迁移旧意图。新操作在 Intent.targets 加两个域隔离的 HMAC content 目标：请求策略标记和请求指纹。指纹绑定 actor、scope、action、规范排序的 record_ids 及 expected_view；operation_id 由 Intent 本身绑定。原 HMAC 带实例/项目密钥边界，不落正文或明文参数。
2. 此目标使用与 message/policy 不同的明确身份域，不参与内容匹配；物理记录、来源/内容规则及两个请求目标合计仍受 10,000 上限约束，超限在落计划前返回 governance_scan_limit。suppressed_count 仍只统计物理记录。
3. 已知 operation_id 首先核对 scope/action，再验证请求指纹；不一致返回 409 governance_operation_conflict。旧意图没有当前请求策略标记时返回明确的 409 governance_operation_request_unproven，不伪造一致性。旧意图自动恢复仍沿用既有签名范围，不依赖该新指纹。
4. execute 和 resume 复用同一个治理串行锁，固定顺序仍为治理锁 → Drain quiesce → SDK/接纳独占。已持治理锁的 execute 调内部恢复逻辑，不能再次获取同一锁；也不能在 quiesce 内反向进入恢复锁。并发相同请求应只追加一次意图，并都返回该操作的真实结果。
5. 恢复后必须重新读取操作状态。若日志已提交且恢复成功，返回重新读取的 completed；若孤立计划被丢弃，按原 expected_view 重新核对并实际执行，或返回真实错误。操作不存在、仍 planned 或恢复失败时都不能返回 completed。

## 兼容与验收

不改数据库列和 Intent 协议字段，不重签历史。新增 content 目标只是签名身份域；内容规则仍只匹配其独立 message 域。旧版本不能提供这组新重试保证，回退需要另行验证，不能因能打开数据库就宣称安全。

测试必须覆盖：同请求丢失响应后重试；改变 action、选择、expected_view、scope 的冲突；旧操作无法证明请求时明确拒绝但仍能自动恢复；日志前失败后同请求真实重做；日志后失败再恢复；恢复失败不得报告成功；两个并发相同请求单次落日志；目标总预算和治理目录不保存正文。使用真实 HTTP、签名存储和 SDK 合成提炼，保留独立审查及现有治理回归。

新增 `test_governance_request_identity.py` 19 项通过，覆盖上述场景以及 actor 冲突、选择排序、重复取消线程等待、9998+2/9999+2 目标预算和内容身份域隔离。独立审查未发现新缺陷；恢复后重读状态及锁顺序均按本设计落实。[安装记录](../reviews/2026-11-09-chroma-purge-sequence.md) 包含本地/NAS 的相同与冲突请求重试、重启及恢复验证，仍不能代替真实客户端验收。
