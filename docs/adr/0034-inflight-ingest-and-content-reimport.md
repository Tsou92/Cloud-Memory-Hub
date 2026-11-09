# ADR-0034：在途接纳与换 ID 同内容重导入

日期：2026-11-09。状态：分步实现中；Q1/Q2 已由用户确认，内容接纳规则和完整 worker 等待已实现并定向验证，存量队列清扫及版本范围仍未完成。2026-11-09 修订一：按 T-034-1 实测（提交 c7f9ba4）更正 E3，重写决策 1 第 2、4 条与启动就绪策略，见「修订一」。2026-11-09 修订二：按 T-034-2a 实测（提交 932279a）更正方案 X 的判据，重写 Q5 选项，新增 Q7，见「修订二」。关联 FR-10、FR-11；补充 [ADR-0032](0032-governance-execution.md) 与 [ADR-0033](0033-durable-source-suppression.md)；对应[验证记录](../reviews/2026-11-09-source-suppression.md)「下一步」第 1、2 项。第 3、4 项（结果缓存清理依据、同 operation_id 冲突）不在本文范围。

## 当前实施修订（2026-11-09）

以下实施约束优先于后文保留的历史候选方案。Q1/Q2 已确认；Q3 沿用无法证明则拒绝的兼容策略。Q5–Q7 中不能证明无写入就终止 unknown 的候选不可采用：d3d3125 已实测旧 cache 与新向量库混合恢复时，结果行不存在但 raw 仍存在。因此保留无法判定任务及现场，不据错误码或空结果将其改为 dead。范围变化必须重新预览确认。

### 先排空完整 worker，再进入治理独占

固定上游 b7d9f0e 加既有补丁的 `memory_server/durable_queue.py` 已负责 claim、续租、执行、产出观察和结算。新增最小可选补丁，复用其 worker 生命周期，不另建任务队列。

1. `DurableWriteDrain.quiesce()` 复用 `_run_lock`，暂停新 claim 并等待已登记 worker 的完整 `_run_claimed_job` 结束；治理期间持有同一锁。`run_one` 与 `run_forever` 必须共用该入口。
2. claim 在线程内完成；取消 await 不会停止线程。因此 shield/join 的最小单元必须包含「claim 返回到注册全部 worker」，不能仅等待 claim 后丢弃其 IDs。共享 worker 集合从注册到执行、产出观察及续租收尾完成期间持续保存。
3. 取消 quiesce 等待者不取消 worker；主循环退出也等待共享集合。不得等待 Drain 主任务，因为其维护回调会直接调用 Hub resume，存在自锁。
4. Core 增加默认不启用的 claim-ready 回调，与原来源 claim 授权同时检查。启动恢复成功前只关闭 claim；不改变已领取任务中途的来源授权，否则已实测会产生维护型 unknown。
5. Hub 锁顺序固定为 resume 锁（仅恢复路径）→ quiesce → 等 worker 完成 → `SdkAdmission.exclusive` → 重算范围、记录规则、清扫和治理。execute 的历史重试若需 resume，必须在取得 quiesce 之前完成。
6. `/hub/v1/events` 的实际接纳进入共享 `SdkAdmission.operation`，并复用 `_joined_thread(ingest.accept)`，保证断开或取消请求后仍等待接纳线程结束；治理接口不进入共享准入。先排空 worker 再独占，已接纳的线程先结束，新接纳返回明确维护 503。

该补丁先验证暂停与恢复合同；它本身不证明存量 unknown、旧队列恢复或同来源修订的清扫已完成。新增测试须运行真实 Drain 生命周期，覆盖线程内 claim 取消、等待者反复取消、run_one、产出观察未结束及主循环维护回调；既有 fixture 停止 drain 后直接调用 execute 的测试不能替代这些证据。

### 内容规则的审查修正

合法的超长转义字符消息会使 JSON 身份超过 journal 的长度上限。内容身份改为 `[1,"message",sha256(规范化文本的 UTF-8).hexdigest()]`，再交给原 `journal.target` 生成按实例和项目隔离的 HMAC；裸摘要只存在于计算过程，不写入治理库。策略标记使用 `nfc-whitespace-min16-any-sha256-v1`，尚未发布旧格式，无需迁移新格式。

旧 forget 的原文随后被新 purge 删除时，可复用带当前策略标记的签名操作规则，但必须证明这些操作的物理原文集合覆盖已删除原文；未被覆盖的其余原文继续校验并重建规则。部分覆盖且剩余原文缺失、无关策略标记或签名记录损坏不能放行；证明不足仍拒绝。

### 来源修订的范围实现约束（已实现，本地验证）

同一项目内经原文校验的 `(source_id, conversation_id, record_id)` 表示稳定来源记录，各 revision 都是它的历史版本。沿用 `RelationNode.versions` 与 `plan_relations`，按 revision 排序只连接相邻原文，避免全连接图；既有数量、正文和边预算继续适用。相同内容但身份不同的事件不冒充版本，本项不解决它们的清扫。

预览和实际执行使用同一版本关系，所以已接纳但尚未提炼的修订也进入预览，并参与原有 view_digest；预览后新增修订必须返回 409 重新预览。执行只能作用于用户最终确认的范围，不静默取消或删除未显示的修订。这里落实的是既有版本治理和“实际范围先预览”约束，不把 Q4 表中的历史备选当作新的用户回答。

先验证版本关系和原文可见范围，再接队列清扫。只扩展版本关系并不能证明 pending 已取消或旧队列恢复安全；剩余失败应按实际行为保留。

本地全量 743 通过、5 跳过、6 个在途缺口预期失败；独立审查和预算/范围专项见[来源修订记录](../reviews/2026-11-09-source-revisions.md)。本项尚未纳入 NAS 的 60ad0ce 制品。

## 证据（源码核对，未运行）

以下缺口最初由阅读源码得出；G-A、G-B、G-C 已由 T-034-1 复现（c7f9ba4，8 个 strict xfail，见「修订一」）。E3 原文有误，保留在表中供追溯，以「修订一」的 E3′ 为准。

| 编号 | 事实 | 位置 |
| --- | --- | --- |
| E1 | 接纳在线程池中持 `remote-access.sqlite3` 写事务，事务内先读规则（`require_source_allowed`）再 `queue.submit` 和写回执。执行持 `SdkAdmission` 独占区（只拦本进程 SDK 调用，不拦接纳），规则写在 `execution.sqlite3`。两者之间没有顺序保证。 | `remote_ingest.py` `accept`；`governance_execution.py` `execute`；`sdk_admission.py` |
| E2 | 记录级规则只在接纳时检查。队列 worker 调用 HY 前的检查 `_source_writes_on(source_id)` 只带 source_id，Hub 授权回调只看 `require_writes_ready`（操作未完成时才拦）、旧规则可用性和来源授权。 | 上游 `api.py` `admit_hy_invocation`；补丁 0005；`app.py` `source_write_authorizer` |
| E3（已更正，见 E3′） | ~~被拒的 worker 若尚未调用 HY，上游用 `release_unstarted` 把任务退回 pending，不丢任务~~ 只对两种错误码成立，维护锁占用时不成立；有 `settle(state="cancelled")` 终态。非终态任务的 `payload_json` 保存完整 WriteRequest（含正文），只有终态任务会被 `clear_terminal_payloads` 换成摘要。 | 上游 `durable_queue.py` |
| E4 | 彻底删除的字节核验 needle 是范围内节点内容；原文节点内容是回执 `payload_json` 整串，与队列中的序列化不同，因此队列中的正文副本不会被核验发现。 | `governance_purge.py` `run`/`_verify`；`output_lineage.py` `inventory` |
| E5 | 客户端的 record_id、conversation_id 由 outbox 密钥对本地标识做 HMAC 得到；source_id 来自每台设备×适配器的来源注册。重装客户端、重新注册来源、从其他 Agent 导出后再导入、同一段话出现在另一会话，三元组都会变，ADR-0033 规则不命中。 | `capture_store.py`；`remote_ingest.py` `create_source` |
| E6 | 治理记录的 TargetKind 已含 `content`，目前没有写入者；`journal.target` 已按实例、项目、类型做 HMAC 隔离。 | `governance_journal.py` |

