# ADR-0042：已有诊断文件的范围证明与恢复

日期：2026-11-10。状态：只读前置接口已通过本地全量、独立审查及本地/NAS 隔离安装验收，尚未接通物理文件清理；证据见[验证记录](../reviews/2026-11-10-diagnostic-inventory.md)。关联 FR-10、FR-11、ENG-01；承接 [ADR-0040](0040-diagnostic-write-quiescence.md) 与 [ADR-0041](0041-strict-diagnostic-content-policy.md)。

## 问题与复用

严格诊断策略只约束新输出。旧 JSONL、SDK 服务日志及其恢复副本仍可能含正文；现有 `Purger.present` 只检查数据库、向量和队列，不能发现仅恢复旧日志的情况。已完成操作不能因此跳过实际文件检查。

固定上游提交沿用 manifest。复用原 `PipelineLogWriter` 的逐行格式、SDK `SingleLineFormatter` / `JsonLineFormatter` 和既有 handler，不创建第二套日志系统。复用 Hub 的完整任务暂停、共享 SDK 准入、诊断排空、签名治理意图和请求证书。上游没有精确范围授权、与治理操作绑定的文件重写进度或旧文件恢复重放，因此这些协调职责在 Hub 实现。

本项分开交付：先实现无副作用的请求选择证明，再接文件分类与范围预览，最后接持久清理状态、迁移和真实替换。每步记录已执行证据；前置接口或解析器通过不等于物理删除完成。

## 已决定：无正文、无行写入的范围证明

`InflightGovernance.prepare_cleanup_selectors(scope, *, signed=False, completed=False)` 返回两个 frozen / slots 数据对象：

- `CleanupSelector(job_id, request_id, input_digest, disposition)`，disposition 仅为 `cancel_unpermitted` 或 `terminal_cleanup`。
- `CleanupPreparation(selectors, rule_targets)`，两者为稳定排序的 tuple。rule_targets 复用现有策略标记和逐 job 证书，不引入第二种业务删除授权。

接口复用 `_inspect` 和 `_prove_live`：整条 `cmhe:` 选择集合必须与已证明任务集合严格相等，签名调用核对每条既有证书，不能将局部 snapshot 的 owner request 升格为整请求选择。`completed=True` 仅适用于 signed scope，并使用现有 anchored 检查恢复正文已清除后的请求摘要。实际意图已提交、完整 record 集合与 operation 的关联仍由执行器验证；返回对象本身不是独立删除许可。

返回对象不得包含 Candidate、动态任务状态、输入对象或 `message_texts`。空 selectors 只表示没有完整写请求选择，不表示没有日志副本，也不自动通过文件覆盖检查。

当前 `_prove_live -> proof_in -> _lookup -> _witness` 会补写准入 witness。增加默认 `True` 的 `refresh_witness` 参数，纯接口传 `False`：仍校验绑定、签名、已存在 floor 与当前 checkpoint 的一致性，只跳过 witness 持久更新。合法落后的 witness 允许观察当前最新 proof，不能退回旧的 enrolled 状态；缺失、超前、篡改或绑定不匹配则拒绝。缺失外部锚不能成为“未获准调用”的负面证明，可经既有显式 verify_all 修复后重试。其他调用保持默认行为；纯接口复用现有 admissions 对象，不能在内部重新构造会自动 verify_all 的对象。

范围证明在原访问库写锁下取得一致证明，再对连接启用 `query_only` 防止未来意外写行；这不是用回滚掩盖写入。调用前后所有用户表、任务正文与状态、witness 和治理 head 必须相同。`prepare` 与 `apply` 可共用内部 contextmanager；`apply` 必须在同一访问库锁内继续取消 CAS，不能先证明、释放锁后再取消。既有返回值、异常合同和执行行为保持兼容。

## 文件覆盖原则

初始可成功范围仅包含所有旧正文记录均被完整授权的 SDK pipeline / service 文件；经完整封闭 schema 验证的 strict-v1 元数据记录可按原字节保留。任一未选旧正文、独立读取、混合自由文本或未知记录无法排除目标副本时，必须在业务与文件的破坏性变更前拒绝。

不同 request ID、缺少 memory_ids 或全文未命中均不是无副本证明。只删除五条目标、保留十三条来源未闭合的旧正文，仅能验证底层重写算法，不能宣称整个 purge 完成。SDK 文件字段没有逐行签名：request 匹配只建立候选关联，不能描述为密码学来源证明，也不能授权删除以后任意同 request 的正文。

新物理记录授权采用不可变 `RecordGrant(format, request_id, sha256, byte_length)`：摘要覆盖含末尾 LF 的原始记录字节。预检将完整范围内的候选 grants 去重、稳定排序，并将集合摘要纳入明确的诊断策略 content target；预览说明确认的是这些具体诊断记录及逐字节相同副本，不声称补造其来源证据。执行同时要求完整请求证书与精确 grant 命中；无关 strict 元数据保持原字节。

恢复原样文件、轮转改名或同一记录多次出现可按原 grant 清理。同 request 改了正文、时间或 JSON 空白就是未知记录，不推断变体；预检拒绝，不偷偷扩大范围。既有 completed 意图没有这项记录授权时不能静默补签，后续迁移只能赋予空物理记录集合，遇到旧正文仍须新的明确范围确认或报告不支持。

