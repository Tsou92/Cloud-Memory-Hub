# ADR-0032：忘记与彻底删除的执行

日期：2026-11-07。状态：忘记与彻底删除均已实现，2026-11-07 完成本地安装与 NAS 验证，见[验证记录](../reviews/2026-11-07-governance-execution.md)。关联 FR-09、FR-10、FR-11、FR-12、ENG-01。沿用 [ADR-0011](0011-governance-authority-and-offline-sync.md) 的治理记录和 [ADR-0012](0012-governance-scope-and-lineage.md) 的方向性范围，不新增正文权威库。

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

### 残留实测

用唯一合成标记走完接纳→提炼→完成，再对提炼出的事实和原始轮次调用 SDK delete（两次都返回 success、vdb_deleted），进程退出后扫描所有文件字节和每张表每一列：

| 位置 | SDK delete 后 | 处理 |
| --- | --- | --- |
| chroma.sqlite3 正文、元数据、FTS 表 | 已清除；文件页仍有旧字节，VACUUM 后消失 | delete + VACUUM |
| chroma.sqlite3 embeddings_queue（Chroma 写前日志） | 残留 3 行，含完整元数据和正文；max_seq_id 已追平，按设计可清除 | 按 ID 删行 + VACUUM |
| history.sqlite3 memory_history | 增加 2 行，delete 把被删正文写入 old_memory | 按 memory_id 删全部行 + VACUUM |
| cache.sqlite3 write_outcomes | 本例 0 行；含正文时按 request_id 定位 | 按请求删行 + VACUUM |
| one-memory.sqlite3 | clear_terminal_payloads 后仍有 1 处 verbatim 字节在空闲页 | VACUUM |
| remote-access.sqlite3 cmh_event_receipts 及输入快照表 | Hub 归档原文与快照 | 见下 |
| logs/hy_memory.log | 上游 client 以 INFO 记录 messages detail（完整用户消息） | 见下 |
| logs/default/日期.log（JSONL） | EXTRACT、S1_L1_UPSERT、RECONCILE_APPLY 的 parsed 字段含正文 | 见下 |
| 向量索引段文件 | 合成 1500 条向量删除一条，原向量字节只在 chroma.sqlite3 队列中，段文件未检出 | 随队列清理 |

### 两项需要决定的设计点

1. **日志**：上游在 INFO 级别把完整消息和抽取结果写入日志，日志按天滚动保留 15 天，不能按记录精确删除。可选方案：(a) 补丁让这几处只记录长度/ID，彻底删除时无需处理日志，且日常也不在日志中保留正文；(b) 保留日志，彻底删除时删除涉及日期的整份日志文件。倾向 (a)：日志本不应是正文副本，(b) 会连带删除无关诊断信息。
2. **Hub 输入快照**（实测细化）：在三条链式合成轮次上对每个原文做彻底删除预览。方向性范围已经把"把该原文作为历史输入的后续快照"（cmhj:/cmhm:）纳入，范围外的行不含被删正文，也没有引用被删请求。但后续轮次的原文本身（cmhe:）不在范围内，它的快照（cmhj:）在范围内——这正是需要墓碑的情况：保留后续原文与其产出，删除其快照中的被删历史条目。原方案：后续请求的快照会包含被删记录作为历史输入，即快照跨回执引用正文。彻底删除需从这些快照中移除对应条目，而快照摘要不可改写（来源证明依赖它）。方案：被删条目替换为带密钥的墓碑（ID + purged 标记），快照摘要重新计算并在 execution 表登记"已因治理改写"，来源图把墓碑视为已删除父节点而非缺失。这改变了快照"不可变"的既有合同，需要同步修改 ADR-0022/0024/0027/0029 的兼容说明。

原定顺序：派生记忆（SDK delete）→ 原始轮次 → history、embeddings_queue、write_outcomes 对应行 → Hub 归档与快照墓碑 → 日志（按上面决定）→ 全部相关库 VACUUM 与 WAL 截断。每步写进度，崩溃后续做；SDK 已缺失按 already_absent。最后用记录 ID 和带密钥正文摘要扫描核验，残留则保持隔离并可重试。

备份与恢复：恢复旧快照时治理目录不被覆盖；启动时对恢复回来的数据重放全部 completed 的彻底删除，未完成前保持隔离。这依赖第二段实现。

## 第二段实现（彻底删除）