据此推断的缺口：

- **G-A 已接纳未完成**：执行前已入队、尚未提炼的任务，在操作 completed 后会照常被提炼。忘记时，范围内原文会产出新记忆，而新记忆不在抑制表中，可以被召回；同来源的新修订（不在范围内）也会被提炼。彻底删除时，正文还留在队列行中，不被清理和核验；worker 之后调用 HY，lineage 找不到原文才报错，此时记忆已经写入。
- **G-B 并发接纳**：接纳读规则（还没有规则）→ 执行写入计划 → 接纳提交任务和回执。漏过的任务同 G-A。
- **G-C 恢复旧备份**：恢复的 `one-memory.sqlite3` 可能带回 pending 任务；`replay_purges` 只检查记录是否重现，不检查队列。
- **G-D 换 ID 重导入**：E5 的任一情形都能让已忘记或已删除的内容被重新接纳和提炼（FR-11「重新导入历史记录」）。

## 决策

### 决策 1：执行时清扫在途任务（解决 G-A、G-B、G-C；无需产品取舍）

1. 保持顺序：计划（规则在 planned 阶段即生效，ADR-0033）→ 追加意图 → `_finish`。`_finish` 第一步新增 `sweep_inflight(intent)`，忘记和彻底删除都执行。
2. （修订一重写）清扫在 Hub 接纳库的写事务内进行（`ingest._database(writing=True)`，即 BEGIN IMMEDIATE，与接纳用同一把锁）：列出该项目的全部回执，用该意图的规则（来源三元组；若 Q1 选 A，再加内容指纹）匹配，对命中的回执调用 `queue.get(request_id)`：
   - pending、waiting_for_permission、waiting_for_model，或 running 且 `hy_invoked=0` → `queue.settle(job_id, state="cancelled", error_code="governance_source_suppressed", hy_invoked=False)`。c7f9ba4 已实测四种状态都能无 owner 取消。
   - unknown，或 running 且 `hy_invoked=1` → 按「修订一」的方案 X 或方案 Y 处理（Q5），不再一律报 `governance_ingest_in_flight`。
   - completed、failed、dead、cancelled → 跳过。completed 的产出已在 SDK 中，由正常的范围预览处理，不属于清扫。
3. 为什么能关闭竞态：计划提交之后才开始的接纳会读到规则，返回 409；计划提交之前已开始的接纳一直持有接纳库写锁直到回执提交，清扫拿到同一把锁时一定能看到这条回执。锁顺序与接纳一致（remote-access → one-memory），不会死锁。
4. （修订一重写）启动清扫：`resume` 在开放写入前对全部操作（含 completed）各执行一次清扫。清扫完成前，Hub 的 `source_write_authorizer` 返回 False（就绪标志 `ready=False`）。原文写的「worker 按 E3 退回 pending」只在 worker 被授权拒绝时成立；如果 worker 先撞上维护锁，任务会变成 unknown。详细时序和约束见「修订一」的启动就绪策略。
5. 彻底删除额外两步：清扫在 purger 之前运行；purger 在 VACUUM 前循环调用上游 `clear_terminal_payloads()` 直到返回 0；字节核验的 needle 增加范围内原文的各条消息文本（≥16 字符，与现有阈值一致）。
6. 被取消的任务不自动重新提交。忘记时原文保留，用户仍可主动查阅。

### 决策 2：内容指纹规则（解决 G-D；是否启用及匹配口径待决定）

技术部分（无论 Q1、Q2 怎么选都适用）：

- 指纹源：执行时在独占区内，从范围内经校验的 cmhe 原文（与 `capture_sources` 同一次读取）逐条取消息文本。取隐私过滤后的文本，做 NFC 规范化、去首尾空白、连续空白合并为一个空格；规范化后不足 16 字符的消息不生成指纹。
- 存储：`journal.target(scope, "content", json.dumps([1, text]))`，与来源摘要一起放入同一签名 `Intent.targets`。不存原文、不存明文摘要，不新增表或列。`suppressed_count` 仍只统计物理记录。
- 检查点：与来源规则相同，在设备身份、来源授权通过之后、提交队列之前；同时纳入决策 1 的清扫匹配。
- 命中返回 409 `governance_content_suppressed`（新错误码，与来源命中区分，便于管理员排查）。

## 修订一（2026-11-09，按 T-034-1 实测）

依据：提交 c7f9ba4 的 `apps/hub/tests/test_governance_execution.py`（QA 复核报告在仓库外，不链接）。上游行号对应固定提交 b7d9f0e 的 `memhub/apps/core/src/one_memory/` 打完补丁后的本地检出 `.local/upstream/hy-memory`。

### E3′：维护锁占用时的实际处置

`test_inflight_worker_during_sdk_exclusive_records_actual_outcome` 的实测结果：另一个 Task 持有 `cmh_sdk_admission.exclusive()` 时执行 worker，SDK `add` 没有被调用，任务最终为 `state=unknown`、`detail=hy_memory_maintenance_in_progress`、`hy_invoked=1`，不是 pending。`get_sdk_write_outcome(request_id) is None`，`cmh_reconciler.run()` 返回 0，任务仍为 unknown。

原因：`execute_job` 的异常分类（`durable_queue.py:2425-2487`）只对 `model_content_source_disabled`、`model_sedimentation_disabled` 且未调用 HY 的任务调用 `release_unstarted`（:2437），退回 pending；其他未识别错误码在 :2475 强制记为 `UNKNOWN, hy_invoked=1`。`hy_memory_maintenance_in_progress` 不在任何白名单中（`POLICY_STOPPED_CODES` 见 `memory_tasks.py:64-71`，以及 `durable_queue.py:235-263` 的三个集合）。

另外，源码中的 `hy_memory_source_disabled`（`memory_processing.py:1591`）同样不在白名单中，会落到 :2475 记为 unknown，而不是退回 pending（**仅源码推断，未实测**）。这意味着就绪标志只有在 claim 阶段拦截时才是安全的，见下表第 1 步。

### 调用顺序（claim → 模型调用）

