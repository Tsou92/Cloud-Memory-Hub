# 显式记忆提炼修复与跨机验收

关联 FR-01、FR-02、FR-03、FR-07、ENG-01。设计见 [ADR-0054](../adr/0054-explicit-memory-input-role.md)。

## 故障证据

模型启用后，NAS 的既有队列为 18 个 completed、12 个 unknown，返回的记忆 ID 总计仅 1。新增一条独立合成 remember 事件后，事件 accepted，但队列终态 unknown，另一台 Mac 的 memory-search 返回零条。上述数字只描述当时任务状态，不把 completed 当作有效记忆数量。

同一安装镜像 `mvp-31737ba` 在禁网临时目录使用真实 SDK、合成模型分类响应重现：tool 输入被判为 chat，SDK 删除工具消息后得到零条消息，真实 writer 返回 `success=false,error_code=400,content or messages is required`。确认修复点为 Hub 角色映射，无需更改上游工具过滤或模型接口。

首个验收脚本误把 session_id 写成非 UUID，客户端在上传前拒绝；改为 UUID 后才产生上述 accepted 事件，两个脚本结果均留在本机私有证据目录，不把输入校验失败算作产品写入失败。

## 代码与回归

生产代码仅调整新接纳的 remember 输入角色，原文与来源保持，旧任务不重算、不重放。新增回归先产生 2 项失败，再修复；投影与 AgentAccess 43 项、原文归档与 SDK 准入相关 27 项，共 70 项通过。原版 tool 冻结队列经新代码重复接纳后，回执、任务数和 payload 均不变。`make check` 通过，包含 20 项工程测试。

升级与新的真实非空记忆产出、跨机检索结果待补充。既有 unknown 现场保留，未自动换 ID 重试。
