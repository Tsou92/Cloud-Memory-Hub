# ADR-0040：维护期间排空真实诊断写入

日期：2026-11-10。状态：本地、安装及 NAS 隔离验收完成，见[验证记录](../reviews/2026-11-10-diagnostic-quiescence.md)。关联 FR-10、FR-11、ENG-01；承接 [ADR-0039](0039-sdk-write-effect-observation.md)。本项是日志治理的写入协调，不代替已有文件清理或完整副作用证书。

## 已复现问题与复用

隔离合成 Hub 实验中，原生 JSONL 和 SQLite trace 的最终 I/O 被暂时阻塞；HTTP purge 已返回 completed，释放后台任务后，目标正文才出现在文件或重新出现在已清理的诊断表。操作签名及日志头未变化。另有独立证据证明已经落盘的 JSONL/text 正文未被现有 Purger 清除，这一缺口也必须继续处理。

固定上游已有 wrap_store_pipeline_log、跨 loop 派发、executor guarded await、runtime/cache 生命周期和 drain_pipeline_log_tasks。现有 drain 只扫描当前 loop，超时只 warning；跨 loop trace 的代理 future 不在同一集合。外层 task 取消也不代表 executor 的原生 future 已结束。因此不能直接把该函数当作治理屏障。

继续复用原缓存、PipelineLogWriter、线程池和 SDK 共享准入，不重建日志服务或业务队列。两个原生搜索入口未参加共享准入，必须补入，保留搜索并发而不增加每桥串行锁。

## 合同与锁顺序

每个安装了 pipeline wrapper 的 cache 拥有独立诊断工作组。入队前先登记票据，任何正常/失败路径都在**实际任务及 I/O 结束之后**归还；同 loop、跨 loop、线程池分别保留真实完成依据。原生 concurrent future 完成前，取消其外层 asyncio task 不能使工作组变空。普通没有诊断上下文的 executor 调用保持原行为。

```text
完整 worker quiesce
  -> SDK shared admission 的 exclusive（等待已接纳读写）
  -> 复用既有 Chroma 磁盘序列只读预检，拒绝损坏的旧库
  -> 初始化治理桥需要的 client，再快照关联 runtime/cache，释放注册表锁
  -> 同时暂停关联工作组的新入队
  -> 等待实际任务、跨 loop 派发及 executor I/O 全部结束
  -> 原治理观察/修改/清理
  -> 确认工作组仍排空
  -> 恢复日志入队 -> 释放 SDK 准入 -> 恢复 worker
```

暂停期间 pipeline wrapper 返回明确的未接纳结果，不缓存正文、不等待维护自身释放锁；只保存不含正文的计数。治理自身的权威审计仍写独立签名日志，不能借此关闭审计。默认日常日志调用保留异步返回，不为了清理而把全部普通请求改成同步日志 I/O。

工作组暂停、票据计数和异步派发存在/完成必须受线程安全状态保护；不得把 run_coroutine_threadsafe 的代理 future 取消当作 owner loop 的实际 task 已退出。完成回调中的异常消费及可能的日志动作先结束，最后才归还票据。

冷启动不得以空 runtime 快照通过屏障：治理桥在持有 SDK 独占准入后先完成所需 client 的初始化，再快照其 cache 与同一数据目录已注册的 runtime。独占期间其他前台操作不能新增 runtime。每次暂停拥有独立租约，多组暂停部分失败也须释放已取得的租约；嵌套退出只恢复自身持有部分，不提前放开外层暂停。

初始化前仍须复用既有磁盘序列只读预检；SDK 打开 Chroma 本身可能写 acquire_write，不能先初始化再拒绝损坏的旧队列。初始化后的实际配置路径、派生范围和序列复查继续保留。旧库损坏场景必须继续保持数据库逐行不变，不放宽原验收断言。

没有执行记录的空恢复不进入 SDK 维护：在原 `_resume_lock` 保护下读取签名执行记录为空即可结束，不提前初始化原本按需打开的原生存储。存在记录时仍在完整屏障内重读并执行。连续 12 个空实例的隔离实验中，过早初始化让已关闭实例的原生 Chroma 进程缓存占用从 16 增至 82 个文件句柄；旧惰性路径始终为 7。保留惰性空启动，不通过放宽进程锁测试来隐藏新增资源占用。

不得持有 runtime registry、SQLite 或 logging handler 锁跨 await 等待。严格排空超时必须抛出明确错误，不取消仍可能写入的任务、不返回成功；治理不得开始破坏性操作。失败/取消退出仍恢复暂停状态并保留未完成工作的真实计数，重试可继续等待，不重新发送业务写入。

原生搜索增加仅包裹 operation_guard 的共享入口；exclusive_access 继续用于需要每桥互斥的现有路径。外层已接纳操作和治理所有者可重入，不能制造共享→独占升级死锁。维护等待必须同时覆盖读路径产生的日志，不能只观察生成请求。

## 生命周期与兼容