| 步 | 位置 | 动作 | 此处被拒的结果 |
| --- | --- | --- | --- |
| 1 | `durable_queue.py:2661-2673`、`:2819-2841`；回调 `api.py:5446` `_sedimentation_claim_enabled`（含 `:5428` `_source_writes_on` → Hub `app.py` `source_write_authorizer`） | claim 前按来源过滤，再写 running、owner、lease | 不 claim，任务保持 pending（安全） |
| 2 | `durable_queue.py:2323-2351` | 读冻结 payload 与 evidence | payload 缺失记为 failed |
| 3 | `memory_processing.py:1586-1592` | 检查沉淀开关与 `_source_may_write`（同一来源回调） | `model_sedimentation_disabled` 退回 pending；`hy_memory_source_disabled` 记为 unknown（源码推断） |
| 4 | `memory_processing.py:1593-1604`；`hy_memory_bridge.py:160-169,501-513,521-524`；Hub `sdk_admission.py:29-36` | 查凭证、建 bridge，`prepare()` 进入 `exclusive_access` → `SdkAdmission.operation` | `hy_memory_maintenance_in_progress` 记为 unknown 且 `hy_invoked=1`（实测，E3′） |
| 5 | `memory_processing.py:1612` → `durable_queue.py:2354-2386` → `api.py:1201-1214` | `admit_hy_invocation` 持锁复查 `_source_writes_on`，然后 `mark_invoked` | `model_content_source_disabled` 退回 pending（:2437） |
| 6 | `memory_processing.py:1613-1617` | `bridge.ingest` 再次进入独占区，调用 SDK/模型 | 已标记 `hy_invoked`，任何失败都记为 unknown |
| 7 | `durable_queue.py:2401` → Hub `output_lineage.py:186-195` | SDK 写入成功后，`observe_sdk_outputs` 再次进入 `bridge.exclusive_access` | **SDK 已写入**，此处的 maintenance 或其他错误同样记为 unknown（源码推断，未实测） |

第 7 步说明：同一个错误码 `hy_memory_maintenance_in_progress` 既可能出现在没有写入时（第 4 步），也可能出现在已经写入后（第 7 步）。**错误码本身不能证明没有 SDK 产出。**

### unknown 任务的最终落点与恢复路径

| 路径 | 位置 | 结果 | 是否自动 |
| --- | --- | --- | --- |
| Hub `SdkReconciler.run` | `sdk_reconcile.py:173-231` | 只在 `proven_receipt` 证明有成功产出时调用 `resolve_unknown(found=True)` 转为 completed；没有产出时 defer，仍为 unknown（实测 run()=0） | 自动，只证明「有」 |
| 上游 SDK 模式的自动重试 | `api.py:1170-1176` | SDK 权威模式下只调 Hub 回调后即 return；`retry_first_unknown_after_reconcile`、`retry_timed_out_unknown_after_reconcile` 只在旧权威分支调用 | Hub 部署中不会发生（源码核对） |
| `resolve_unknown(found=False)` | `durable_queue.py:1758-1858`；`job_state.py:77` | `UNKNOWN + GIVE_UP → DEAD`，`last_error=model_content_outcome_not_found`；状态机不允许 unknown 回到 pending。dead 是终态，`clear_terminal_payloads` 会清掉正文 | 只能显式调用；**已核实源码，未调用** |
| `retry_unknown_confirmed` | `durable_queue.py:1860-1909`；入口 `api.py` `POST /api/v1/memory-processing/retry-unknown`（:3935-3953） | unknown → pending，`hy_invoked=0`；会重跑提炼 | 用户显式确认 |
| `settle(cancelled)` | `durable_queue.py:1425-1432` | unknown 状态下 settle 到其他状态被静默忽略（no-op） | — |

结论：在当前 Hub 部署中，因维护锁变成 unknown 的任务会**永久停在 unknown**，正文留在队列行中。只有在用户触发上游 retry-unknown 之后才会被重新提炼。清扫不能用 `settle(cancelled)` 结束它，只能用 `resolve_unknown(found=False)` 转为 dead。

这还带来一个与治理无关的已有影响：任何治理执行、purge 重放或 resume 持有独占区期间被 claim 的任务都会变成 unknown，内容不再自动提炼（无关来源也一样）。是否把修复范围扩大到这里，见 Q6。

### 清扫对 unknown 的两个方案（Q5，不替使用者取舍）

**方案 X：证明「无 SDK 产出」后转为 dead**

在独占区内，对命中规则的 unknown 任务，同时满足以下四条才认定无产出，然后 `resolve_unknown(job_id, found=False, detail="governance_source_suppressed")`：

1. `bridge.get_sdk_write_outcome(request_id)` 为 None（与 `SdkReconciler` 用的是同一个 SDK 结果缓存，补丁 0007）；
2. 按 `request_id` 推导出的 trace（`productivity_inventory.trace_id`）与该任务的 evidence_id 对应的 l1_raw 节点，在 SDK 向量库中都不存在；
3. 该回执在 `cmh_event_outcomes`（Hub 产出关联）中没有行；
4. 判定在 `admission.exclusive()` 内进行：此时其他 worker 不可能处于第 4–7 步之间，判定期间不会出现新的写入。

任何一条不满足：如果证明有产出，就按 completed 处理，其产出由正常的范围重算纳入（范围变化按 ADR-0032 返回 409，要求重新预览）；如果无法判定（如 SDK 读取失败），报 503 `governance_ingest_in_flight`，操作保持 planned。

- 优点：不改执行顺序，也能收拾已存在的 unknown（含恢复的旧备份、以前的维护期遗留）。
- 风险：第 1 条依赖 SDK 结果缓存在写入时一定落盘。若 SDK 写入了节点但没写结果缓存（崩溃窗口），第 2 条兜底；第 2 条依赖 raw 节点 ID 能由 evidence 推导，**需 T-034-2a 先用测试证明**。dead 是终态，误判就是丢一次提炼；忘记时原文还在，可以手动恢复，彻底删除时符合语义。

**方案 Y：执行前先关就绪标志，让 worker 在进入独占区前被拒**

1. `execute` 和 `resume` 在调用 `admission.exclusive()` 之前，先把 `SourceSuppression.ready` 设为 False（进程内标志，受 `source_write_authorizer` 读取）。
2. 等已 claim 的 running 任务结束（`exclusive()` 本来就会等 `_active` 清空；还要等已过 claim、尚未进入 prepare 的任务，见下文窗口），然后才进入独占区。
3. 新的 claim 在表中第 1 步就被拒，任务保持 pending，不会变成 unknown。
4. 操作 completed、启动清扫完成后再把 `ready` 设回 True。

- 优点：从源头不产生新的 unknown，也顺带解决 Q6 的大部分问题（只限治理触发的独占区）。
- 剩余窗口：任务在第 1 步之后、第 4 步之前（读 payload、查凭证）时，就绪标志已经无效，这时进入独占区仍会变成 unknown。要关闭这个窗口，必须等「已 claim 未进入 operation」的任务清空，Hub 无法从 `SdkAdmission` 看到它们；可以轮询 `memory_write_jobs` 中 running 且 `hy_invoked=0` 的行直到为零（有超时，lease 10 秒）。
- 不能处理已存在的 unknown（恢复的旧备份、以前遗留的），这些仍要方案 X 或人工处理。
- `ready=False` 期间所有来源都不提炼，接纳不受影响。

