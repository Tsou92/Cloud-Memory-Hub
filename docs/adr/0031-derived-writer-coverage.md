# ADR-0031：派生写入路径的覆盖边界

日期：2026-11-07，2026-11-09 补充。状态：原六项开关防护及实体副本拒绝检查已实现，恢复专项与本地/NAS 安装验证通过，制品 cde427c。关联 FR-09、FR-10、FR-11、ENG-01。

## 问题与实测

ADR-0030 之后，原计划补齐"会话摘要的输入快照与生产来源"。动手前先核对固定上游 b7d9f0e 中所有生成派生记录的路径，以及 Hub 运行时实际启用了哪些。在真实 Hub 初始化的 SDK 上读取生效配置，结果为：mode=pro、未开图存储、episode_summary/session_episode/series_episode/topic_summary/entity_store 均关闭，仅 dynamic_episode 开启。

| 上游派生写入 | 触发条件 | Hub 中是否可达 |
| --- | --- | --- |
| 滚动会话摘要 summarize_closed_episode（L3） | episode_summary_enabled | 否，桥接固定为关闭 |
| 会话／动态／系列 episode 胶囊（L3） | 写入请求带 finalize_session，或调用 finalize_session | 否，无任何 Hub／Core 调用方；dynamic_episode 只影响读取侧 |
| 主题摘要（L3） | 图存储且 topic_summary_enabled | 否，两者都关闭 |
| 意图（L7） | llm.enable_intention | 否，桥接固定为关闭；实测抽取结果含 intentions 时不落库 |
| System2 演化（L6）与 cross-domain sweeper | ultra 模式下 evolve() | 否，非 ultra；Core 的 graph/digest 路由在 SDK 权威下返回 409 |

因此滚动会话摘要在 Hub 中不会产生任何记录，ADR-0030 的收尾修复仍然有效：一旦启用，生命周期正确，但其来源尚未建模。为一条不可达的路径建输入快照属于推测性工作，本轮不做。

## 决策

范围观察在打开 SDK 后先核对上述开关；任一未建模的派生写入可能产生记录时，返回 503 governance_derivation_unmodeled，不给出可能遗漏派生内容的治理范围。不修改上游配置或桥接，也不改变写入行为。日后启用其中任何一项，必须先补齐对应的输入快照、生产来源和范围接线，再解除该检查。

同轮实测管理员 PUT /api/v2/memories/{id}（纠正）：上游原地重嵌入并清除来源标记，治理预览仍按存储的原文指针把纠正后的记录纳入原文范围，没有遗漏或越界。纠正的版本历史与"用户纠正优先"语义属于 FR-09，与删除执行一并处理，本 ADR 不宣称已完成。

## 2026-11-09 补充：实体副本仍有未覆盖路径

新隔离探针证明，原先“实测默认关闭”不能替代运行时防护：设置 `MEMORY_ENTITY_STORE_ENABLED=1` 后，真实 Hub SDK、MemoryWriter 与 Chroma 会保存独立实体副本；提炼、实体识别和 embedding 使用合成替身，无外部模型或真实历史。现有 require_modeled_derivations 仍通过，包含真实 lineage 的范围观察只返回原文、raw 和 L2，漏掉实体副本。将开关设回 False 后，既存副本仍保留且继续被漏掉。

因此在实体来源与物理删除尚未建模前，必须同时检查当前写入开关和已存在的实体存储：开关开启直接拒绝；关闭时仍应通过上游存储接口检查受影响项目是否有残留实体集合。能证明没有该项目副本时才继续；无法证明或读取失败不返回不完整范围。不得靠关闭开关掩盖旧备份带回的实体副本，不自动清除未确认的内容。

检查沿用现有库存以存储 user_id 为项目归属的合同：复用上游实体集合命名函数与 Chroma 原生 get_collection，再按目标 user_id 查询最多一个 ID，不读取正文或向量。不存在的集合不创建；只把原生 NotFoundError 当作不存在。目标项目命中、返回形状错误或其他读取异常均拒绝，正常其他项目不因此受阻。上游 list_entities 会创建集合且丢弃归属字段，因此不用于这项只读检查。该检查不宣称审计了整个库的归属元数据完整性，也不证明无归属损坏行或任意混搭备份安全；这些边界与其他按 user_id 隔离的 SDK 库存一致，负向产出证明必须另行设计。

最小防护已经实现；本补充不将实体治理能力标为已实现，也不改变整体 G1 退出条件。

独立审查进一步复现：升级前已提交的 planned forget 可绕过新预览检查，在 resume 中变为 completed，而实体副本仍存在。因此同一只读“配置+目标实体 ID”前置检查必须用于执行收尾和启动恢复；已完成操作恢复时，检查也必须先于 purge 的 present 判断，以捕获只恢复实体集合而其他记录仍缺失的情形。恢复只复用这项有界前置检查，不重新构造可能已被部分删除的整个来源图。检查失败保留计划和原数据，不报告新的完成状态。

## 验证

- 新增 test_unmodeled_derived_writers_are_off_and_refuse_preview：在真实 Hub/SDK 上确认实际配置通过检查，逐个开启 6 个开关时预览均被拒绝；去掉检查后该测试失败。
- 实测脚本（合成数据，未提交）：意图抽取不落库、digest 路由 409、纠正后预览范围。

上述 2026-11-07 验证为顺序自审，非独立同行审查。

2026-11-09 新增 `test_entity_inventory_guard.py` 31 项：真实 SDK add 和 Chroma 实体存储使用合成提炼/实体识别/embedding，验证开关开闭、关闭后残留、重启、其他项目隔离、不创建不存在的集合及异常形状拒绝。恢复部分先复现 10 项失败，再验证旧 planned forget/purge 收尾与重启、completed forget/purge 的仅实体恢复、全部项目先检查后 present、purger=None，以及没有实体时正常恢复。与治理执行、缓存恢复、worker 协调和请求身份联合回归共 94 通过、6 个既有在途缺口预期失败。

恢复绕过问题由独立审查发现并纳入修复；最终联合审查和[安装结果](../reviews/2026-11-09-chroma-purge-sequence.md) 单独记录。完整删除执行、恢复防复活、两机六入口仍按原目标继续。
