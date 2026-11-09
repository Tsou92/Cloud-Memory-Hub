# ADR-0034：在途接纳与换 ID 同内容重导入

日期：2026-11-09。状态：提议，未实现；含 4 项待使用者决定（见「需要你决定」）。关联 FR-10、FR-11；补充 [ADR-0032](0032-governance-execution.md) 与 [ADR-0033](0033-durable-source-suppression.md)；对应[验证记录](../reviews/2026-11-09-source-suppression.md)「下一步」第 1、2 项。第 3、4 项（结果缓存清理依据、同 operation_id 冲突）不在本文范围。

## 证据（源码核对，未运行）

以下缺口由阅读当前分支源码得出，尚未复现；T-034-1 负责实测，结论以实测为准。

| 编号 | 事实 | 位置 |
| --- | --- | --- |
| E1 | 接纳在线程池中持 `remote-access.sqlite3` 写事务，事务内先读规则（`require_source_allowed`）再 `queue.submit` 和写回执。执行持 `SdkAdmission` 独占区（只拦本进程 SDK 调用，不拦接纳），规则写在 `execution.sqlite3`。两者之间没有顺序保证。 | `remote_ingest.py` `accept`；`governance_execution.py` `execute`；`sdk_admission.py` |
| E2 | 记录级规则只在接纳时检查。队列 worker 调用 HY 前的检查 `_source_writes_on(source_id)` 只带 source_id，Hub 授权回调只看 `require_writes_ready`（操作未完成时才拦）、旧规则可用性和来源授权。 | 上游 `api.py` `admit_hy_invocation`；补丁 0005；`app.py` `source_write_authorizer` |
| E3 | 被拒的 worker 若尚未调用 HY，上游用 `release_unstarted` 把任务退回 pending，不丢任务；有 `settle(state="cancelled")` 终态。非终态任务的 `payload_json` 保存完整 WriteRequest（含正文），只有终态任务会被 `clear_terminal_payloads` 换成摘要。 | 上游 `durable_queue.py` |
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
2. 清扫在 Hub 接纳库的写事务内进行（`ingest._database(writing=True)`，即 BEGIN IMMEDIATE，与接纳用同一把锁）：列出该项目的全部回执，用该意图的规则（来源三元组；若 Q1 选 A，再加内容指纹）匹配，对命中的回执调用 `queue.get(request_id)`：
   - 非终态且 `hy_invoked=0` → `queue.settle(job_id, state="cancelled", error_code="governance_source_suppressed", hy_invoked=False)`。
   - 非终态且 `hy_invoked=1`（running、unknown）→ 抛 `governance_ingest_in_flight`。操作保持 planned：规则仍然生效，`require_writes_ready` 继续拦截 SDK 新写入，由 resume 重试。
   - 终态 → 跳过。
3. 为什么能关闭竞态：计划提交之后才开始的接纳会读到规则，返回 409；计划提交之前已开始的接纳一直持有接纳库写锁直到回执提交，清扫拿到同一把锁时一定能看到这条回执。锁顺序与接纳一致（remote-access → one-memory），不会死锁。
4. 启动清扫：`resume` 在开放写入前对全部操作（含 completed）各执行一次清扫。清扫完成前，Hub 的 `source_write_authorizer` 返回 False，worker 按 E3 退回 pending，不丢任务（解决 G-C）。
5. 彻底删除额外两步：清扫在 purger 之前运行；purger 在 VACUUM 前循环调用上游 `clear_terminal_payloads()` 直到返回 0；字节核验的 needle 增加范围内原文的各条消息文本（≥16 字符，与现有阈值一致）。
6. 被取消的任务不自动重新提交。忘记时原文保留，用户仍可主动查阅。

### 决策 2：内容指纹规则（解决 G-D；是否启用及匹配口径待决定）

技术部分（无论 Q1、Q2 怎么选都适用）：

- 指纹源：执行时在独占区内，从范围内经校验的 cmhe 原文（与 `capture_sources` 同一次读取）逐条取消息文本。取隐私过滤后的文本，做 NFC 规范化、去首尾空白、连续空白合并为一个空格；规范化后不足 16 字符的消息不生成指纹。
- 存储：`journal.target(scope, "content", json.dumps([1, text]))`，与来源摘要一起放入同一签名 `Intent.targets`。不存原文、不存明文摘要，不新增表或列。`suppressed_count` 仍只统计物理记录。
- 检查点：与来源规则相同，在设备身份、来源授权通过之后、提交队列之前；同时纳入决策 1 的清扫匹配。
- 命中返回 409 `governance_content_suppressed`（新错误码，与来源命中区分，便于管理员排查）。

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
    def sweep_inflight(self, ingest, queue, intents) -> int: ...  # 返回取消数；遇到已调用 HY 的任务抛 governance_ingest_in_flight
    ready: bool  # 启动清扫完成前为 False，source_write_authorizer 据此拒绝