两个方案可以组合（Y 防新增，X 收拾存量），代价是两份实现。

### 启动就绪策略（修订后）

1. 进程启动时 `ready=False`，第 1 步 claim 拒绝一切 Hub 来源，任务保持 pending。
2. `resume`：先 `replay_purges`，再完成未完成的操作，然后对全部操作执行清扫（含 completed）。清扫遇到 unknown 时按 Q5 选定的方案处理。
3. 清扫成功后 `ready=True`；清扫失败则保持 False，健康检查报 `governance_sweep_incomplete`（新错误码），不开放提炼，接纳与读取不受影响。
4. 约束：`ready` 只能在 claim 阶段（第 1 步）起作用；不得依赖第 3 步的 `_source_may_write` 拦截，因为那里会把任务记为 unknown（源码推断，T-034-3 须用测试固定）。

### 对接口契约的修订

- `governance_ingest_in_flight`（503）只用于「无法判定有无产出」，不再用于所有 `hy_invoked=1` 的任务。
- 新增健康检查错误码 `governance_sweep_incomplete`。
- 方案 X 新增队列终态语义：命中规则的 unknown 任务转为 dead，`last_error=governance_source_suppressed`。

## 修订二（2026-11-09，按 T-034-2a 实测）

依据：提交 932279a 新增的 5 个测试实例（`apps/hub/tests/test_governance_execution.py`）。以下每条结论后注明测试名与原始输出行；没有测试支撑的标为「源码」。修订一中的方案 X、Q5 选项以本节为准。

### 实测事实

| 编号 | 事实 | 依据 |
| --- | --- | --- |
| F1 | 第 3 步（supervisor 来源回调拒绝）：任务为 unknown，`hy_invoked=1`，SDK 没有写入，`get_sdk_write_outcome` 为 None。修订一的源码推断得到证实 | `test_t0342a_native_supervisor_prewrite_refusals[3]`：`step=3: state=unknown, hy_invoked=1, error=hy_memory_source_disabled, sdk_written=False` |
| F2 | 第 4 步（维护锁）：同上，outcome 为 None | `test_t0342a_native_supervisor_prewrite_refusals[4]`：`step=4: state=unknown, hy_invoked=1, error=hy_memory_maintenance_in_progress, sdk_written=False` |
| F3 | 第 7 步（写入后的产出观察）遇到维护锁：任务同样为 unknown，错误码相同，但 SDK 已有 active 记忆和可读的 raw 正文，outcome 为 `succeeded` | `test_t0342a_postwrite_observer_maintenance`：`step=7: state=unknown, hy_invoked=1, error=hy_memory_maintenance_in_progress, sdk_written=True, raw_text=True` |
| F4 | 正常完成时，结果缓存已落盘：独立连接、实例关闭后都读到 succeeded 和 raw ID | `test_t0342a_cache_persistence_and_random_raw_identity[False]`：`interrupt_finish=False, disk_status=succeeded, raw_already_stored=True` |
| F5 | 节点写入与成功结果不原子：节点已写入（含正文）、结果尚未 finish 时中断，磁盘上是 `running` / `{}` | 同测试 `[True]`：`interrupt_finish=True, disk_status=running, raw_already_stored=True` |
| F6 | raw 节点 ID 是 `uuid4` 随机生成的，不能由 evidence 推导 | 同测试两例：`raw_id_random_uuid4=True, evidence_derivable=False`；源码 `hy_memory/pipelines/writer.py:2730-2745`、`hy_memory/models/memory.py:335-337` |

由此推出的结论：

- **C1 错误码不能区分有无产出**：F2 与 F3 的状态、`hy_invoked`、错误码完全相同，一个没有写入，一个已经写入（F2、F3）。
- **C2 修订一的判据 2 作废**（F6）。
- **C3 修订一的判据 1 写错了**：原文是「outcome 为 None 即无产出」。F5 说明写入后可能停在 `running/{}`，此时 outcome 不是 None，也不是 succeeded；原判据会把它当成什么都不是，而节点其实已经写入了。

### 判据 1 的更正：只有「结果行不存在」能证明没有写入

源码：`async_add` 在调用 `_async_add_impl` 写任何节点之前，先执行 `_claim_write_outcome`（`hy_memory/client.py:2529-2538`），也就是 `INSERT OR IGNORE ... 'running','{}'` 后立即 `commit`（`hy_memory/data/cache_sqlite.py:443-455`）；`add_extracted`（:3053）、`import_raw`（:3216）以及 :3338 处同理。异常路径会把它改成 `unknown`（:2562-2563）。所以按 request_id 查结果行，有四种情况：

| 结果行状态 | 含义 | 能否判定 | 依据 |
| --- | --- | --- | --- |
| 不存在 | 本次 add 还没有开始，SDK 不可能有这次请求的产出 | **能：无产出** | F1、F2（实测 outcome 为 None 且 `sdk_written=False`）；顺序为源码 |
| `succeeded` | 已有产出 | **能：有产出** | F3、F4 |
| `running` / `{}` | 可能已写入节点（F5），也可能进程仍在执行 | **不能** | F5 |
| `unknown`、`failed` | 异常退出；`failed` 由 `success` 不为 True 决定，不能证明没写入节点 | **不能**（源码） | `client.py:2562-2568` |

适用边界（判定前必须满足，否则一律视为「不能判定」）：

1. 判定在 `admission.exclusive()` 内进行，而且没有其他进程写同一个数据目录（ADR-0032 的存储租约）。否则「不存在」可能只是还没 claim。
2. 本操作的 purger 还没有删除该请求的 `write_outcomes` 行（`governance_purge.py:107`）。决策 1 已规定清扫先于 purger 运行；启动清扫遇到之前已被彻底删除的请求时，不使用这条判据，交给 `replay_purges` 处理。
3. 结果缓存与向量库来自同一时刻的数据。如果只恢复了其中一份备份（例如旧 `cache.sqlite3` 搭配新 Chroma），「不存在」不可信。Hub 目前无法检测这种情况，在实现说明中写明，并由 T-034-2b 尽量用测试固定。
4. **未验证**：SDK 的 cache 连接是否使用 `synchronous=FULL`。断电时已 commit 的行可能丢失，也就是 SIGKILL 和断电下的持久性没有测（T-034-2a 未跑）。

### 「不能判定」的任务（running/{}、unknown、failed 行）

修订一中的判据 2（由 evidence 推导 raw ID）作废。可替代的候选判据目前都**未经测试**，只能作为 T-034-2b 的探查目标，不能直接用于实现：

- 候选 2′a：Hub 写入的每条 raw verbatim 末尾都带有 `<cmh_source_reference>`，里面有 event 身份（`remote_ingest.py:93-103`）。可以在项目命名空间内扫描 raw 节点的内容，看有没有这个事件的引用。代价是每次全量扫描。
- 候选 2′b：派生节点的 `custom.request_id` 始终会写入（`writer.py:243-251`）；但 raw 节点的 `custom` 只挂调用方 meta，不含 request_id（`writer.py:2728-2729`）。所以 2′b 只能找到派生记忆，找不到 raw。

