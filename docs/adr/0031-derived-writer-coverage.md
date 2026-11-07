# ADR-0031：派生写入路径的覆盖边界

日期：2026-11-07。状态：已实现。关联 FR-09、FR-10、FR-11、ENG-01。

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

## 验证

- 新增 test_unmodeled_derived_writers_are_off_and_refuse_preview：在真实 Hub/SDK 上确认实际配置通过检查，逐个开启 6 个开关时预览均被拒绝；去掉检查后该测试失败。
- 实测脚本（合成数据，未提交）：意图抽取不落库、digest 路由 409、纠正后预览范围。

顺序自审，非独立同行审查。删除执行、恢复防复活、两机六入口仍按原目标继续。
