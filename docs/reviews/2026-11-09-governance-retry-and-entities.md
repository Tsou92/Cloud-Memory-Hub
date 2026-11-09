# 治理重试身份与恢复边界

日期：2026-11-09。关联 FR-10、FR-11、ENG-01；按 [roadmap](../roadmap.md) 继续 G1。设计见 [ADR-0036](../adr/0036-governance-request-identity.md) 和 [ADR-0031](../adr/0031-derived-writer-coverage.md)。

## 请求重试

原实现会把同 operation_id 的不同 action 或选择集合回答成已完成；计划写入后、日志追加前中断，重试丢弃计划也可能返回 completed 而未改变目标。新实现复用签名 Intent 的 HMAC 目标，绑定 actor、scope、action、排序后的选择及确认视图，不新增明文请求或数据库。

execute 与 resume 共用治理串行锁；内部恢复不重复取锁。恢复后重读持久状态：已提交操作返回真实完成结果，孤立计划按原确认视图重新执行，失败或仍 planned 不报完成。旧操作缺少请求指纹时明确拒绝同 UUID 的一致性推断，原自动恢复能力保留。

19 项新增请求身份测试通过，包括各参数冲突、规范排序、故障位置、旧操作兼容、并发与重复取消、总目标预算及身份域隔离。独立审查未发现新增问题。

## 未纳入范围的实体副本

真实 SDK/Chroma 合成实验确认：开启实体索引会保存独立副本，原预览遗漏该副本；关掉开关不清除既有数据。新检查复用上游集合命名、线程池和 Chroma 原生读取，只查目标项目最多一个 ID，不读正文或创建集合。

独立审查又复现旧 planned 操作可绕过新预览检查。修复将同一有界检查用于执行收尾和启动恢复：首次恢复先检查所有记录项目，包括 completed forget，再判断 purge 目标是否重现。只恢复实体集合也被阻止；检查失败保留计划和数据，不输出新的成功状态。该检查不重建可能已被部分删除的来源图。

新增 31 项实体测试。恢复部分先记录 10 失败、4 正常对照通过，修复后与治理执行、缓存恢复、worker 协调及请求身份联合回归 94 通过、6 个既有预期失败。独立复审再次运行真实 HTTP/SDK 复现：forget/purge 的同 UUID 重试和直接 resume 均拒绝未建模实体，计划保持 planned，实体保留，治理锁、Drain 锁和 SDK 独占均释放。

## 验证边界

`make check-runtime` 全量 793 通过、5 项可选后端跳过、6 项在途缺口预期失败、269 条上游弃用警告；`make check` 通过。扩展后的 `make probe-governance-execution` 通过：跨进程重启、缓存单独恢复与完整业务数据恢复后，相同确认请求返回真实结果、冲突请求被拒绝，来源修订仍包含在范围内且忘记后原文可主动查阅，混合消息命中长句时整事件拒绝。

新增 `make probe-entity-inventory` 的本地源码执行通过，六场景、12 个独立子进程覆盖真实 SDK 实体写入及 ID 关联、关闭开关与重启后拒绝预览、旧 planned forget/purge 保护、completed purge 仅恢复实体时拒绝启动，以及无实体/其他项目实体的正常恢复对照。脚本复用原文归档探针，不导入测试模块；安装验证需同时提供其 `probe_original_archive.py` 依赖。

以上使用真实 Hub、HTTP、SDK 和持久存储，提炼、实体识别及 embedding 为合成替身，没有真实历史或外部模型调用。实体治理本身尚未实现，此次完成的是拒绝遗漏范围的检查；不将其他项目的正常实体误判为本项目副本，也不宣称检查了所有无归属损坏行。

本轮后续已与来源修订及日志修复一起进入 cde427c，通过本地/NAS 五项安装探针和完整容器检查；常驻空实例停服备份后升级并重启健康，见[安装记录](2026-11-09-chroma-purge-sequence.md)。6 个已接纳任务及旧队列恢复缺口继续保留，不能将本轮修复当作存量队列清扫完成；客户端闭环、离线传播及完整备份恢复仍按 roadmap 推进。

