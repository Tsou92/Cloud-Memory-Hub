# ADR-0045：真实授权恢复图与联合文件重放

日期：2026-11-10。状态：组件已实现，完整 Hub 与同镜像本地/NAS 隔离安装验证通过；正式服务接线未完成。关联 FR-10、FR-11、ENG-01；接续 ADR-0043/0044，不代替其完整产品接线要求。证据见[验证记录](../reviews/2026-11-10-diagnostic-recovery.md)。

## 问题与复用

同一个 SDK 日志可包含不同项目、不同父删除以及多次 repair 已明确授权的记录。现有文件引擎只读取单个 cleanup authority，未授权 legacy 记录必须阻塞；依次调用父操作、R1、R2 会互相阻塞。原父的空 grants 不能改写成后来 repair 的授权，repair 也不能进入普通业务删除或在途取消路径。

复用现有 Store2 主链/父关系验证、不可变 CleanupAuthority、CAS 进度、两域安全扫描器、严格记录分类器、同步 handler gate 和文件替换算法。固定上游不存在 Hub 的父操作及 signed repair 关系，因此只在 Hub 增加恢复图及多贡献者 pass；不新增日志、队列、文件后端、数据库表或上游补丁。

## 同快照恢复图

`RecoverySnapshot.committed_digests` 来自同一次已验证的真实 journal 快照，不在外部第二次查询拼装。只读图分离已提交的普通业务操作、repair、父子族、孤立计划、无计划主意图和 pending。已提交但尚未 completed/verified 的合法 repair 可以贡献授权，否则首次修复会循环等待。

新增 `CleanupContributor(operation_id, intent_digest, cleanup_digest)`，引用真实主链摘要和既存不可变选择摘要。解析器逐项复核实际 committed、非 orphan、purge cleanup、父链/ledger/scope/选择关系；允许不同父及 scope 的合法独立贡献者。相同 request 映射不同 namespace 或出生选择、相同 job 映射不同 request/正文摘要时整次拒绝，不覆盖或猜测。

`ResolvedCleanup` 保存规范排序去重的贡献者、WriteRequest 与精确 RecordGrant，不是新的用户授权。`build_recovery_graph` 保留 unplanned 供只读维护诊断；批量文件入口遇 unplanned 必须在文件 I/O 或进度写入之前拒绝。引用解析器本身不因无关 unplanned 隐藏合法的已存 manifest，便于保留现场和诊断。

恢复图的操作、贡献者、去重请求与 grants 分别有 10000 硬上界，超限返回明确错误而不截断。继续使用既有全域文件/字节/记录预算，不把单操作预算乘操作数。图预算不声称替代现有数据库开库与验证的资源边界。

## 冻结 pass 权限与兼容性

CleanupManifest 增加可空 contributors：`None` 仍序列化为完全相同的 v1 JSON；非空规范 tuple 使用 v2，并要求物理 pass owner 在引用集合内。现有 SQL schema2、capability、主 intent、旧 authority 和其 MAC 不重签。v1 的隐式贡献者仅为真实 owner；新 v2 的 begin/checkpoint/verify 均在 Store2 原 mutation 快照内重新核验冻结引用，checkpoint 不得修改引用。

新 repair 只能加入新 pass 的贡献者，不能追溯补权到旧 pass。旧实现遇 manifest v2 必须拒绝而非降级忽略；已有 v1 继续按原 canonical JSON/MAC 验证。该格式扩展是本地恢复进度合同，不是新的反回滚 witness；每次重放仍读取真实主链并重新扫描文件。

同一恢复快照中相同规范贡献者集合只解析一次，每份 manifest 仍检查自己的 owner；缓存仅属于本次函数调用，不进入对象、快照或全局状态，不跨核验复用。不同 v1 隐式 owner 仍分别验证，避免多个 v2 receipts 的共同引用造成不必要的重复摘要与选择检查。

## 一次全域预检、旧 artifact 证明与替换

```text
真实 recovery snapshot → 有界恢复图 → 当前完整精确授权集合
            ↓                          ↓
所有旧 manifest/冻结 refs ──→ 全域只读扫描与所有旧 stage 证明
                                        ↓ 全部通过才有首次变更
                    退休已证明 stage → 新 v2 物理 pass
                                        ↓
             原 stage/fsync/replace/rebind → 全域复扫
                                        ↓
                    每个已提交 purge 的文件 receipt verified
```

