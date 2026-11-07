# ADR-0032：忘记与彻底删除的执行

日期：2026-11-07。状态：第一段（忘记）已实现，本地验证通过；第二段（彻底删除）待实现。关联 FR-09、FR-10、FR-11、FR-12、ENG-01。沿用 [ADR-0011](0011-governance-authority-and-offline-sync.md) 的治理记录和 [ADR-0012](0012-governance-scope-and-lineage.md) 的方向性范围，不新增正文权威库。

## 实测：内容实际落在哪里

在真实 Hub 上用合成事件走完接纳、提炼、纠正，进程退出后逐个扫描数据、配置和治理目录的所有文件与 SQLite 表。同一段合成内容出现在：

| 存储 | 位置 | 由谁写入 |
| --- | --- | --- |
| 已接纳原文与各类输入快照 | config/remote-access.sqlite3：cmh_event_receipts.payload_json、cmh_event_chat_inputs 等 | Hub |
| 记忆正文、原始轮次、全文索引 | vector/chroma.sqlite3（embedding_metadata 与 FTS 表）及向量段文件 | SDK |
| 写入结果缓存 | cache.sqlite3：write_outcomes.result_json | SDK（补丁 0007） |
| 修订历史 | history.sqlite3：memory_history | SDK |
| 运行日志 | logs/ 下按日滚动文件 | SDK |

队列表 one-memory.sqlite3 在完成后由上游 clear_terminal_payloads 把原文换成摘要，未残留合成正文。治理库只有带密钥摘要，无正文。

因此"彻底删除"不能只调用 SDK delete：它会修复演化链、删向量和图，但把被删正文写入 history，write_outcomes 和日志也保留副本，Hub 自己的归档、输入快照也在其他库中。这些必须逐项纳入执行清单。

## 状态机

一次操作由治理记录中已签名的意图唯一标识（operation_id）。执行状态另存于治理目录的 execution 表，同样只保存摘要和记录 ID，不保存正文：

```text
accepted ──> suppressed ──> executing ──> verifying ──> completed
                 │              │              │
                 └──────────────┴──────────────┴──> failed_retryable（保持抑制，重启续做）
```

- accepted：意图已追加到治理记录。此刻起"有未完成意图"，原有 require_ready 隔离仍然生效，直到同一事务写入抑制集。
- suppressed：在 SDK 共享准入的独占区内重新计算范围（不使用预览缓存），确认与用户确认的 view_digest 一致后，把范围内全部记录 ID 写入抑制表。范围变化则拒绝，要求重新预览；不默默执行新范围。
- 忘记到此结束为 completed：抑制生效即满足"停止自动调用、禁止再提炼、原始历史仍可主动查阅"。
- 彻底删除继续 executing → verifying：按上表逐项清理，再扫描核验，全部无残留才 completed。

require_ready 从"有任何意图即整库隔离"改为"有未到达 suppressed 的意图才隔离"。抑制是持久的，进程重启后先加载抑制集再开放读取。

## 抑制点

Hub 的抑制表是权威；为了让 SDK 自身的召回和合并也看不到这些记录，同时把它们在 SDK 中设为 archived、is_latest=false。实测依据：

- 上游 reconcile 的候选检索（向量、关键词、查询三条通道）只取 active 与 superseded，链修复只处理 supersedes/superseded_by；固定版本中只有 System2 会写 archived，而 ADR-0031 已确认 System2 不可达。因此 archived 不会被合并路径重新激活，也不进入普通召回。
- status/is_latest 不在来源摘要字段内，只改这两项不会使已有来源证明失效，预览和来源图仍可遍历被抑制记录。
- 不用 shadow：上游 dynamic episode 会批量写 shadow，语义冲突。

抑制点：

1. SDK 记录：范围内的 memory 设 archived；原始轮次（l1_raw）同样设 archived，阻止 dynamic episode 等读取。设置前保存原状态，以便"恢复"操作（后续）。
2. Hub 读取：RemoteRead、UnifiedReadBridge 对抑制表中的记忆返回不存在；enrich 不列出被抑制的原文。history 读取对忘记的原文仍可读（用户主动查阅），彻底删除后不可读。抑制表检查独立于 SDK 状态，防止 SDK 侧状态被意外改回。
3. 再提炼：接纳新事件时，按项目内稳定来源身份（source_id、conversation_id、record_id）查抑制表，命中则拒绝入队，返回 409 governance_source_suppressed。换 ID 的同内容重导入需要内容指纹，属于第二段。
4. 上游管理网页的 /api/v2 读取不经过 RemoteRead；SDK 状态为 archived 后，其默认列表不再显示，按 ID 读取由 boundary 按抑制表返回 404。

## 彻底删除的清理清单（第二段）

按依赖顺序：派生记忆（SDK delete，记录回执）→ 原始轮次 → history 中这些 ID 的全部行 → write_outcomes 中引用这些请求的行 → Hub 归档与各输入快照 → 日志中的行（无法逐行定位时轮换并删除涉及时段的整个文件，记录被删文件）→ SQLite VACUUM。每一步写进度，崩溃后续做；SDK delete 已缺失按 already_absent 处理。最后用记录 ID 和正文摘要扫描核验，残留则 failed_retryable。

备份与恢复：恢复旧快照时治理目录不被覆盖；启动时对恢复回来的数据重放全部 completed 的彻底删除，未完成前保持隔离。这依赖第二段实现。

## 第一段实现与兼容

执行状态存于治理目录 execution.sqlite3（独立于业务快照，签名覆盖每行）。顺序为：保存计划（记录 ID）→ 追加治理意图 → SDK 设 archived → 标记 completed。各步之间崩溃时：

- 计划已存、意图未写入：启动续做时丢弃该计划。
- 意图已写入、未完成：计划行已对 Hub 读取生效；新 SDK 写入（来源授权回调）拒绝，直到续做完成。续做在 SDK 独占区内执行，挂在 Core 已有的未知写入核对循环上。
- 没有计划的意图（彻底删除、其他写入者、计划丢失）：沿用原整库隔离。

执行状态行被篡改、版本未知或文件不可读时，启动和健康检查失败，不会当作空抑制集。旧镜像（c0a14af 及以前）遇到任何治理意图即整库隔离，因此回退到旧镜像不会让被忘记的内容重新可读。

管理接口 POST /hub/v1/governance/execute 仅管理员，必须带预览返回的 view_digest；范围在独占区内重算并比对，变化返回 409。可带 operation_id 重试，已记录的操作直接返回结果。

## 分段交付与边界

第一段：execution 表、accepted→suppressed→completed、持久抑制集、四个抑制点、执行 API（仅管理员）、崩溃恢复、真实存储测试与 NAS 验证。第二段：清理清单、核验、内容指纹、恢复重放。

已经发送给 Agent 的上下文无法撤回；原 Agent 文件和用户自存副本不自动修改，遵守需求边界。多进程写入者由现有身份库事务串行化；SDK 独占区只覆盖本进程，执行期间不允许第二个 Hub 进程写同一数据目录（已有存储租约保证）。

顺序自审，非独立同行审查。