## 安装前发现的已完成 forget 恢复缺口

独立实验在保留治理目录的条件下，恢复 forget 前的 data/config。重启后操作仍 completed、服务 ready，Hub 对旧记忆返回 404，同内容换 ID 重导入返回 409；但旧 fact/raw 在 SDK 中恢复为 active/shadow，真实 Core Drain → SDK add 的新事件提炼 history_context 再次含已忘记原文。普通重启而不恢复业务数据的对照不出现该原文。

这与原 6 项队列预期失败不同；根因是 completed purge 会重放而 completed forget 不会。已按 [ADR-0032 恢复补充](../adr/0032-governance-execution.md) 修复：启动重新应用签名范围的原生节点抑制，核对节点归属，不删除主动可查的原文，失败不就绪。SDK get_by_id 会将读取/解码错误吞为 None，因此对 None 额外点查原生 Chroma ID；存在但无法解码时不能当作已缺失。

新增 9 项测试已红转绿，覆盖真实新事件提炼输入、普通重启对照、重复启动、无关 raw 和原文保留、更新返回 False/异常、错误 owner/id、损坏节点，以及真实缺失和 purger=None。独立复跑原失败探针（含 purger=None）确认恢复后 raw/fact 均 archived，下一轮 history_context 不含已忘记原文、仍包含无关历史，原有 404/409 行为保持。本切片全量 822 通过；后续 cde427c 的本地/NAS 组合安装探针已覆盖该恢复路径，见上述安装记录。

## 生产力忘记的明确能力缺口

独立实验还在 working、coding 两变体复现：forget 返回 completed，Hub fact 404，但独立生产力库中的 fact 仍 active；真实 reconciler 候选和生成的模型请求包含该 fact 的 ID 与正文。这个缺口不依赖恢复备份，也不是本轮新增的普通节点恢复回归。

按 ADR-0032 增加提前拒绝保护：检查完整关联闭包中的 `cmhp/cmht/cmhc/cmhv`，不是只看请求选择。新操作在计划和意图前拒绝，已提交旧计划、已完成恢复及同 ID 重试均不得绕过；未提交孤立计划仍按 ADR-0036 丢弃重验。拒绝合同与完整生产力忘记能力分开验收，后者仍未完成，不能凭 Hub 404 或保护性拒绝宣称通过。

新增 20 项专项测试红转绿，覆盖 working/coding 的真实 HTTP 闭包、签名状态及原生数据不变、真实 reconciler 候选/提示仍属未完成能力、旧操作重试及重启保护、孤立计划恢复和前缀分类。另有 `make probe-productivity-forget-guard`：4 个独立进程通过，复用真实 SDK 存储、受控 writer 产出和合成 embedding；不将该替身称为完整原生提炼验证。探针依赖 `probe_original_archive.py`、`probe_sdk_reconciliation.py`；后续已在 cde427c 的本地与 NAS 镜像内执行通过。

本轮源码切片的最终 `make check-runtime` 为 822 通过、5 跳过、6 个既有队列预期失败、289 条上游弃用警告；专项组合 126 通过、6 预期失败，独立审查与原始恢复复现通过。工程门禁通过。

组合治理安装探针新增“恢复后继续真实 SDK 写入”断言时，发现 purge 后新写入 ID 可见性异常；未放宽断言，也没有用 forget-only 通过替代组合场景。独立取证确认：seed 的 queue max/watermark 为 6/6，govern 后为 9/11，后续新 raw/fact 使用 seq 10/11 而部分未进入可读存储。Chroma 的 seq_id 不是 AUTOINCREMENT，直接删掉队列最高行会使后续序号回退。当时暂停安装；后续按 [ADR-0037](../adr/0037-chroma-purge-sequence.md) 修复，组合探针、本地/NAS 制品验证均通过，并随 cde427c 更新常驻实验实例。
