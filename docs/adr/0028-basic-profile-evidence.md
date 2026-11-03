# ADR-0028：基础画像旁路的输入与写入证据

日期：2026-11-03。状态：原生证据已实现，Hub 接线待完成。关联 FR-08、FR-09、FR-10、FR-11、FR-12。

## 复用调查与实际路径

固定上游 b7d9f0e 的 agent/tools/basic_profile.py 直接维护 L0_BASIC_INFO。它一次读取用户的旧 L0，组装完整 KV，比较新字段；通常只写差异，新建版本并更新旧链头。无差异但存在历史 Agent 链/多头时，可能复制旧完整 KV 进行迁移。这里的旧状态是确定性规则输入，不是一次额外模型调用。

writer._process_with_agent 调用该路径，但 client 的持久结果目前只透传普通语义 memory_ids，画像产出只出现在有限的过程日志；不能据此给出完整治理范围。画像节点按 user_id 共享，agent_id/session_id 为空、source 为原默认值，不能套用普通记忆的生产请求身份比较。

复用原清洗、排序、差异、迁移、互斥、存储及状态更新，不改画像算法。复用 ReconcileInputCapture 有界冻结和现有 origin 字段；通过独立 profile 摘要域绑定 basic_info_kv，保持 memory/experience/fact 摘要字节兼容。仅新增捕获、标记和持久结果透传。

## 合同与兼容性

upsert_basic_profile 增加可选 request_id/capture 参数，不改变原返回类型或 to_dict 字段。调用内捕获在旧节点查询/组装后、任何写入前完成；保存实际旧 payload（不含向量）及规则输入，包括允许字段、清洗后的新 KV、旧完整 KV 和选中的链头 ID。basic_profile_update 的 used 表示规则输入已使用，不代表调用过模型。

SDK add/add_extracted 结果增加可选 basic_profile_inputs：schema_version/status/stages，以及 attempted_output_ids、output_ids、attempted_supersede_ids、superseded_ids。未走画像路径明确 not_used；旧结果缺字段仍为未观测。upsert 返回对应 ID 后才登记 output_ids，更新旧链头明确返回 true 后才登记 superseded_ids；已尝试的 ID 单独保留。部分失败或捕获失败为 unavailable，不能当作零副作用或完整来源。effects_complete 表示副作用 ID 列表未因预算或非法标识而丢失；它不保证尝试的操作已成功，每类最多 1024 个、每个最多 512 字符。结果总预算仍为 2MiB，输入超限优先移除阶段，副作用仍超限则明确标记 effects_complete=false。

捕获沿用 512 条输入、2MiB 总预算；查询上限仍为上游 1000，不把达到容量边界的截断结果解释成完整数据。故障不改变原写入/错误处理，不新增读取。画像新节点使用 profile 摘要域覆盖普通语义字段及 basic_info_kv；未指定/无效请求保持来源未知。普通记忆的摘要行为不变。

Chroma 的原 metadata 解码会把空字符串变为 null；画像摘要仅将 agent_id/session_id 的 null 与空字符串视为同一个用户级身份，非空身份仍逐字绑定。不修改后端解码和普通记忆的旧摘要域；真实 SDK 跨 Agent 写入和重启验证此兼容性。

## 工程自审与验证

- 架构：先把旁路纳入持久结果，再继续 Hub 精确产出、历史输入与范围接线；不把空 Agent 当跨项目授权。不开放删除，后台摘要、图和其他控制输入继续按实际路径补齐。
- 代码：共享有界捕获和来源摘要，规则逻辑不另写副本；结果日志不重复塞入大快照。画像读取 KV 与正文可能有相同渲染但不同类型，必须把 KV 本身纳入来源摘要。
- 测试：差异、无变化、迁移、旧链头状态改变前冻结、并发、查询/编码/写入/旧头更新故障、预算、来源篡改及旧调用兼容；真实 SDK 覆盖只有画像而无普通记忆的持久化、重开与无重写重放。保留上游画像、模型及写入结果回归。
- 性能：复用唯一一次 L0 查询；按调用冻结，不增加模型调用或共享“最近一次”状态；超限明确未知。安装、NAS 和合成模型证据分开记录。

这是顺序自审，非独立同行审查。Hub 画像来源接线、异步任务与删除/恢复仍属于完整交付范围；本阶段原生证据不能替代这些验收。
