# 治理关联范围预览

关联 FR-09、FR-10、FR-11；设计见 [ADR-0012](adr/0012-governance-scope-and-lineage.md)。此接口只读取关联范围，不提交治理意图、不删除或修改正文。实验性执行器和写入隔离已接通，见 [ADR-0032](adr/0032-governance-execution.md)；预览本身仍是观察结果，不构成执行授权。完整队列清扫等边界尚未完成，响应仍保留 `execution_available: false`，不能据实验接口存在宣称完整产品可用。

`POST /hub/v1/governance/preview` 使用现有管理员认证；设备读取凭证不能调用。浏览器会话继续要求同 Origin。输入为 scope_id、action（forget 或 purge）、record_ids（1–100 个已选记录），以及 offset / limit（每页最多 50 条）。可提供上一页的 expected_view；内容或关联变化时返回 409，不混合两份观察结果。设备协议仍为 v1，当前不增加设备治理权限。

响应包含 scope_id、action、view_digest、`consistency: observational`、`execution_available: false`，以及完整影响计数和分页 items。每项包含记录 ID、类型、状态、过滤后的短正文、纳入原因，以及是否保留原文：forget 保留原文供主动查阅，purge 将原文列入清理范围。view_digest 是带密钥的观察摘要，不是执行授权或并发写入屏障。

读取当前固定 SDK 的 Chroma / Kuzu 存储，包含全部生命周期状态、原文和显式来源／演化关系；普通搜索和语义邻居不参与扩展。单次扫描和响应有预算，达到预算明确报错，不返回截断的“完整计划”。不支持或不可用的后端、关闭但仍存在数据的图存储、损坏引用和身份冲突均拒绝生成预览。

工作／编程经验、所有状态的事实、共享轨迹和向量索引副本已纳入观察，见 [ADR-0019](adr/0019-productivity-governance-inventory.md)。事实替代链按版本处理，事实到经验的两个原生字段按同一方向合并；共享经验不会反向吞并其独立来源。已接纳原文及已核验的完整输出集合在同一配置事务中加入范围，原文或关系摘要损坏会失败，不能按展示引用的 32 条上限截断治理范围。

同项目、同来源／会话／记录的各 revision 作为原文版本相连，未提炼修订也进入影响范围；相同正文但不同来源身份不因此成为版本。预览后新增修订会使摘要失效，必须重新确认，不能静默扩大实际删除范围。

逻辑经验／事实沿用 `cmhp:`，原文沿用 `cmhe:`；索引副本以 `cmhv:`、轨迹以 `cmht:` 表示。后两类目前仅供管理员观察，不是新增设备历史读取接口。`memory_count` 包含索引副本，`raw_count` 包含原文和轨迹，均为本次影响对象数，不能当作去重后的记忆数量。轨迹只按完全相同的写入 request_id 关联原文；未知来源不按文本推断。

SDK 已捕获 extractor 实际使用的历史请求，以及确实提交的编程链轮次 ID；Hub 持久保存、核对身份，并把历史归档加入产出的输入父节点，见 [ADR-0020](adr/0020-exact-productivity-context.md)。旧结果没有捕获字段仍表示 unobserved，不能推断成空依赖。

已证明来源的编程链现可观察：`cmhc:r:<ID>` 是轮次，`cmhc:t:<ID>` 是完整线程存储，均为管理员 raw 记录，不新增设备历史读取权限。轮次关联实际原文和原生相邻版本，线程包含其全部轮次；线程 body 表示现有存储内容，不宣称逐字来自某条对话。选择一个来源时不会反向吞并其他轮次独立的原文。设计见 [ADR-0021](adr/0021-coding-chain-inventory.md)。

每条当前项目轮次都必须有完整上下文证明，缺失时返回 503 `governance_chain_lineage_incomplete`；已保存证明指向不存在的轮次时返回 409 `governance_reference_unresolved`。unavailable 捕获状态、缺失索引、损坏序号或无归属的旧 repo_symbol 索引也明确拒绝。其他项目正文不会加入范围，未知 workspace owner 不自动映射。确认后可用 POST `/hub/v1/governance/execute` 执行忘记，把预览的 `view_digest` 作为请求 `expected_view`，见 [ADR-0032](adr/0032-governance-execution.md)；`action: purge` 执行普通记忆彻底删除，生产力记录等未支持类型仍拒绝。SDK 开启尚未建模来源的派生写入，或目标项目存在未建模实体副本时返回 503 `governance_derivation_unmodeled`，见 [ADR-0031](adr/0031-derived-writer-coverage.md)。完整模型输入依赖、无归属索引迁移、在途清扫和完整删除覆盖仍待完成，不能用观察结果替代完整验收。