在 2′a 被测试证明之前，「不能判定」的任务只能按「可能已写入」处理，见下面的方案。

### 修正后的可选方案（Q5，不替使用者取舍）

| 方案 | 做法 | 对新产生的 unknown | 对存量 unknown | 依据与风险 |
| --- | --- | --- | --- | --- |
| **X′ 证明后终止** | 独占区内，对命中规则的 unknown：结果行**不存在** → `resolve_unknown(found=False)` 转为 dead；**succeeded** → 走已有的 `SdkReconciler`（`proven_receipt` → completed → lineage 观察），其产出进入范围，范围变化按 ADR-0032 返回 409，要求重新预览；**running/unknown/failed** → 按 Q7 处理 | 处理得了：第 3、4 步产生的 unknown 都满足「不存在」（F1、F2）；第 7 步产生的为 succeeded（F3） | 处理得了，但受上面四条边界约束；属于 running/{} 的不能自动处理 | 误判风险集中在边界 3、4。dead 是终态，误判就是少提炼一次；忘记时原文还在，可以手动恢复 |
| **Y 源头阻止** | 进入独占区前先关就绪标志，拦截只在第 1 步 claim 过滤上生效；等「已 claim、未进 operation」的任务清空后才进独占区 | 基本不产生：第 3 步拦截会落 unknown（F1），所以必须在第 1 步就拦住；claim 之后、prepare 之前的窗口要轮询 running 且 `hy_invoked=0` 的行 | **处理不了**：恢复的旧备份、以前遗留的 unknown 原样保留；命中规则的这类任务之后也不会被提炼（unknown 不自动重排，修订一已核），但正文留在队列行中，彻底删除的核验会失败 | 依据 F1：第 3 步不能作为拦截点。Y 单独使用时，彻底删除遇到存量 unknown 会一直核验不通过 |
| **Z = Y + X′** | Y 防止新增，X′ 处理存量 | 同 Y | 同 X′ | 两套实现；X′ 的边界同样适用 |
| **W 人工处置** | 不自动终止任何 unknown。命中规则的 unknown 使操作停在 planned（503 `governance_ingest_in_flight`），健康检查列出数量，由管理员确认后，Hub 再调用 `resolve_unknown(found=False)` 或上游 retry-unknown | 不处理，每次都要人介入 | 不处理，每次都要人介入 | 最保守、实现最少；代价是操作可能长时间停在 planned，期间 `require_writes_ready` 拦截整个实例的 SDK 新写入 |

修订一方案 Y 中「解决 Q6 大部分问题」的说法维持不变，但要补充一点：Y 对存量 unknown 无能为力（上表）。

### 对其余设计的影响

- 决策 1 第 2 条中「unknown，或 running 且 `hy_invoked=1`」的处理改为按上表执行；`governance_ingest_in_flight` 只在 W，或 X′/Z 遇到「不能判定」且 Q7 选择阻塞时返回。
- 启动就绪策略第 4 条从「源码推断」改为「实测」（F1）：就绪标志只能在第 1 步起作用。
- 彻底删除：结果为 succeeded 的 unknown 已有产出，必须先纳入范围，否则 purger 清不掉这些新记忆（F3）。这正是 X′ 中 409 重新预览的理由。
- 修订一的四条判据整体作废，以上表为准。

## 替代方案与否决理由

| 方案 | 说明 | 否决理由 |
| --- | --- | --- |
| 更简单 A：整源封禁 | 来源下任一记录被治理后，worker 授权对整个 source_id 返回 False | 一个 Agent 在一台设备上的全部后续内容都无法提炼，违反 ADR-0033 已验收的「无关记录可接纳」 |
| 更简单 B：执行期间接纳全局 503 | 有未完成操作时拒绝所有上传，不清扫队列 | 只关闭 G-B；G-A、G-C 的存量任务仍会被提炼 |
| C：上游补丁按任务授权 | 给 `_source_writes_on` 传 job_id/evidence，在每次调用 HY 前做记录级检查 | 更精确，也能覆盖恢复后的队列；但要新增上游补丁、每次 HY 调用都查一次，且仍需取消任务以清理队列正文。作为升级路径保留 |
| D：客户端去重 | outbox 记住已删除内容，不再上传 | 重装或换设备后状态丢失，依赖尚未实现的离线规则同步，客户端还要保存指纹 |
| E：语义相似去重 | 接纳时用向量相似度判断重导入 | 结果不确定、误伤难控制，接纳路径要调用 SDK |
| F：指纹另存一张表 | 不放入签名意图 | 没有签名，恢复备份时可能丢失，还要新增表结构；`content` 类型已预留，不需要 |

## 接口与数据契约

示意（签名，不是实现）：

```python
# governance_sources.py
def capture_rules(journal, ingest, scope_id, records) -> tuple[Target, ...]: ...
    # 来源三元组 + （Q1=A 时）内容指纹；原 capture_sources 收进这里
def event_rule_targets(journal, event) -> frozenset[Target]: ...
class SourceSuppression:
    def require_allowed(self, event) -> None: ...   # 409 governance_source_suppressed | governance_content_suppressed
    def sweep_inflight(self, ingest, queue, intents) -> int: ...  # 返回取消/转 dead 数；无法判定有无产出时抛 governance_ingest_in_flight（修订一）
    ready: bool  # 启动清扫完成前为 False，source_write_authorizer 据此拒绝
```

| 接口 | 变化 | 类型 |
| --- | --- | --- |
| POST 事件接纳 | 新增 409 `governance_content_suppressed`（仅 Q1=A） | 新增错误码 |
| POST /hub/v1/governance/execute | 新增 503 `governance_ingest_in_flight`（修订一：仅在无法判定有无 SDK 产出时），可带同一 operation_id 重试；响应字段不变 | 新增错误码 |
| 队列任务 | 命中规则的在途任务终态为 `cancelled`，`last_error=governance_source_suppressed` | 数据语义 |
| Intent.targets | 新写入 `content` 类型目标（已有 TargetKind）；总数仍受 10 000 上限约束，超限在写意图前返回 409 `governance_scan_limit`，不能落到通用 503 | 签名数据语义 |

数据库列、schema_version 与治理协议不变。

## 失败模式

| 场景 | 行为 |
| --- | --- |
| 清扫时命中 unknown 或已调用 HY 的任务（修订一） | 按 Q5 的方案 X/Y 处理；只有无法判定有无产出时才 503 `governance_ingest_in_flight`，操作保持 planned。若已有产出，下一次重试重新计算范围，范围变化按 ADR-0032 返回 409，要求重新预览 |
| 治理持有独占区期间，无关来源的任务被 claim（修订一） | 记为 unknown 且永久停留，内容不再自动提炼（E3′ 实测）。见 Q6 |
| 取消后、操作完成前崩溃 | 取消是终态，resume 重跑清扫时跳过已终态任务，幂等 |
| ~~`settle` 不允许无 owner 把 running 改成 cancelled~~ | 已排除：`test_inflight_upstream_ownerless_settle_cancels_uninvoked_jobs` 实测 pending、waiting_for_permission、waiting_for_model、running（`hy_invoked=0`）均可无 owner 取消；旧 worker 之后不能 mark 或覆盖终态 |
| 对 unknown 调用 `settle(cancelled)` | 上游静默忽略（`durable_queue.py:1425-1432`），任务仍为 unknown。实现不得把 settle 的返回当作已取消，必须回读状态 |
| 只有队列任务、没有回执（接纳事务在 submit 后回滚） | 清扫按回执查找，找不到这类任务。这是已有缺陷（lineage 会报 `lineage_original_missing`），不在本文范围，另行登记 |
| 隐私过滤策略版本变化 | 同一文本过滤后不同，指纹不命中。在验证记录中如实写明；升级策略时需要重新评估 |
| 指纹被用作探测（设备能否知道某段话已删除） | 单用户系统，设备本来就持有该内容；HMAC 带实例密钥，离线无法穷举 |
| 启动清扫失败 | 授权回调保持 False，健康检查报错，不开放提炼 |
| 规则检查成本 | 每次接纳遍历本项目全部操作并计算 HMAC；G1 规模可接受。`ponytail:` 线性扫描，操作数上千时改为按项目缓存规则集 |

