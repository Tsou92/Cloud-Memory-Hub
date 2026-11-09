# 在途任务有界候选清单方案

日期：2026-11-09。状态：只读设计，尚未实现或验证。关联 FR-10、FR-11、ENG-01；承接 [ADR-0034](../adr/0034-inflight-ingest-and-content-reimport.md)、[来源规则](../adr/0033-durable-source-suppression.md) 和 [治理请求身份](../adr/0036-governance-request-identity.md)。

## 目的与非结论

新增一个只读观察步骤，在治理写入计划之前确定：哪些队列任务属于本项目、哪些对应已确认原文、哪些仅命中来源或内容规则，以及哪些任务无法确认归属。恢复路径复用同一观察步骤。

本方案不取消任务、不清理正文、不改任务状态，也不判断 SDK 是否已经产生副作用。`hy_invoked=0`、任务状态、错误码、结果缓存缺失、`outcome=None` 或结果为空，一概不能作为无产出证据。旧 queue 与新 SDK、旧 cache 与新 vectors 混合恢复时，这条边界同样成立。

## 内部范围输入与调用顺序

建议接口：

```python
collect_inflight_candidates(
    ingest,
    scope: ConfirmedScope,
    *,
    budget: CandidateBudget,
) -> CandidateInventory
```

`ConfirmedScope` 是仅供服务内部构造的只读值，不接受客户端反序列化。建议字段为 `scope_id`、`action`、`records`（物理 ID 到 kind 的映射）、`rule_targets`、`basis`，以及适用时的 `view_digest`、`operation_id`。它有两种合法来源：

| 路径 | 范围依据 | 观察时机 |
| --- | --- | --- |
| 新 execute | 在治理独占区内重新读取权威 inventory，验证 expected_view，再经 `plan_relations` 和 `capture_rules` 得到的完整范围 | `ExecutionStore.plan`、日志 append 之前 |
| 恢复已有操作 | 现有治理日志和 ExecutionStore 验证后的 intent、签名物理 records 及适用规则 | 沿用既有操作恢复流程，不能重签历史或替换其范围 |

客户端原始 `record_ids`、自行拼装的 cmhe 列表或未校验 Intent 都不能直接成为观察依据。新执行不要求预先存在已持久化的签名操作；否则必须先写 planned 才能发现 blocker，会把一次可拒绝的请求变成持久恢复负担。

沿用锁顺序：治理串行锁 → Drain quiesce 并等待完整 worker → SDK／接纳独占 → 重算范围 → 观察候选。线程内按 remote-access → Core 数据库顺序建立只读快照。接纳线程必须已按现有 `_joined_thread` 合同结束，不能只等待其 HTTP 调用者返回。

新 execute 若发现 blocker、归属覆盖不完整、规则命中但未确认范围或预算超限，应在落计划前拒绝并释放本次锁，保留原现场。不得为了表示失败而写入 planned、追加意图或永久关闭全实例写入。恢复路径遇到同样问题则保留原有未完成操作，不伪造 completed；本方案不改变既有恢复隔离语义。

## 确定性枚举与查询

复用现有 `MAX_RECORDS=10_000` 和 `MAX_CONTENT_BYTES=32 MiB` 量级的数量／字节预算。来源、回执、队列元数据及实际读取的正文均有计数；累计读取内容受字节预算约束。重复 job 只生成一个候选，但真实发生的正文读取不能隐去预算消耗。查询始终先取身份与长度，检查预算后才读取正文。

### 1. 项目来源目录

```sql
SELECT source_id, device_id, scope_id, adapter_id, status
FROM cmh_sources
WHERE scope_id = ?
ORDER BY source_id
LIMIT ?;
```

LIMIT 使用剩余数量预算加一；出现额外行即拒绝，不截断后声称完整。来源状态包含 active 和 revoked：撤权不抹掉历史所有权，不调用当前写入授权回调来过滤历史任务。

### 2. 本项目回执

```sql
SELECT receipt_id, request_id, device_id, event_id, source_id,
       scope_id, conversation_id, record_id, revision, payload_digest,
       length(CAST(payload_json AS BLOB)) AS payload_bytes
FROM cmh_event_receipts
WHERE scope_id = ?
ORDER BY rowid
LIMIT ?;
```

利用现有 `cmh_original_scope` 索引。先把回执身份与来源目录交叉核对，累计长度；通过后按 receipt_id 和 scope 精确读取 payload_json，复用 `validate_original`。

### 3. 来源对应的非终态任务

来源键为 `cmh:<source_id>`，每批绑定参数不超过 500 个，复用现有来源队列索引：