固定白名单覆盖原生 pipeline 日期文件与 SDK 服务日志的日期轮转；Core 多行旧日志、Docker 已保存 stdout 与外部日志是独立未闭合域。不遍历任意目录、不授予 Docker socket、不吞掉未知文件。整体执行接线须明确证明适用域，不能把局部 parser 的 closed 结果当作所有副本已清。

逐条使用二进制 LF 分隔，严格 UTF-8、禁止 JSON 重复键、校验字段类型/有限数值、完整末尾换行和预算。U+2028 不是记录分隔。SDK text 必须从首字节锚定原生 header，不反解正文中的转义或伪 header。仅有 strict 标志不足，须核对封闭键集、空正文和受限值。

第一步的纯解析接口为 `RecordInspector(requests, mode, grants)` 和单条 `inspect_record`。`propose` 对候选旧正文返回 candidate，`authorized` 还须精确命中传入的 grant 才返回 remove；这只是分类结果，模块不验主治理意图、不执行删除，不可把方法名当独立许可。任一未选、未知、读取步骤或伪 strict 记录返回 blocked。

只读域接口 `inspect_diagnostic_files(data_root, sdk_log_root, requests, mode, grants, budget)` 仅检查显式数据根下 `hy-memory/governed-v2/logs/default/日期.log` 和显式 SDK 日志根下 `hy_memory.log[.日期]`，不猜实际运行根；后续接线须从真实配置与资源取得这两个根。原生 Core 通常让两者共用同一个 logs 根，检查须合并白名单且每个文件只计一次；也支持两个独立目录，不能用分离的测试布局代替实际共根验证。返回冻结的域存在性、文件身份/摘要/计数和去重排序 grants，不返回正文。缺目录表示该检查域当前不存在，不证明全局没有副本。

绝对路径逐组件 `O_NOFOLLOW` 打开，包括祖先；调用方负责传入其实际规范路径，模块不 resolve 绕过链接。域内未知条目、软链接、文件硬链接、非普通文件、跨文件系统及扫描时变化拒绝。祖先只核对设备、inode 和模式，避免无关目录新增条目误触发；域内目录及文件核对完整快照。单轮默认上限为 256 文件、单文件 32 MiB、总计 128 MiB、单记录 2 MiB、10 万条记录及 1 万个精确 grants，可缩小但不提高。先按 stat 尺寸检查再流式读取，返回前复核全部域；调用者仍须持有写入排空边界，瞬时观察不是永久完成证书。

## 后续持久化与恢复合同（尚未启用）

计划将 execution 库升级到版本 2：保留原 operations/suppressed 签名，为每个 purge 增加唯一 cleanup 行；不可变 selectors、pass、manifest 和 pending/verified 使用独立 MAC 域并绑定原 intent。所有读取在同一数据库快照验证主操作、suppression 和 cleanup 的完整关系。主操作 completed 不可遮蔽 cleanup pending。

进度是重试缓存，不是防止合法旧快照回滚的独立证据。每次启动重新扫描正式文件与登记的 staging；verified 不提供跨启动免检权。pending 纳入 readiness、pending_operations 与每次 resume。

显式停服迁移须先验证全部旧签名、实际意图、集合及可恢复 selectors，再在一次事务内建表、回填和升版本；不能调用取消任务的 apply，不能补签或重写旧意图。可恢复的未提交 planned orphan 保留供原启动路径丢弃，不赋予文件写权。缺证据明确拒绝；新程序拒绝旧 schema，旧程序拒绝新 schema，须用实际制品验证。

文件操作需先登记 artifact 名字及预期保留摘要，再创建仅含保留字节的同文件系统私有临时文件，fsync 后替换并同步目录。不创建含目标正文的备份。已有临时文件只能按已登记结果或可证明的当前正式文件保留前缀处理；未知内容保留现场，旧 temp 不可覆盖新增尾部。多文件替换不是全局原子事务，失败保持 pending。

活动 handler 在同一同步线程按固定次序锁定、flush、复查身份与摘要、关闭旧 stream、替换并重开；保留 handler 对象及 formatter/filters/level/轮转状态。外层取消须等待实际线程结束。正式文件与 handler fd 必须指向相同 inode，后续安全日志必须仍可见。

## 验收与当前边界

第一步验证真实接纳队列的 pending/unknown/terminal、已清理正文恢复、证书替换/集合缺项、witness 缺失/落后/超前/篡改、对象不可变、无正文返回，以及调用前后数据库无行变动。原取消 CAS 和拒绝路径的回归继续执行。

后续文件实现必须同时包含成功与拒绝路径：真实原生格式、全选旧正文加无关 strict 元数据、未选旧正文提前拒绝、活动 fd 重开、路径/符号链接/硬链接/预算拒绝、多文件故障、仅恢复旧日志、登记 temp 恢复及未知 artifact 保留。安装镜像和 NAS 合成验证单独记录，不能以源码测试替代。

当前未改变 execution 数据库版本、HTTP 协议、purge 完成条件或常驻 NAS；不宣称旧文件、完整删除或 G1 已完成。