## 迁移与回滚

- 无表结构迁移。已有的 0033 操作没有内容指纹：忘记操作的原文仍在，可在规则检查时现算；彻底删除操作的原文已不存在，无法补齐（见 Q3）。
- 回滚到不含本修复的镜像：内容指纹不再检查，在途任务不再清扫，G-A 到 G-D 重新出现。已取消的任务保持取消，已写入的意图不受影响。回滚前必须停服备份，并在验证记录中注明。
- 取消在途任务属于不可逆的数据效果：忘记时可借助保留的原文和上游 `readmit_cancelled_permission_confirmed` 手动恢复（尚无工具）；彻底删除时不可恢复，这符合彻底删除的语义。

## 已确认的产品决定

2026-11-09，用户在本项目对话中明确选择“拒绝整条事件，优先防止内容复活”：Q1=A、Q2=A。规范化后至少 16 字符的任一消息命中忘记/删除规则，即拒绝整个事件；不能只检查稳定来源 ID，也不能要求所有消息都命中才拒绝。以下表格保留原选项用于追溯，Q1/Q2 不再待定，不重复询问。

### 内容规则的工程落实

- Q3 沿用 ADR-0033 的失败关闭原则（工程选择，不表述为用户另外回答过 Q3）：旧忘记从保留且校验的原文重建内容规则；旧彻底删除若无法证明内容规则，所属项目新上传返回 governance_content_rules_unavailable。若连旧来源规则也缺失，优先原 governance_source_rules_unavailable。当前 NAS 没有治理操作，不涉及真实旧操作迁移；不得改写旧意图。
- 新操作额外保存一个带密钥的 content 策略目标，证明已经执行本版本采集。规范身份分别为 `[1,"policy","nfc-whitespace-min16-any-sha256-v1"]`、`[1,"message",sha256(规范化文本UTF-8).hexdigest()]`，二者都交给原 journal.target 做 HMAC。因此全是短消息时也不会被误认为没有采集过内容规则；策略目标与任意文本目标域隔离。字符数按 NFC 后 Unicode 码点计算。
- 同一次校验原文读取生成来源及内容规则；物理记录和全部规则合计不超过 10 000，超限明确返回 409，不静默截断。
- 捕获与接纳检查可独立实现和测试，暂不依赖在途清扫。T-034-5 只有在清扫也使用同一规则且全部场景通过后才算完成；此拆分不降低 FR-11。

## 决策选项与其余待定项

| 编号 | 问题 | 选项 A | 选项 B | 影响的卡 |
| --- | --- | --- | --- | --- |
| Q1 | 彻底删除或忘记后，同内容换了来源/会话/记录 ID 再次上传，是否拒绝？ | 拒绝（409，按内容指纹）。满足 FR-11「重新导入不复活」；代价是用户以后原样重说同一段话（≥16 字符）也会被拒 | 允许接纳，只按稳定来源身份屏蔽（保持现状）。不误伤；FR-11 的重导入场景仍不满足，需在验收中写明 | T-034-5 |
| Q2 | 一个事件含多条消息时，怎样算命中？（仅 Q1=A） | 任一条命中就拒绝整个事件。漏判少；新消息里引用了旧原文，整个事件都会被拒 | 所有可指纹的消息都命中才拒绝。误伤少；重导入时把旧消息和新消息合进同一事件就能绕过 | T-034-5 |
| Q3 | ADR-0033 之后、本修复之前完成的彻底删除没有内容指纹，怎么处理？（仅 Q1=A） | 对应项目的新上传返回 503，直到管理员处理（与 0033 旧操作一致，保守） | 这些旧操作只保留来源三元组保护，内容保护只对新操作生效，并在验证记录中标注。当前 NAS 是空实例，实际影响很小 | T-034-5 |
| Q4 | 彻底删除时，同来源但不在确认范围内的在途新修订（已接纳、未提炼），它的原文怎么办？ | 预览把这些修订纳入范围，view_digest 随之变化，用户确认的就是实际删除的内容 | 只取消提炼，原文保留在 Hub，在预览和结果中提示数量，由用户另行删除 | T-034-6 |
| Q5（修订二重写） | 清扫遇到命中规则的 unknown 任务，用哪种方案？详见「修订二」 | X′：按结果行四态处理，「不存在」转 dead，succeeded 纳入范围；能处理存量，但受四条边界约束，误判会少提炼一次。Z（Y+X′）：两者兼得，代价是两套实现 | Y：只在 claim 阶段拦截，不产生新 unknown，但处理不了存量，彻底删除遇到存量会一直核验不通过。W：全部交给管理员手动处置，最保守，操作可能长时间停在 planned、拦截整个实例的新写入 | T-034-2b、T-034-2、T-034-3 |
| Q7（修订二） | X′/Z 遇到「不能判定」的任务（结果行为 running/{}、unknown、failed），怎么处理？ | 阻塞：操作停在 planned（503 `governance_ingest_in_flight`），健康检查列出，由管理员确认后再处置。不会误删，但可能长期阻塞 | 视为「可能已写入」：用候选 2′a 找到 raw 节点后纳入范围，返回 409 要求重新预览；2′a 未证明可用前只能选 A | T-034-2b、T-034-2 |
| Q6（修订一） | 治理持有独占区期间，无关来源的任务也会变成永久 unknown，不再自动提炼（已有行为，E3′ 实测）。本文要不要一起处理？ | 一起处理：选方案 Y（或 X+Y），并把 Hub 自己持有独占区的时段都纳入就绪标志控制 | 不处理：本文只管命中规则的任务，无关来源的 unknown 另开卡，在验证记录中如实写明 | T-034-3 |

Q4 的同一问题也适用于同来源、已完成提炼、不在范围内的修订（ADR-0033 的现状），那部分不在本文范围，只在此指出。

## 影响面清单