```sql
SELECT job_id, idempotency_key, source_key, state, hy_invoked,
       queue_seq, updated_at,
       length(CAST(payload_json AS BLOB)) AS payload_bytes
FROM memory_write_jobs
WHERE source_key IN (...)
  AND state NOT IN (...)
ORDER BY source_key, state, queue_seq, job_id
LIMIT ?;
```

终态集合必须取 Core 的状态常量，不在 Hub 另写一份执行状态机。未知状态不会因为不在白名单而被漏掉；不应由 Hub 正常接纳产生的 assembling 等状态须作为待核验项，不能自动处置。

再按 ConfirmedScope 中 cmhe 的 job_id，以及已校验回执的 receipt_id／request_id 精确补查队列元数据，不受状态过滤。以 job_id 去重，同时检查 request_id 与 job_id 的对应关系。此步骤覆盖已确认原文 purge 后仅恢复旧 queue，以及确认范围内的终态残留身份；没有找到行只表示当前队列不存在该行，不表示 SDK 未写入。

### 4. 有界的无归属任务元数据审计

仅枚举已知来源不能证明覆盖完整：旧 queue 可能带回没有 cmh_sources 项、没有回执、也没有签名 cmhe 锚点的任务。必须另做以下元数据审计：

```sql
SELECT job_id, source_key, state
FROM memory_write_jobs
WHERE source_key GLOB 'cmh:*'
  AND state NOT IN (...)
ORDER BY source_key, state, queue_seq, job_id
LIMIT ?;
```

该查询使用大小写精确的 Hub 来源前缀；不读取 payload_json、evidence 正文或 SDK 数据。结果受现有队列数量预算约束；取回列的长度同样计入元数据预算。对出现的来源 ID 分批执行目录精确查找：

```sql
SELECT source_id, device_id, scope_id, status
FROM cmh_sources
WHERE source_id IN (...);
```

- 已有目录且归属其他 scope：只确认其归属存在，不读取任务正文、不纳入本项目候选。
- 缺失目录或来源键不符合原身份格式：不得读取 payload 来猜 scope。返回无归属 blocker，标记 ownership coverage 不完整。
- 元数据审计超过预算：拒绝完整性结论，不把尚未检查的行视为无孤儿。
- 对外只暴露固定原因与数量；内部诊断也仅保留经格式校验的身份，不输出任意损坏字段。

这里审计的是所有可能继续执行的 Hub 队列行；确认范围内的终态行由精确锚点补查。它不是全库物理副本清理或任意终态孤儿归属调查。未知状态仍在审计范围内。

跨数据库不需要新建 ATTACH 写入通道：在两个已有只读连接中分批查元数据并交叉核对即可。数量和字节预算不等于数据库执行时间预算；SQL progress handler、额外索引及执行期限属于后续性能验证建议，本方案不引入未经实测的 deadline 默认值。

## 所有权与原文校验复用

优先从 [Purger._requests](../../apps/hub/src/cloud_memory_hub/governance_purge.py) 抽取纯元数据校验函数，供删除和候选观察共同调用，而不是复制另一套归属规则：

```python
verify_request_owner(job, source, *, instance_id, scope_id) -> RequestIdentity
```

检查来源键与目录 ID 一致，目录 scope 一致，request_id 严格为 `cmh:<当前实例>:<来源设备>:<规范 event UUID>`。有真实回执时还须检查 job_id、request_id、设备、事件、来源和项目全部对应。目录存在只能证明该来源所属项目，不证明任务正文未被替换。

正文校验分两种：

1. 有真实回执：复用 [validate_original](../../apps/hub/src/cloud_memory_hub/original_archive.py)，再用 [write_request](../../apps/hub/src/cloud_memory_hub/remote_ingest.py) 重建 frozen 请求。沿用 `_recover_original` 已有的 `event_at`、`time_quality`、`priority` 比较豁免，不扩大豁免集合。
2. 没有回执：从 `_recover_original` 抽出“严格末尾 footer、逐消息相同事件头和序号、EventEnvelope、write_request 回建相等”的纯函数。重建事件必须再次与已确认的目录和队列身份相等。结果标记为 `queue_only`，不能构造假回执再用 `validate_original` 自证已接纳。

无回执可能来自 purge、接纳事务回滚或恢复差异；单凭缺回执不能判定原因。signed cmhe 是历史确认范围的身份锚点，不能代替现存 frozen 正文的一致性校验。非终态 payload 缺失、损坏、被 reaped 或回建不一致时保留 blocker，不按空内容处理。

## 规则匹配、确认边界与返回值