新增原生内部 API 提供关联 cache 的 async quiesce context。Hub 在既有 GovernanceExecutor._exclusive 内调用，不修改治理意图、确认范围、数据库版本或一次性许可。未知旧任务仍保持未知；`write_effects` 仍为 observation_only。

关闭最后一个共享 runtime 或拥有全部资源的 solo client 时，在 cache 和两个 loop 关闭前严格等待日志完成。共享 runtime 尚有其他 client 时，单个 client 关闭不能暂停其余使用者。排空失败时保留可重试资源，不提前标记 closed、吞错后继续停线程。不得通过关闭并重建全部 SDK 客户端来实现维护排空。

可重试性覆盖完整所有权链：bridge 的 client 引用、leased client 的 released 标志、共享 runtime 引用计数/注册表条目和 native closed 状态均在对应关闭成功后提交。关闭中的 runtime 保留注册表位置，不能被另一次借用替换或重复释放；失败后仍能定位原对象重试。批量关闭不能先清空注册表再吞掉失败。不得持有注册表锁等待关闭，已经成功释放的部分不得在重试时重复扣除引用。

借用保护指 Hub 实际使用的 Core runtime 注册表链。原生 SDK 没有新增 runtime 租约 API；第三方绕过该注册表、直接把已经关闭或关闭失败的 runtime 对象传给另一客户端构造器，不由本轮保证有效。这不影响 Hub 的创建与重试路径。

成功关闭后工作组永久封闭，保留的 cache wrapper 不得再次排入 JSONL 或 trace；维护暂停退出则可以恢复正常入队。资源尚未关闭时的排空失败可以释放暂停租约，资源部分关闭后保持 closing 并拒绝新借用。每桥关闭需等待同桥已经接纳的搜索，普通搜索之间仍可并发；不能把只供现有独占方法使用的每桥锁误当成共享搜索的生命周期屏障。

老 SDK 未启用新接口时保持原默认行为；Hub 使用固定补丁组合并验证接口存在。旧排空 API 的测试/优雅关闭兼容保留，但只有新的严格合同可以作为治理屏障。实际 shutdown 行为与队列准入回归分开验证。

## 测试、性能与失败行为

| 场景 | 必须观察到的结果 |
| --- | --- |
| JSONL/SQLite 在最终 I/O 前暂停 | 治理先等待；释放后再执行删除，之后不会新增目标缓存行 |
| 跨 search/owner loop | 两 loop 的实际任务和底层 I/O 都被等待，不以当前 loop 空集合通过 |
| 外层取消、重复取消、线程仍运行 | 票据仍存在；维护不提前进入；实际 I/O 结束后可重试 |
| 入队与暂停竞争、嵌套维护 | 无漏登记、无负计数、无静默旁路或死锁 |
| 维护中日志调用 | 明确未接纳、无正文缓冲，普通治理签名审计不受影响 |
| timeout/loop 关闭/派发失败 | 固定错误且不开始破坏性修改；真实仍运行工作不被遗忘 |
| 搜索并发 | 普通搜索可以并发；维护期间新搜索被准入阻止，已接纳搜索等待完成 |
| runtime/solo 关闭 | 日志真正完成后才关闭资源；失败可重试，不关闭其他 client 共享资源 |
| 真实 HTTP 治理 | 原生延迟 trace 的缺陷复现转为等待后成功；无关请求保留，签名操作不新增 |

工作组登记/释放应为有界常数操作；排空随正在执行的诊断工作量变化，不扫描历史正文。普通路径不增加模型调用或持久数据库。对模块、异常、并发和真实 HTTP/SDK 路径分别验收；运行 make check、对应上游回归及 Hub 全量，之后安装/NAS 验证不与合成模型质量混称。

## 后续必须完成

已经落盘的 pipeline JSONL、SDK text/JSON、恢复的旧 Core 日志及 Docker 旧 stdout 副本仍需分别处理。按写任务 request_id 清理也不能覆盖独立读取 request_id 的内容副本。应补充未来输出策略、来源/节点关联、预览范围、白名单路径/预算、原子重写与仅日志恢复重放；未知归属不能靠全文 grep 或目录位置授予删除权，也不能暗中清空整日志目录。

本项不将上述未实现部分标记完成，不减少长期原文保留、诊断可用性或完整删除的既有需求。

屏障范围仅为 pipeline wrapper 产生的诊断任务和它们的 executor I/O，不声明全部 cache I/O 均已停。原生 memory-operation 日志、metrics 后台刷新以及绕过实例 wrapper 的直接类方法调用不属于本组；Hub 当前关闭 memory-operation 日志。非诊断后台任务沿现有关闭流程处理，其完整线程生命周期还须另行验证，不能从本轮 pipeline 关闭测试推导通过。

依据：[Python concurrent futures](https://docs.python.org/3.12/library/concurrent.futures.html) 与 [asyncio shield](https://docs.python.org/3.12/library/asyncio-task.html#asyncio.shield)。外层取消、代理 future 状态和实际后台 I/O 完成是不同事实，必须分别跟踪。