- 要改的文件：`governance_sources.py`（规则采集、匹配、清扫、就绪标志），`governance_execution.py`（`_finish` 调用清扫、`resume` 启动清扫、错误码映射、目标上限），`governance_purge.py`（清除队列正文、核验 needle），`app.py`（授权回调接入就绪标志），Q4=A 时再加 `governance_preview.py`/`governance_plan.py`。
- 波及的模块：接纳（`remote_ingest.py` 的调用点不变，行为变化来自注入的回调）、队列 worker（被取消或退回 pending 的任务）、来源图（被取消任务没有产出，属于正常的「未完成」）。
- 可能变红的测试：`test_governance_execution.py` 中断言队列状态或 one-memory 字节的用例；`test_remote_ingest.py` 中依赖授权回调默认值的用例（就绪标志默认值必须让未接线的实例保持原行为）；`scripts/probe_governance_execution.py` 跨进程探针。`make check` 本身不运行 Hub 测试。
- 文档：本 ADR 状态、`docs/verification.md`、`docs/roadmap.md`、新的验证记录。

## 验证计划

用真实 Hub HTTP、原生 SDK/存储和合成提炼结果（不调用收费模型，不导入用户历史）：

1. V1 复现 G-A：同来源 rev2 处于 pending，对 rev1 做忘记；修复前 rev2 在完成后被提炼，修复后为 cancelled，SDK 中没有新记忆。彻底删除重复一次，再按消息文本扫描 one-memory 字节。
2. V2 复现 G-B：在 `capture_sources` 与 `store.plan` 之间注入一次接纳：修复后该任务被清扫取消；在 plan 之后注入：返回 409。
3. V3 复现 G-C：完成操作后恢复带 pending 任务的旧 `one-memory.sqlite3`，重启：启动清扫完成前不提炼，完成后任务为 cancelled。
4. V4（修订一；修订二后，方案 X 按 X′ 理解，验收以 T-034-2 卡为准）unknown 任务：以 `test_inflight_worker_during_sdk_exclusive_records_actual_outcome` 的方式制造命中规则的 unknown（无产出）。方案 X：执行后任务为 dead，正文被清除，操作 completed；再制造一个已有 SDK 产出的 unknown，执行不得转为 dead（返回 409 范围变化或按 completed 处理）；SDK 读取失败时返回 503 `governance_ingest_in_flight`，操作 planned。方案 Y：执行期间新 claim 被拒、任务保持 pending；claim 后、prepare 前的任务在执行进入独占区前已结束。
5. V5 内容指纹（Q1=A）：新注册来源、新会话、新记录 ID 上传同一文本，返回 409；无关文本、不足 16 字符、其他项目可以接纳；治理目录字节中没有该文本；Q2、Q3 按所选口径各有一例。
6. V6 回归：无关来源记录可接纳，ADR-0033 全部用例通过，`suppressed_count` 不变。
7. 门禁：`make check`、Hub 全量、`make probe-governance-execution`；跳过项单列，不算通过。

## 任务卡

每张卡最晚交付点按「开工后」计时。都在 `codex/governance-reimport-recovery` 上进行，不 push、不合并 main。

### T-034-1 复现在途缺口并核实上游合同

- 目标：用测试确认 G-A、G-B、G-C 真实存在，并核实决策 1 依赖的上游行为。
- 输入：本 ADR「证据」与 V1–V3；上游 `durable_queue.py` 的 `settle`、`release_unstarted`、`clear_terminal_payloads`；`sdk_admission.py`。
- 输出：V1–V3 的测试（预期在当前代码上失败）；记录实测结论：无 owner 时 `settle` 能否把 pending、waiting_*、running（`hy_invoked=0`）改成 cancelled；独占区内 worker 遇到 `hy_memory_maintenance_in_progress` 时任务最终处于什么状态。结论写入新验证记录草稿 `docs/reviews/2026-11-xx-inflight-ingest.md`。
- 改动文件：`apps/hub/tests/test_governance_execution.py`，新验证记录。
- 验收标准：三项测试在当前代码上失败，且失败原因与 G-A/B/C 的描述一致；若有一项不能复现，记录实际结果，并通知架构师修订本 ADR，之后的卡暂停。上游行为每一条都有测试或源码行号作为证据。
- 依赖：无。
- 最晚交付点：开工后 4 小时。
- 状态：已完成，提交 c7f9ba4。结论见「修订一」；没有写仓库内验证记录草稿，证据记在仓库外的任务台账，由 T-034-7 收入验证记录。

### T-034-2a 方案判据的前置测试（修订一新增）

- 目标：为 Q5 提供证据。只写测试，不改生产代码。
- 输入：「修订一」的四条判据、调用顺序表第 3、7 步。
- 输出：测试证明或证伪以下三点：(1) SDK 写入成功时一定有 `get_sdk_write_outcome` 结果，包括在 `observe_sdk_outputs` 之前崩溃的情况；(2) 能否由任务 evidence 推导出 SDK l1_raw 节点 ID，并据此查存在性；(3) 第 3 步 `hy_memory_source_disabled` 的实际落点（unknown 还是 pending）；第 7 步观察阶段遇到维护锁时，是否 SDK 已写入而任务为 unknown。
- 改动文件：`apps/hub/tests/test_governance_execution.py`。
- 验收标准：三点各有测试与原始输出；不能证明的判据标为「不可用」，并通知架构师修订方案 X。
- 依赖：T-034-1。可在 Q5 决定前进行，供决定参考。
- 最晚交付点：开工后 3 小时。
- 状态：已完成，提交 932279a。结论见「修订二」F1–F6：第 3、4 步落点为 unknown 且无写入；第 7 步落点为 unknown 但已写入；判据 2 不可用；判据 1 只在正常完成时成立。

### T-034-2b 「不能判定」情形的补充测试（修订二新增）

- 目标：为方案 X′ / Z 的适用边界补证据。只写测试，不改生产代码；Q5 选 Y 或 W 时可以不做。
- 输入：「修订二」判据 1 的四条适用边界，候选判据 2′a。
- 输出：测试回答以下三点：(1) 结果行不存在时，SDK 中确实没有这次请求的任何节点，覆盖 `add`、`add_extracted` 两种写入；(2) 只恢复 `cache.sqlite3` 旧备份、不恢复向量库时，能否检测出两者不一致，检测不出就明确写出；(3) 候选 2′a（按 `<cmh_source_reference>` 中的 event 身份扫描 raw）能否在 running/{} 的情形下找到已写入的 raw 节点，以及在合成 1 000 条 raw 时扫描的耗时。
- 改动文件：`apps/hub/tests/test_governance_execution.py`。
- 验收标准：三点各有测试名与原始输出；不成立的项标为「不可用」，并通知架构师修订方案 X′。
- 依赖：T-034-2a（已完成）。Q5 决定前就可以做，供决定参考。
- 最晚交付点：开工后 3 小时。

### T-034-2 执行时清扫在途任务

