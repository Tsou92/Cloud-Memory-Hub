# ADR-0054：显式记忆保留为 Agent 内容

日期：2026-11-10。状态：实施中。关联 FR-01、FR-02、FR-07、ENG-01。

## 故障与复用证据

真实 MCP/CLI `remember` 事件在 NAS 被接纳，提炼任务却进入 unknown。现有 AgentAccess 把显式记忆标记为 `operation`，RemoteIngest 无差别把所有 operation 转为工具输出。SDK 看到工具消息后进行场景分类；分类为 chat 时，既有 `strip_tool_messages` 删除全部工具消息。已在同一安装镜像、禁网、合成模型条件下复现：一次分类请求后消息列表为空，真实 SDK writer 返回 `success=false,error_code=400`，Core 保守映射为未知结果。

继续复用事件、隐私过滤、投递队列、Core EvidenceRecord 和 HY 提炼；不改 SDK 的工具过滤、不建立另一套提炼机制。显式保存记忆是 Agent 提交的文字，不是命令执行输出，应投影为 assistant / WrittenBy.AGENT。

## 最小兼容修复

仅对 metadata 严格标记 `agent_access_kind=remember`、整数版本 `agent_access_version=1` 的 operation 使用 assistant 角色。普通 operation、handoff、未知版本及其他采集角色维持原行为。布尔值不当作整数版本。保留原文字、元数据、来源、项目范围及事件身份，不伪装成用户原话。

客户端 MCP/CLI 参数、事件格式、摘要与幂等身份不变；无需修改两台 Mac 配置。修复只作用于新接纳事件的投影，不重写已有冻结队列、不重放 unknown 任务、不删除既有失败现场。旧事件重复提交仍返回既有回执。

## 验证

复用原投影、队列和 AgentAccess 回归，覆盖专用记忆保留、普通工具操作不变、版本拒绝、原文及来源完整，以及真实 SDK 预处理不会删除显式记忆。升级后以全新合成事件验证非空记忆产出，再在另一台 Mac 搜索并使用；接纳或 completed 均不能单独替代最终检索验收。
