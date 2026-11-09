# ADR-0037：删除清理后保持 Chroma 日志序号单调

日期：2026-11-09。状态：已实现，定向/完整回归、独立审查、本地/NAS 安装与空实例备份升级通过，制品 cde427c。关联 FR-10、FR-11、ENG-01；补充 [ADR-0032](0032-governance-execution.md)，不改变已接受的 HNSW 预分配空间限制。

## 问题与实测

扩展治理安装探针，在 forget/purge、重启与恢复之后通过真实 Core Drain/SDK 继续写入，发现 SDK 返回成功的部分新 ID 不可读，产出观察因此保持 unknown。原有删除/恢复测试没有覆盖这一继续写入路径。

独立实验核对 Chroma 1.5.9 的实际 RustBindingsAPI 与 SQLite：seed 时 embeddings_queue 最大序号和元数据段水位均为 6；purge 后队列最大只剩 9，但段水位为 11；新 raw/fact 复用 10/11，部分记录未进入可读 embeddings。该表 `seq_id INTEGER PRIMARY KEY` 没有 AUTOINCREMENT，现有按目标 ID 删除全部队列行会删掉最高序号。

已安装 Chroma Python `SqlEmbeddingsQueue.purge_log` 仅删除小于段最低水位的日志，可作为保守清理策略的源码参考；它不是本次 Rust runtime 已调用该函数的证据。不得通过添加 AUTOINCREMENT、重写上游表或改迁移校验值掩盖兼容性问题。

## 修复方向

1. 仍沿用现有治理锁、Drain 等待、SDK 独占与进程存储租约。首先检查当前队列最大序号不低于已保存段水位。对旧版本已经造成的倒退，明确拒绝并保留现场；不能靠追加一次日志假定修复了所有已丢失写入。
2. 普通 SDK 目标删除后、清理目标队列副本前，对全部已确认 native IDs 使用原生 collection.delete 再提交真实目标的无正文 DELETE。沿用上游 `_node_id_to_point_id`，不能假定原生 ID 总是 UUID；分批调用，不创建 MemoryNode、不调用模型或改无关用户数据。SDK delete 在目标已缺失时可能不追加日志，因此仅沿用该外层调用不足以覆盖恢复路径。
3. 精确清理目标日志时保留严格无正文的 DELETE：operation=3、vector/encoding=NULL、metadata=NULL 或 '{}'; 其余目标行仍清理。空 JSON 对象不是用户正文。该墓碑同时保留最高序号和向量段尚未消费的真实删除依据，最终由 Chroma 原生 GC 按消费边界回收。`_side_present` 只豁免这种严格墓碑；含正文、向量或其他元数据的 DELETE 仍属于残留。history/cache 继续使用原生记录/请求身份，Chroma 队列使用映射后的 point ID。
4. 旧 completed purge 可能因目标已不存在而跳过 purger.run，因此序号完整性检查也在首次治理恢复的副本前置检查中执行，并早于 completed forget 的状态写入。复用固定上游的 namespace 与目录构造，对已有 Chroma 文件在 SDK 初始化前先只读检查；初始化后再按实际 SDK 配置检查。首次空库仍允许初始化。该检查只证明当前日志/已保存水位未倒退，不证明混搭备份整体一致或未知任务没有历史副作用。
5. 清理前后核对序号不变量，继续验证目标正文清除、无关节点保留、幂等中断恢复、后续真实写入和独立进程重开。允许保留无正文删除墓碑，不宣称所有目标 ID 都从底层删除日志消失；它是防止复活的删除状态，不是被保留的正文。不得删除未消费的真实 DELETE，也不能只凭序号增大宣布完整安全。

## 复用与验收

复用 Chroma 原生 collection.delete、ID 映射、既有线程池、SDK 节点删除、目标队列清理与现有逐字节核验；只增加单调性保护和无正文日志保留。不重写向量索引、记忆算法或任务队列。

随机不存在 ID 锚点曾让小规模组合通过，但它仍会删除尚未被向量段持久化的真实目标 DELETE：1000 节点实验观察到 vector 水位 1000、metadata 水位 1002、目标 DELETE 1001。保留真实删除日志更符合原生消费边界，不应仅用随机锚点替代目标删除依据。

早期高阈值实验使用大量相同向量，控制组 seed 查询本身会退化，且原诊断脚本没有 assert；该轮输出已撤回，不计通过证据。正式回归保持真实 1000 节点/水位，改用不重复向量并对 seed 查询加严格断言。独立审查使用完全相同的新 fixture：加载 9f9c851 的旧清理函数后，get 虽返回新正文，query 却给出旧向量（新向量距离 2、旧向量距离 0）；新实现多次以及独立复跑均为新向量距离 0、旧向量距离 2。新的红绿因果证据取代早期不稳定输出，不以脚本退出码代替结果断言。

最终私有原型“原生目标 delete + 仅清含内容/非 DELETE 目标行 + present 豁免严格墓碑”通过 seed→govern→read→read，队列/metadata 水位依次为 13、16、19，真实 SDK 产出观察完成；目标旧正文/向量日志清除，高阈值待消费 DELETE 仍保留。旧 queue max=9/watermark=11 状态只追加一次原生日志得到 10/11，证实不能自动修复，需前置拒绝。

正式实现的混合安装源码探针也由独立审查完整跑过：forget/purge、重启、仅缓存恢复、完整业务恢复以及每次恢复后的新 SDK 提炼均通过。`test_chroma_purge_sequence.py` 与 `test_chroma_delete_replay.py` 共 4 项真实存储回归通过。旧损坏启动测试允许构造 Hub 时按已有签名日志推进治理 floor，但核验原文和其余配置不变；随后启动阶段完整库快照不变、无 forget 状态更新、不开放 ready。

检查预算：先验证 embeddings_queue 的 INTEGER PRIMARY KEY 确为 rowid 别名，再分别以 MIN/MAX 端点查询读取序号；不对全部日志做 SUM 扫描。max_seq_id 最多读取 10,001 行，超过 10,000 段明确拒绝。真实 SQLite 的 1 行/100,000 行合成空 DELETE 对照分别为 475/465 条 VM 步骤，EXPLAIN 均使用 SEARCH；这是本机定向证据，不是 NAS 大规模性能验收。完整回归 838 通过、5 跳过、6 个原有在途缺口预期失败；安装结果见[验证记录](../reviews/2026-11-09-chroma-purge-sequence.md)。