- 目标：实现决策 1 的第 1–3 条（按修订二）和第 6 条。
- 输入：T-034-1、T-034-2a 的结论（X′/Z 还要 T-034-2b）；Q5、Q7 的决定；本 ADR「修订二」「接口与数据契约」「失败模式」。
- 输出：`sweep_inflight` 由 `_finish` 第一步调用。可取消状态取消，取消后回读确认。unknown 按 Q5 处理：X′/Z 按结果行四态分别处理（不存在 → `resolve_unknown(found=False)`；succeeded → 走 reconciler，范围变化返回 409；其余按 Q7）；Y 是执行前关就绪标志、只在 claim 阶段拦截，并等待已 claim 任务清空；W 是阻塞并列出数量。`governance_ingest_in_flight` 只在修订二规定的情形返回；目标超限映射为 409 `governance_scan_limit`。
- 改动文件：`governance_sources.py`，`governance_execution.py`，`apps/hub/tests/test_governance_execution.py`。
- 验收标准：V1（忘记部分）、V2、V4（所选方案的分支）、V6 通过；X′/Z 必须有三条测试：结果行不存在 → dead、succeeded → 不转 dead、running/{} → 不转 dead，其中 running/{} 用 `test_t0342a_cache_persistence_and_random_raw_identity[True]` 的中断方式制造；第 7 步产生的 unknown（同 `test_t0342a_postwrite_observer_maintenance`）不得被判为无产出；c7f9ba4 中 G-A（忘记）、G-B 的 strict xfail 实例改为普通通过断言（不能只删 xfail 标记；按 QA 备注，purge+revision1 用例中「队列正文仍在」的前置断言要同步改写）；已有治理测试全部通过；清扫不触碰其他项目或无关来源的任务（有断言）。
- 依赖：T-034-1、T-034-2a（均已完成）；Q5 选 X′ 或 Z 时还依赖 T-034-2b；**Q5、Q7（待产品确认，未决定前不开工）**。
- 最晚交付点：Q5 决定后开工，开工后 4 小时。

### T-034-3 启动清扫与授权就绪标志

- 目标：实现「修订一」的启动就绪策略（第 4 条已由修订二 F1 实测确认），覆盖恢复旧备份。
- 输入：T-034-2 的 `sweep_inflight`；Q5、Q6、Q7 的决定；调用顺序表；修订二 F1。
- 输出：进程启动时 `ready=False`；`resume` 在开放写入前对全部操作清扫一次，成功后 `ready=True`，失败时健康检查报 `governance_sweep_incomplete`；`SourceSuppression.ready` 接入 `app.py` 的 `source_write_authorizer`；未接线时默认值保持原行为。Q6=A 时，Hub 持有独占区的所有时段（执行、resume、purge 重放）都先关就绪标志。
- 改动文件：`governance_execution.py`，`governance_sources.py`，`app.py`，`apps/hub/tests/test_governance_execution.py`。
- 验收标准：V3 通过，c7f9ba4 中 G-C 的 strict xfail 实例改为普通通过断言；**就绪前任务在 claim 阶段被拒、保持 pending**（断言 state=pending 且 `hy_invoked=0`，不能是 unknown 或 failed）；有测试证明就绪拦截发生在调用顺序第 1 步而不是第 3 步（对照 `test_t0342a_native_supervisor_prewrite_refusals[3]`：在第 3 步拦截会落 unknown）；启动清扫遇到之前已被彻底删除的请求时，不使用「结果行不存在」判据（修订二边界 2）；Q5=Y 时，存量 unknown 要列入健康检查，不能宣称已清扫；`test_inflight_worker_during_sdk_exclusive_records_actual_outcome` 在 Q6=B 时保持原断言，Q6=A 时按新行为改写并说明；`test_remote_ingest.py` 全部通过。
- 依赖：T-034-2；**Q5、Q6、Q7（待产品确认）**。
- 最晚交付点：开工后 3 小时。

### T-034-4 彻底删除时清除队列正文

- 目标：实现决策 1 第 5 条。
- 输入：T-034-2；ADR-0032 清理清单。
- 输出：purger 在 VACUUM 前循环调用 `clear_terminal_payloads()` 直到返回 0；核验 needle 加入范围内原文的消息文本。
- 改动文件：`governance_purge.py`，`apps/hub/tests/test_governance_execution.py`。
- 验收标准：V1（彻底删除部分）通过：one-memory 字节中找不到消息文本；故意保留一个非终态任务时核验报 `governance_purge_residue`；已有彻底删除用例与 HNSW 限制用例保持原结果。
- 依赖：T-034-2。
- 最晚交付点：开工后 3 小时。

### T-034-5 内容指纹规则

- 目标：实现决策 2。
- 输入：Q1、Q2、Q3 的决定（**待产品确认，未决定前不开工**）；T-034-2 的清扫匹配入口。
- 输出：`capture_rules` 写入 content 目标；`require_allowed` 返回 409 `governance_content_suppressed`；清扫同时按内容匹配；按 Q3 处理旧操作。Q1=B 时本卡改为：只在验证记录和 roadmap 中写明 FR-11 的重导入场景未覆盖，不改代码。
- 改动文件：`governance_sources.py`，`governance_execution.py`，`apps/hub/tests/test_governance_execution.py`。
- 验收标准：V5 全部通过；`suppressed_count` 不变；治理目录与 execution.sqlite3 中扫描不到测试文本。
- 依赖：T-034-2；Q1、Q2、Q3。
- 最晚交付点：决定下达后开工，开工后 4 小时。

### T-034-6 同来源在途修订的范围处理

- 目标：按 Q4 的决定处理范围外的同来源在途修订原文。
- 输入：Q4 的决定（**待产品确认，未决定前不开工**）；`governance_plan.py`、`governance_preview.py`。
- 输出：A：预览把同来源在途回执纳入范围并参与 view_digest；B：预览和执行结果新增「保留的在途原文数」字段（新增字段）。
- 改动文件：`governance_preview.py`，`governance_plan.py`（仅 A），`governance_execution.py`（仅 B），`apps/hub/tests/test_governance_execution.py`。
- 验收标准：A：预览列出 rev2，确认后 rev2 原文被删除，按字节核验通过；B：rev2 原文保留，任务为 cancelled，计数正确。两种选择下，未确认的范围都不会被默默执行。
- 依赖：T-034-2；Q4。
- 最晚交付点：决定下达后开工，开工后 4 小时。

### T-034-7 收口：探针、门禁与文档

- 目标：完成工程门禁和文档，让 QA 可以验收。
- 输入：T-034-2 至 T-034-6 的产出。
- 输出：`scripts/probe_governance_execution.py` 增加 V1–V3 跨进程场景；验证记录定稿；本 ADR 状态改为「已实现」；`docs/verification.md`、`docs/roadmap.md` 补记。
- 改动文件：`scripts/probe_governance_execution.py`，`docs/reviews/2026-11-xx-inflight-ingest.md`，本 ADR，`docs/verification.md`，`docs/roadmap.md`。
- 验收标准：`make check`、Hub 全量、`make probe-governance-execution` 通过，跳过项单列；验证记录列出未验证项（NAS、多进程、Q 系列中未实现的选项）；文档链接检查通过。
- 依赖：T-034-2、T-034-3、T-034-4；T-034-5、T-034-6 按决定完成或明确标为未做。
- 最晚交付点：开工后 4 小时。

依赖顺序（修订二）：T-034-1、T-034-2a（已完成）→ T-034-2b（Q5 选 X′/Z 时需要，现在可以开工）→（等 Q5、Q7）T-034-2 → T-034-3（还要等 Q6）、T-034-4 可并行（Q1–Q4 决定后 T-034-5、T-034-6 也可并行）→ T-034-7。其余实现卡都要等 Q5。

顺序自审，非独立同行审查。