- 日志：当时按用户选择采用补丁 0017，认为非 debug 文本日志与 JSONL 不再记录正文。后续诊断调查证明此结论不完整，不能据此跳过日志清理；见下方 2026-11-10 更正。
- 快照：实测范围已包含引用被删原文的后续快照，因此没有实现条目级墓碑改写，而是删除这些快照的整行，并在 `cmh_purged_snapshots` 登记。来源图把登记过的快照视为"因治理删除"而非缺失证据，补录循环跳过它们，提交路径拒绝重写。这比原计划少改写一份不可变合同：被保留的快照字节从不修改。代价是后续轮次失去该次历史输入的记录，但它们自己的原文和产出保留。
- 清理顺序：SDK delete → Hub 接纳原文与该回执的所有快照、产出关联 → 范围内其他回执的快照 → Chroma 写前队列、修订历史、写入结果缓存 → 五个库 VACUUM 与 WAL 截断 → 按正文字节核验。执行进程知道正文时核验字节；续做时按 ID 重复同一组幂等步骤。
- 恢复防复活：每次启动在服务前检查已完成的彻底删除；任何对应记录重新出现（例如恢复了旧业务数据备份）时，在 SDK 独占区内重跑清理。抑制表本身也让这些记录在重跑前不可读。
- 范围外类型：生产力记录（cmhp/cmhc/cmht/cmhv）位于 SDK 侧独立数据库，物理清理未实现；范围含这些记录时在写入意图前拒绝（governance_purge_kind_unsupported），不报告部分完成。

### 已接受的已知限制：HNSW 预分配空间

2026-11-07 用户决定：暂不修复，作为已接受限制记录，不阻塞 G1。理由：NAS 为用户自有设备，Hub 所有读取接口（设备读取、搜索、管理网页、Agent）均不可达这些字节。影响范围如实记录如下，彻底删除的结果说明不得宣称"磁盘上无任何残留"。

- 位置：数据卷中 Chroma 段目录的 data_level0.bin 未使用槽位，持久化在磁盘上，重启不消失；只有新元素占用该槽位或段重建时被覆盖。
- 随备份留存：卷备份、自动备份与完整导出会原样复制该文件。
- 可被直接读取：能访问 NAS 文件系统或硬盘的人可用字节搜索找到片段；这类人本来也能读取全部未删记忆。
- 内容性质：进程近期释放的内存片段，不完整、不稳定（实测约半数运行出现）；也曾见到 SQL 语句与 JSON 元数据，未专门验证是否可能包含凭证，不能排除。
- 重新评估条件：实现受控备份清理或完整导出时，与备份中的删除一并处理（对删除涉及的集合重建段文件）；或 Chroma 升级修复预分配初始化；或用户提高彻底删除要求。

原记录：

Chroma 1.5.9 的 Rust HNSW 写入器预分配 data_level0.bin，未使用的槽位保留未初始化内存，可能含有进程近期释放的任意字节。在 8 次重复运行中约半数在未使用槽位检出被删正文片段；已计数元素槽位中从未检出（测试固定此点）。这不是存储的记录，任何读取路径不可达，但磁盘上字节仍在。处置方向：对删除涉及的集合在维护窗口重建 HNSW 段（导出向量后重建文件），或向上游报告。在此之前不能宣称"磁盘上无任何残留字节"。

## 第一段实现与兼容

执行状态存于治理目录 execution.sqlite3（独立于业务快照，签名覆盖每行）。顺序为：保存计划（记录 ID）→ 追加治理意图 → SDK 设 archived → 标记 completed。各步之间崩溃时：

- 计划已存、意图未写入：启动续做时丢弃该计划。
- 意图已写入、未完成：计划行已对 Hub 读取生效；新 SDK 写入（来源授权回调）拒绝，直到续做完成。续做在 SDK 独占区内执行，挂在 Core 已有的未知写入核对循环上。
- 没有计划的意图（彻底删除、其他写入者、计划丢失）：沿用原整库隔离。

执行状态行被篡改、版本未知或文件不可读时，启动和健康检查失败，不会当作空抑制集。旧镜像（c0a14af 及以前）遇到任何治理意图即整库隔离，因此回退到旧镜像不会让被忘记的内容重新可读。

管理接口 POST /hub/v1/governance/execute 仅管理员，必须把预览返回的 view_digest 作为 expected_view；范围在独占区内重算并比对，变化返回 409。可带 operation_id 重试；后续 [ADR-0036](0036-governance-request-identity.md) 要求核对完整请求指纹并重新读取实际状态，不能仅因 ID 已记录就返回成功。

## 分段交付与边界

第一段：execution 表、accepted→suppressed→completed、持久抑制集、四个抑制点、执行 API（仅管理员）、崩溃恢复、真实存储测试与 NAS 验证。第二段：清理清单、核验、内容指纹、恢复重放。