所有已存 manifest 的冻结引用先验真，聚合 stage 的 owner/pass/name 归属。未知 artifact、多重归属、不安全路径或预算超限均保持现场。在首次 unlink 前，实际存在的每个旧 stage 都必须通过原注册摘要/长度、当前对应正式文件的完整保留前缀、stage 自身的安全前缀及 mode/device/link/identity 检查。同路径多个旧 manifest 必须同时证明；不能先删一份，再发现另一份不成立。

当前 authorized 分类器唯一 keep 分支为严格无正文元数据，grant 对 strict 行无效；旧合法 stage 因而只能含 strict 保留字节。后来新增 grant 可解除新 legacy 的扫描阻塞，但不会改变相同正式字节的 keep 集合。因此联合预检得到的 strict retained 可用于上述旧摘要/前缀证明，绝不以新 grant 给旧 stage、未知 artifact 或唯一副本补权。没有实际 stage 时维持现有轮转、改名和已完成进度的重新扫描合同。

历史登记名的数量不等于实际存在的 stage 数量。沿用原预算语义：正式文件合计最多 256 个/128 MiB，实际 stage 另合计最多 256 个/128 MiB；两类均跨全部操作统一计算，不随操作数相乘，也不将此说成两类合计仅 256 个/128 MiB。所有实际 stage 证明通过后逐个退休，并以原 receipt/revision CAS 标记 abandoned 和核验缺席，再创建新 pass。

一次物理 pass 持有全局规范贡献者，由一个确定的已提交 purge 作为进度 owner。复用原 `_stage → _prove_stage → replace → dirfsync → adopt → final scan`；同一原生 gate 对每个路径只 suspend/adopt 一次，不循环调用单操作引擎。其余操作在联合清理及真实复扫后取得各自无 artifact 的 v2 pass/verified receipt。多行进度不是一个全局原子提交；中断保留 mixed/pending，下次重新核全图、文件与 artifacts，不能凭任一 verified 快退。

文件组件不调用主 `complete`、不取消队列、不重删业务对象。旧父 authority、空 grants、原 intent 和 checkpoint 保持原样；完整 purge 完成仍需业务重放、其他受管输出域及全局 pending 终检。

没有已提交 purge 时返回空结果，不扫描文件，也不产生“诊断域已验证”的证据；正式启动仍须独立验证输出域，orphan 丢弃仍须取得真实 artifact 缺席证明，不能用空结果代替。

## 验收与失败处理

- 真实父 P 与 R1/R2、不同父/不同 scope 共根，完成联合文件清理；父空授权不变，planned repair 可贡献，未提交 repair 不贡献。
- 实际主链摘要、cleanup 摘要、父关系、跨项目请求身份、冻结贡献者或 MAC 漂移必须在破坏前拒绝；v1 精确兼容、旧组件拒绝 v2。
- 新 repair 与旧 stage 并存可安全续做；第二个 stage 无法证明时，第一份也不删除。未知、篡改、唯一副本、冲突前缀保持现场。
- stage 写入、替换、重绑定、逐操作 verified 中断后重开；原样日志恢复、轮转、新 strict 尾部、不同新字节变体，以及所有旧 progress 回滚均重新检查。
- 同一物理文件只替换/重绑定一次，未选 strict 字节逐字节保留，无正文备份；完整预算超限不产生部分执行。
- 使用既有真实 Journal/Store2/native emitter/handler 和合成内容，执行相关回归、完整 Hub、工程门禁、独立审查及同镜像本地/NAS 安装验证。

## 实施分工与仍需接通的产品路径

Store2/manifest 与其测试一条实现线；纯恢复图与其测试一条线；文件批处理及安装探针由协调者集成；另行独立审查并发与失败路径。同文件仅由对应负责人修改，共同接口先固定。

本轮不提前移除普通服务的 schema2 runtime_unsupported。正式启用必须把 store 选择、抑制读取、cleanup.pending 判定、父业务/repair 分流、Core/Docker 输出域和停服审阅/确认/续做入口一起接通。跨项目混合旧记录的整条删除决定仍待用户明确；重导入拒绝整事件不是此项授权。这些是原产品目标的剩余工作，不因两域联合重放成功而移出范围。

现有停服只读预览同时修正旧格式兼容：实际已签父记录中没有 cmhe 原文时，selectors 可以合法为空，不要求后来才引入的在途证书；仍验证主链、原记录及全部实际日志。含 cmhe 的旧操作缺原证书继续拒绝，不能补签。repair 本身不能作为新的 repair 父，即使其 action 为 purge、records 为空；预览仍不签确认、不执行物理删除。