直接复用 [source_target、content_targets](../../apps/hub/src/cloud_memory_hub/governance_sources.py)。来源规则匹配稳定三元组，不含 revision；内容规则沿用当前 NFC／空白规范化、至少 16 字符、任一消息命中策略。仅消息身份域参与内容匹配，不能把 ADR-0036 的请求指纹等其他 content 目标当作消息规则。

旧操作的内容策略标记或规则覆盖不足时必须明确报告，不能将无法判断当作没有命中，也不能从当前请求补签历史规则。

建议返回以下只读结构；不返回正文：

```text
CandidateInventory
  confirmed_original_candidates[]
  rule_only_candidates[]
  blockers[]
  ownership_coverage: complete | incomplete
  usage: sources, receipts, jobs, orphan_metadata_rows, payload_bytes

Candidate
  job_id, request_id, source_id, device_id, scope_id
  state, hy_invoked, source_status
  receipt_presence: present | absent
  event_proof: accepted_original | queue_roundtrip | unavailable
  conversation_id, record_id, revision  # 仅在事件已校验时提供
  reasons: confirmed_cmhe | source_record | content
  input_digest                         # 内部重查绑定，不作为无副作用证明
  sdk_effect: not_checked
```

分类规则：

- `confirmed_original_candidates`：对应的 cmhe 物理 ID 已在内部 ConfirmedScope.records 中。新执行由权威 inventory／预览一致性证明，恢复由已验证签名范围证明。
- `rule_only_candidates`：来源或内容规则命中，但对应 cmhe 不在该物理范围。即使同来源三元组匹配，也不能自动当作已确认 revision。
- `blockers`：归属、事件正文、状态、规则覆盖或预算无法确认。缺正文时即使已有 cmhe 锚点，也必须保留其无法校验的状态，不能把身份候选解释为可执行任务。

同来源版本关系通常会让各 revision 进入预览；如果观察发现额外 revision，仍属于第二组。已接纳的换 ID／换来源事件命中内容规则，也属于第二组。必须扩展预览并重新确认，或返回 409 要求重新预览，不能静默扩大物理删除范围。本方案不自行决定是否允许取消这组任务。

只有元数据审计完成、所有候选的归属核验完成，ownership coverage 才可标记完整。这仍然只表示队列归属观察完整，不表示全部 SDK 产出已发现、没有副作用或删除已经完成。

## 最小验证矩阵（待实现）

| 场景 | 预期观察 |
| --- | --- |
| 新 execute 尚无持久 operation | 可以使用独占区内构造的 ConfirmedScope；成功观察前不落计划 |
| 客户端直接提交 cmhe／伪造内部范围 | 不能成为可信输入 |
| 同来源各 revision 均已确认 | 第一组，去重稳定 |
| 新 revision 或同内容换 ID／换来源未在确认范围 | 第二组，要求扩展预览／409，不静默清理 |
| 相同内容属于其他 scope | 不读取正文、不加入本项目候选 |
| purge 后仅恢复旧 queue | 通过 cmhe 锚点和目录／请求身份定位；无回执不冒充 accepted |
| queue.submit 后接纳事务回滚 | 通过来源枚举发现，验证 frozen 请求，标记 queue_only |
| 来源 revoked | 仍能确认历史所有权，不因当前写权限被过滤 |
| 来源缺失且无回执／无签名锚点 | 元数据审计发现；不读 payload；coverage 不完整；新 execute 在 plan 前拒绝 |
| 来源存在但属于其他 scope | 元数据确认归属后排除，不读取正文 |
| 设备／实例／scope 冲突，回执摘要或 footer 损坏 | blocker，保留现场 |
| 非终态 payload 缺失、被 reaped、回建不一致或状态未知 | blocker，不当作空任务 |
| 同一 job 被来源、回执、cmhe 多次定位 | 单一候选，保留全部匹配理由 |
| 数量／字节预算超限或元数据审计中断 | 不交付完整覆盖结论，不写 planned 或追加意图 |
| 旧规则缺当前内容策略证明 | 明确规则覆盖不足，不宣称无内容命中 |
| hy_invoked=0、未知状态、SDK 缓存缺失或新旧存储混合恢复 | 不据此判断无副作用；sdk_effect 始终 not_checked |
| 恢复路径已有未完成操作且发现 blocker | 保留原状态和现场，不重签或伪造 completed |

本文件没有对应产品实现，矩阵也未执行；仅复用现有源码合同提出最小观察步骤。后续实现须补充真实队列、签名范围和两库恢复组合的专项证据，再讨论取消与 SDK 产出恢复。
