# ADR-0043：诊断物理记录的执行、停服维护与恢复

日期：2026-11-10。状态：独立执行组件与停服只读预览已实现，产品执行入口尚未启用。关联 FR-10、FR-11、ENG-01；复用 [ADR-0042](0042-diagnostic-file-governance.md) 已验证的只读请求、记录与文件清单。

## 目标、复用与尚待明确的范围

将精确物理记录检查接到可实际执行、失败可续做的清理路径。继续使用固定上游的日志后端与 handlers、Hub 原治理主日志、签名请求证书、存储租约和排空屏障；不新建日志服务，不通过全目录删除替代范围证明。

旧日志 request 只提供候选关联。新的 RecordGrant 授权的是具体记录及逐字节相同副本，不证明项目来源。无法可靠分离的跨项目混合内容，是否可经单独逐条审阅后删除整行，已向用户询问；答复前不启用这种扩大范围的执行。只读开库、持久状态和文件替换机制可先用明确的合成选择验证，不据此执行真实内容删除。

SDK 两域的执行成功不等于整个 purge 的全部持久域已闭合。Core 旧多行、Docker 旧 stdout、其他未知写者及不受控日志仍是独立覆盖前提；不得以 SDK grants 为空、看不到目标字串或未声明其他域来制造完整删除结论。

## 停服维护必须可达

正常 create_hub/启动恢复/HTTP preview 可能因旧 completed 操作恢复出未授权正文而拒绝。新增维护入口必须在 create_hub、Uvicorn、Core lifespan、SDK client 和自动 resume 之前分流，并持有现有 data/config/governance StorageLease。不能要求先启动被阻塞的服务才能重新确认。

只读维护复用原校验算法，显式 `read_only=True` 打开现存 Access、Journal、Authority、Admissions。连接使用 SQLite mode=ro 与 query_only，不初始化目录或缺库；不使用 immutable=1 忽略已提交 WAL。只读指业务与治理行不变，不承诺 SQLite 的必要 sidecar 或现有租约锁文件零 I/O。

Authority 仍验证完整链、binding 和已有 floor；实际有 journal scope 却缺少 floor 时拒绝，合法落后返回最新 head 而不补写。Admissions 仍检查完整准入链和 witness，合法落后返回最新 proof，不能返回旧 enrolled；缺失、超前、分叉或签名异常继续拒绝。只读对象的 append/enroll/permit 等写入口在事务和外部回调之前拒绝。原默认模式兼容保留，共用 `_refresh` / `_witness`，不复制第二套签名算法。

`prepare_cleanup_selectors` 对只读 access 使用读事务；普通 prepare/apply 的证明与取消仍共用原写锁。停服调用由上层真实租约保证合作写者已停止，不能把 read_only 标志当作并发互斥证据。

已提供 `diagnostic-preview --writers-stopped --operation-id UUID`。它只读取实际已提交 purge 的原请求证明，返回有界的精确记录授权候选与摘要；不输出正文、不迁移、不签确认、不清理。`origin_proven`、`body_reviewed` 和 `execution_available` 均为 false。正常服务受旧恢复状态阻塞时此入口仍可运行，但调用者必须先停止合作写者。

## 持久格式与旧版本保护

独立 ExecutionStore 组件提供 execution.sqlite3 的显式 user_version=2 迁移，保留 operations/suppressed 原字段与 v1 签名，增加签名 binding 和每个 purge 的唯一 cleanup 行。binding 包含 instance、主 ledger、store UUID、schema 与迁移基线；cleanup 分开保存不可变 policy/selectors/grants/父操作关联，以及可变 pass、revision、manifest、pending/verified。verified 还保存封闭的文件摘要、字节数及 artifact 缺席观察，不保存正文。不同 MAC 域都绑定原 intent；所有读写先在同一快照验证完整关系。可变进度不能增加 grant。

主 journal 沿用 v2，在原闭集 capabilities 中显式增加 `execution_store_v2`，其值为 execution store UUID，使用现有 capability MAC 绑定。旧程序只接受单一 admissions capability，因此会明确拒绝新增能力；这需要实际旧制品验证，不凭源码推断验收。新服务要求 capability 与 execution binding 完全匹配，不在启动时自动补齐。

当前普通服务仍使用原 schema1 执行器；遇新 capability 明确返回 runtime_unsupported，空主日志也不能跳过已有 schema2 库检查。独立组件和只读维护支持已验证的迁移提交间隙；在产品恢复协调接线完成前，不对常驻实例迁移或开放新执行路由。

迁移持停服租约，先只读验证所有旧 operation/suppression、真实意图、外部 floor 和可恢复选择，再在 execution 单事务内建表、回填、升版本；最后显式提交主 capability。两库不是一个原子事务。中断中间态只允许经完整复核后重试迁移，不放行业务服务。旧 intent、原目标及 checkpoint 不重签；旧意图没有物理记录授权，回填 grants 必须为空。

未提交 planned orphan 可以保留供原恢复路径丢弃，但不得拥有任何文件 artifact；删除 orphan 前再次验证没有实际 journal intent。迁移为未提交 purge 单独保存签名 `orphan_only` 标志，固定 legacy 模式、空 selectors/grants；此标志不能由新 plan 生成，不能开启 pass 或完成清理，后来主链若出现该 intent 则拒绝而不提升权限。它只允许在主链仍无意图且真实 artifact 缺席观察成立时丢弃，避免为恢复本来无删除权的孤立计划而补造来源证书。所有对 cleanup 的更新使用带 revision 的严格 CAS，主 completed 不可遮蔽 cleanup pending。MAC/pass 是重试缓存，不是检测全部合法旧快照一起回滚的独立 witness；每次启动仍扫描实际文件与登记 artifact。