合并决策输入以 `cmhi:<receipt>:<stage>` 作为管理员 raw 记录：保存当时的完整快照及实际片段，核验生产请求、正文摘要、项目／来源身份和类型化产出，再连到生产原文及其输入阶段。旧快照不经过可变记忆 ID 回溯，因此后来更新的来源不会倒灌进旧阶段。只有 used 阶段影响成功产出；fallback／skipped 的历史副本仍参与原文清理观察。新捕获 unavailable、缺生产原文／产出、来源核验失败或循环返回 503，超限返回 409；旧 unobserved 仍不表示空依赖。授权记忆读取也会补充这些已核验的间接原文 ID，沿用 32 条展示上限；缺证据时返回 read_unavailable，不输出似是而非的来源。见 [ADR-0023](adr/0023-reconcile-input-lineage.md)。

在线预览不保证跨存储快照一致性。记录中的来源缺失会标记 unresolved_origins，不能将缺失来源当作空依赖。执行器先等待已领取 worker 结束，再进入 SDK/接纳独占区重算范围并绑定持久计划；存量 unknown 和旧队列清扫仍未完成。该预览不能作为完整忘记／彻底删除验收的替代。

请求示例（使用合成 ID）：

```json
{"scope_id":"global","action":"forget","record_ids":["synthetic:decision"],"limit":20}
```

分页必须把上一页的 view_digest 填入 expected_view。摘要绑定当前项目的全部已观察记录、正文、状态、显式来源及版本边；新增派生也会使摘要变化。省略 expected_view 表示重新观察，并不保证接续上一页。摘要不保存正文，也不是可转移的执行凭证。

错误使用固定代码，不回显 SDK 异常或请求正文：选中记录／项目不存在返回 404；引用缺失或跨项目、扫描超限、分页观察变化返回 409；请求格式错误返回 422；后端不可用、身份冲突、引用格式损坏、过滤失败或繁忙返回 503。设备凭证调用返回 403；既有管理员浏览器 Origin 限制继续适用。

当前固定适配 hy-memory 2.0.5.dev3，最多观察 10,000 条记录、100,000 个引用，正文累计预算 32 MiB；短正文每条最多 512 字符，先完整过滤再截取。超过单条过滤预算同样显式失败。单进程同时执行一个预览，SDK 取消路径沿用上游的线程等待机制。范围内存储损坏可能使整次预览不可用，不把部分结果标成完整结果。

复用 SDK 的 Chroma ID 映射（非 UUID ID 对应确定性存储 UUID）、原生工作线程池、元数据解码和 Kuzu 参数查询；JSON 引用另用已有严格解析器拒绝重复键。跨后端同 ID 的项目归属只读取 ID／归属来校验，不取出其他项目正文。输入归档新增独立扩展 schema 1，与旧 lineage／context 格式并存；完成的旧任务可从既有 SDK 结果补核对，不重复提炼。旧运行时忽略新扩展读取原业务格式；新版不会把缺失归档当作空依赖。

需要可靠重试时，管理员客户端应在首次执行前生成 operation_id，并在响应丢失后保留完整确认请求重试。同一 ID 绑定操作者、项目、动作、选中集合和 expected_view；更换这些参数返回 409 governance_operation_conflict。旧意图缺少请求指纹时返回 409 governance_operation_request_unproven，不从派生后的范围猜测初始请求；旧操作自动恢复仍可按已签名意图继续。恢复后只有实际完成才返回 completed，孤立计划被丢弃不等于执行成功。见 [ADR-0036](adr/0036-governance-request-identity.md)。

生产力范围的观察不等于执行支持。完整关联闭包含 `cmhp:`、`cmht:`、`cmhc:` 或 `cmhv:` 时，forget 在写计划/意图前返回 503 `governance_forget_kind_unsupported`；同操作重试及旧已提交操作恢复也不会报告新的成功或开放就绪。独立实测确认原实现只挡 Hub 读取，仍可能将这些记录作为 SDK 内部合并输入，因此当前先明确拒绝，完整忘记能力继续实现。普通原文及输入快照不因保留前缀而被一并拒绝。普通节点的 completed forget 则在启动时重新应用抑制，防止旧业务备份恢复其可提炼状态，见 [ADR-0032](adr/0032-governance-execution.md)。