```

| 接口 | 变化 | 类型 |
| --- | --- | --- |
| POST 事件接纳 | 新增 409 `governance_content_suppressed`（仅 Q1=A） | 新增错误码 |
| POST /hub/v1/governance/execute | 新增 503 `governance_ingest_in_flight`，可带同一 operation_id 重试；响应字段不变 | 新增错误码 |
| 队列任务 | 命中规则的在途任务终态为 `cancelled`，`last_error=governance_source_suppressed` | 数据语义 |
| Intent.targets | 新写入 `content` 类型目标（已有 TargetKind）；总数仍受 10 000 上限约束，超限在写意图前返回 409 `governance_scan_limit`，不能落到通用 503 | 签名数据语义 |

数据库列、schema_version 与治理协议不变。

## 失败模式

| 场景 | 行为 |
| --- | --- |
| 清扫时命中已调用 HY 的任务 | 503 `governance_ingest_in_flight`；操作 planned，规则生效、写入受阻；该任务被核对完成后由 resume 续做。产出若已写入 SDK，下一次重试会重新计算范围——范围变化按 ADR-0032 返回 409，要求重新预览 |
| 取消后、操作完成前崩溃 | 取消是终态，resume 重跑清扫时跳过已终态任务，幂等 |
| `settle` 不允许无 owner 把 running 改成 cancelled | 按「已调用 HY」处理（重试）；T-034-1 实测 |
| 只有队列任务、没有回执（接纳事务在 submit 后回滚） | 清扫按回执查找，找不到这类任务。这是已有缺陷（lineage 会报 `lineage_original_missing`），不在本文范围，另行登记 |
| 隐私过滤策略版本变化 | 同一文本过滤后不同，指纹不命中。在验证记录中如实写明；升级策略时需要重新评估 |
| 指纹被用作探测（设备能否知道某段话已删除） | 单用户系统，设备本来就持有该内容；HMAC 带实例密钥，离线无法穷举 |
| 启动清扫失败 | 授权回调保持 False，健康检查报错，不开放提炼 |
| 规则检查成本 | 每次接纳遍历本项目全部操作并计算 HMAC；G1 规模可接受。`ponytail:` 线性扫描，操作数上千时改为按项目缓存规则集 |

## 迁移与回滚

- 无表结构迁移。已有的 0033 操作没有内容指纹：忘记操作的原文仍在，可在规则检查时现算；彻底删除操作的原文已不存在，无法补齐（见 Q3）。
- 回滚到不含本修复的镜像：内容指纹不再检查，在途任务不再清扫，G-A 到 G-D 重新出现。已取消的任务保持取消，已写入的意图不受影响。回滚前必须停服备份，并在验证记录中注明。
- 取消在途任务属于不可逆的数据效果：忘记时可借助保留的原文和上游 `readmit_cancelled_permission_confirmed` 手动恢复（尚无工具）；彻底删除时不可恢复，这符合彻底删除的语义。

## 需要你决定

| 编号 | 问题 | 选项 A | 选项 B | 影响的卡 |
| --- | --- | --- | --- | --- |
| Q1 | 彻底删除或忘记后，同内容换了来源/会话/记录 ID 再次上传，是否拒绝？ | 拒绝（409，按内容指纹）。满足 FR-11「重新导入不复活」；代价是用户以后原样重说同一段话（≥16 字符）也会被拒 | 允许接纳，只按稳定来源身份屏蔽（保持现状）。不误伤；FR-11 的重导入场景仍不满足，需在验收中写明 | T-034-5 |
| Q2 | 一个事件含多条消息时，怎样算命中？（仅 Q1=A） | 任一条命中就拒绝整个事件。漏判少；新消息里引用了旧原文，整个事件都会被拒 | 所有可指纹的消息都命中才拒绝。误伤少；重导入时把旧消息和新消息合进同一事件就能绕过 | T-034-5 |
| Q3 | ADR-0033 之后、本修复之前完成的彻底删除没有内容指纹，怎么处理？（仅 Q1=A） | 对应项目的新上传返回 503，直到管理员处理（与 0033 旧操作一致，保守） | 这些旧操作只保留来源三元组保护，内容保护只对新操作生效，并在验证记录中标注。当前 NAS 是空实例，实际影响很小 | T-034-5 |
| Q4 | 彻底删除时，同来源但不在确认范围内的在途新修订（已接纳、未提炼），它的原文怎么办？ | 预览把这些修订纳入范围，view_digest 随之变化，用户确认的就是实际删除的内容 | 只取消提炼，原文保留在 Hub，在预览和结果中提示数量，由用户另行删除 | T-034-6 |

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
4. V4 已调用 HY：伪造命中且 `hy_invoked=1` 的任务，执行返回 503 `governance_ingest_in_flight`，操作 planned；任务核对后用同一 operation_id 重试完成。
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

### T-034-2 执行时清扫在途任务

- 目标：实现决策 1 的第 1–3 条和第 6 条。
- 输入：T-034-1 的结论；本 ADR「接口与数据契约」「失败模式」。
- 输出：`sweep_inflight` 由 `_finish` 第一步调用；`governance_ingest_in_flight` 映射为 503；目标超限映射为 409 `governance_scan_limit`。
- 改动文件：`governance_sources.py`，`governance_execution.py`，`apps/hub/tests/test_governance_execution.py`。
- 验收标准：V1（忘记部分）、V2、V4、V6 通过；T-034-1 中 G-A（忘记）、G-B 的测试转绿；已有治理测试全部通过；清扫不触碰其他项目或无关来源的任务（有断言）。
- 依赖：T-034-1。
- 最晚交付点：开工后 4 小时。

### T-034-3 启动清扫与授权就绪标志

- 目标：实现决策 1 第 4 条，覆盖恢复旧备份。
- 输入：T-034-2 的 `sweep_inflight`。
- 输出：`resume` 在开放写入前对全部操作清扫一次；`SourceSuppression.ready` 接入 `app.py` 的 `source_write_authorizer`；未接线时默认值保持原行为。
- 改动文件：`governance_execution.py`，`governance_sources.py`，`app.py`，`apps/hub/tests/test_governance_execution.py`。
- 验收标准：V3 通过；就绪前 worker 不调用 HY，任务退回 pending 而不是 failed；`test_remote_ingest.py` 全部通过。
- 依赖：T-034-2。
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

依赖顺序：T-034-1 → T-034-2 → T-034-3、T-034-4 可并行（Q 决定后 T-034-5、T-034-6 也可并行）→ T-034-7。

顺序自审，非独立同行审查。