## 新确认与原授权的关系

物理确认摘要绑定 scope、精确 selectors 与现有证书、去重排序 grants、解析和覆盖策略。inode、路径、mtime、strict 尾行数量、相同记录的出现次数仅用于 I/O 检查，不进入长期记录授权。新增不同旧记录或改变任一字节需新确认；原样恢复、轮转改名与重复相同记录不扩大已确认集合。

普通业务预览与物理记录选择必须区分，完整清单可分页且预算有界；不能用一小段样本声称已展示全部影响。执行在既有治理锁、worker/SDK/诊断排空边界内重算，在 plan/append/取消之前核对业务选择、精确确认和所有覆盖前提。只签确认而未完成真实清理不得返回 completed。

旧 completed 意图的新增确认使用独立 repair intent，承诺父操作 ID/摘要/ledger、原作用域、selectors、精确 grants 与请求身份。原 intent 和空 grants 保持不变。专用分支不走普通业务 `_apply_inflight` 或重复删除业务对象；后来恢复同字节副本时重新验证 OLD→NEW 关系并使用 NEW 的精确授权，不能只维护一个可改的 resolved_by 指针。不同新记录仍走同一可达维护入口，不能限制只修复一次。具体公开接口在完整接线前补充，未完成时不启用会永久阻塞旧实例的 startup gate。

## 物理替换与日志资源

目录范围、原生语法、预算与 NOFOLLOW 继续复用已有 inventory。每个操作先预检全部文件，再持久登记每个 staging 名称及预期保留摘要，之后才能创建文件。staging 只含保留的原始字节，权限 0600、同文件系统、fsync 后原子替换并同步目录；不生成含目标正文的备份。多文件替换不是全局原子事务，任何失败保留 pending。

登记的临时文件必须按摘要或可证明的保留前缀复核；未知 artifact 保留现场。不能仅凭名字删除临时文件，也不能让旧 staging 覆盖新增 strict 尾部。完成前重新扫描正式文件，验证目标 grants 无匹配、保留字节未丢失、登记 artifacts 已处理。只恢复旧日志或旧 verified 进度也必须重新检查。

只有实际尚存的 staging 才要求对应正式文件保留旧完整保留前缀；已完成且无 staging 时重新扫描当前文件，因此正常轮转不应被旧路径限制。stage 写入并同步后，必须重新打开核对真实字节与身份，不能只相信写入前摘要。文件组件返回两域 verified receipt，不调用主操作 complete，也不声明其他副作用域已经闭合。

在线替换另需真实 handler gate。复用 native diagnostic policy 锁约束受控 SDK factory 的构造/attach，配合 Hub 路由锁和所有命中域的 handler 锁；在同一同步线程完成 flush、重新观察、关闭旧 stream、replace、目录 fsync 和按原配置重开。保留 handler 对象、formatter、filters、level、模式和轮转状态；外层取消使用 joined thread，不能提前释放排空边界。

不能只锁 logging registry：任意 FileHandler 构造会在登记前先打开文件。受控 native factory 必须纳入 gate；命中域的未知或保留的 caller FileHandler 拒绝。任意第三方后来绕过已声明专用进程合同新建输出不由此 gate 保证，不能宣称控制了任意 Python/C 文件描述符。锁获取必须可拒绝/重试，不在持 policy/registry 锁时阻塞等待任意 emit 回调，不跨 await 持线程锁。

补丁 0024 复用原 handler 类型与轮转，提供同步维护租约及原流配置的重绑定。关闭/接管失败后 handler 保持封闭，不能被下一次 emit 自动重开旧路径；重试仍使用原对象。该合同不审计任意第三方 formatter/filter 的私有 I/O，依赖 Hub 专用进程与严格策略；Core 独立日志不属于本次 SDK 两域物理执行范围。

## 验证要求

1. 只读 authority/admissions 的全部行与 heads 不变，缺库不创建，WAL 最新提交可见，缺失/落后/超前/分叉锚点及写入口提前拒绝。
2. schema2 的完整关系、MAC、CAS、丢行/替换绑定/只恢复旧进度、orphan/已提交辨别，以及显式迁移中断重试；实际旧程序拒绝新能力。
3. 原生共根的三格式正文与 strict 元数据：合法精确选择真正移除目标，保留行逐字节一致；缺 grant、未知记录或范围变化在任何破坏前拒绝。
4. handler 构造/emit/轮转竞争、坏 stream、同线程重入、关闭/重开失败、取消等待：不遗留旧 unlinked inode，后续日志仍进入当前文件。
5. stage 创建、部分写、fsync、逐文件 replace、重开、完成标记各处中断；原样文件恢复和新增安全尾部重试；未知 artifact 不删除，不用旧 temp 回滚新内容。
6. 服务启动被恢复阻塞时，真实停服 CLI 仍可做只读维护预览；新确认、失败续做、再次恢复都沿真实签名关系工作，普通业务写入保持关闭。
7. 产品代码和迁移完整接线后执行相关全量、安装、旧新制品及 NAS 合成验证。只读前置、解析器或本设计不能替代这些执行证据。