已经发送给 Agent 的上下文无法撤回；原 Agent 文件和用户自存副本不自动修改，遵守需求边界。多进程写入者由现有身份库事务串行化；SDK 独占区只覆盖本进程，执行期间不允许第二个 Hub 进程写同一数据目录（已有存储租约保证）。

顺序自审，非独立同行审查。

## 2026-11-09 恢复补充：已完成的忘记也必须重放

独立合成实验已复现：保留新治理目录、恢复 forget 前的业务数据后，操作仍 completed 且服务 ready；Hub 对旧记忆返回 404，但 Chroma 的 fact/raw 状态恢复成 active/shadow。通过真实 Core Drain 和 SDK add 提交一条新事件，提炼输入的 history_context 再次含有已忘记的原文。模型与 supervisor 为受控替身，不能以 Hub 读取被挡住代替 SDK 内部输入验证。

启动恢复应在同一治理锁、Drain 暂停和 SDK 独占区内，先通过全部作用域的未建模副本检查，再对 completed forget 重用原 `_suppress_sdk`，按原签名物理记录恢复 archived/is_latest=False；不新增意图、不改已确认范围、不删保留原文。目标已缺失可跳过，更新失败不能开放 ready。completed purge 保持既有删除重放，重复启动幂等。

恢复写入只能作用于签名范围及对应项目。重放前应核对实际节点 ID 与 user_id；错误归属或损坏身份失败关闭，不把恢复来的其他项目记录修改为 archived。该补充只修复已有普通 SDK 节点的抑制重放，不宣称生产力侧所有内部输入或 ADR-0034 的旧队列已经覆盖。

验收需保留原失败复现并验证：重开后 fact/raw 已归档、Hub 原文仍可主动查阅、真实后续提炼输入不含忘记正文、无关节点不变、重复启动无新意图、更新失败不就绪，以及错误项目归属拒绝。后续安装探针增加已完成 forget 的旧业务数据恢复。

## 2026-11-10：诊断日志覆盖更正

0017 只修改了部分正文输出位置，不能证明现存或恢复的 debug JSONL/text 文件没有副本；SQLite 请求诊断表、独立读取日志和外层取消后仍执行的后台 I/O 也需分别处理。原先“彻底删除无需处理日志”的判断撤回，不把默认级别或正文片段扫描作为完整副本证明。

同请求 SQLite 诊断表已补入定向清理与仅缓存恢复，见 [ADR-0035](0035-purge-cache-recovery.md)。后台 pipeline 真实完成等待、搜索准入和关闭重试已补入 [ADR-0040](0040-diagnostic-write-quiescence.md)。已经落盘的文件、独立读请求副本及进程输出仍须后续定位和清理，不能由上述两项已通过测试外推为完整删除覆盖。

## 2026-11-09 生产力忘记的内部输入缺口

另一个独立真实存储实验覆盖 working 与 coding：HTTP forget 返回 completed、Hub fact 读取被拒绝，但独立 CodingMemoryStore 中的 fact 仍 active；真实 WorkingFactReconciler 的候选读取及模型提示仍包含该 fact。仅替换模型响应以截获合成输入，无需恢复旧备份即可复现。原因是现有执行器跳过所有 Hub 保留前缀，而生产力记忆与索引并不都由主 Chroma 节点状态控制。

在补齐独立存储、轨迹、编程链及内部提炼输入的抑制前，不允许将含 `cmhp:`、`cmht:`、`cmhc:`、`cmhv:` 的忘记范围报告完成。新请求在落计划/意图之前明确返回 `governance_forget_kind_unsupported`；已提交旧 planned 或 completed forget 的恢复遇到这些类型则保留现场、拒绝就绪，不重签或伪造旧状态。普通原文和输入快照 `cmhe:`、`cmhi:`、`cmhj:`、`cmhm:`、`cmhb:` 不因此误判为不支持。

这项保护不等于 FR-10 的生产力忘记已实现，也不取消该需求；后续必须复用上游相应存储/状态合同，使实际模型输入和自动调用都受抑制，再解除保护。测试分开记录完整能力缺口与提前拒绝合同，不能用 Hub 404 或拒绝请求替代生产力忘记成功验收。

保护检查必须使用完整关联闭包，并覆盖新执行、`_finish`、启动已提交计划及 completed 操作、已完成请求重试。ExecutionStore 的计划签名不等于日志意图已提交；孤立计划保留原丢弃与重验语义，不能因其包含生产力 ID 就永久隔离。已提交操作的种类检查全部通过后才进入实际存储修改。
