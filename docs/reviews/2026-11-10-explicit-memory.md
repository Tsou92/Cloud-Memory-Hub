# 显式记忆提炼修复与跨机验收

关联 FR-01、FR-02、FR-03、FR-07、ENG-01。设计见 [ADR-0054](../adr/0054-explicit-memory-input-role.md)。

## 故障证据

模型启用后，NAS 的既有队列为 18 个 completed、12 个 unknown，返回的记忆 ID 总计仅 1。新增一条独立合成 remember 事件后，事件 accepted，但队列终态 unknown，另一台 Mac 的 memory-search 返回零条。上述数字只描述当时任务状态，不把 completed 当作有效记忆数量。

同一安装镜像 `mvp-31737ba` 在禁网临时目录使用真实 SDK、合成模型分类响应重现：tool 输入被判为 chat，SDK 删除工具消息后得到零条消息，真实 writer 返回 `success=false,error_code=400,content or messages is required`。确认修复点为 Hub 角色映射，无需更改上游工具过滤或模型接口。

首个验收脚本误把 session_id 写成非 UUID，客户端在上传前拒绝；改为 UUID 后才产生上述 accepted 事件，两个脚本结果均留在本机私有证据目录，不把输入校验失败算作产品写入失败。

## 代码与回归

生产代码仅调整新接纳的 remember 输入角色，原文与来源保持，旧任务不重算、不重放。新增回归先产生 2 项失败，再修复；投影与 AgentAccess 43 项、原文归档与 SDK 准入相关 27 项，共 70 项通过。原版 tool 冻结队列经新代码重复接纳后，回执、任务数和 payload 均不变。`make check` 通过，包含 20 项工程测试。

## 安装与真实跨机结果

独立审查未发现阻断。提交 `6801367` 构建 Linux amd64 镜像 `cloud-memory-hub:mvp-6801367`，NAS 安装摘要 `sha256:7151e3aaa3f7e0ccdd35612ccbdf4ccfdfbe3f71c85a01c701b5ca9078470dbf`。同镜像禁网安装探针验证实际 Core 投影及 SDK 预处理后正文仍在，无外部模型调用。

NAS 无 pending/running 任务时停止 MVP，备份 data/config/governance 和部署配置并保存摘要，保留旧 `mvp-31737ba` 镜像，更新后健康。两台 Mac 的配置和客户端均保持 `272038f` 不变。

当前 Codex 桌面通过已安装 Skill 的 CLI 入口提交全新合成项目约定，接受后 16.643 秒观察到 completed；NAS 对应新任务实际返回 1 个记忆 ID、1 个原文 ID。Mac mini 从自己的安装入口经 HTTPS 搜索该唯一项目标记，0.584 秒返回候选，并命中 `kind=memory` 的真实提炼结果，包含全部预期事实及本次接纳回执来源。查原文或仅查看 completed 不计入此命中。

NAS 队列变为 19 个 completed、13 个 unknown，原 unknown 数量保持。旧现场未删除，也未换 ID 重试这些任务。新合成事件属于独立验收，不宣称旧任务已经恢复。

反向使用 Mini 的已安装 CLI 保存另一条独立合成约定，经 NAS 实际提炼，再由 MacBook 搜索命中真正 memory，全部事实和本次来源匹配；从发起远端写入到本机读取的端到端时间为 9.541 秒。这是本次样例测量，不代表所有输入的延迟承诺。

Mini 的真实 Claude Code 2.1.319 使用自己的既有登录、已安装原生 Skill 和 MCP 完成搜索并回答，进程退出 0，耗时 16.817 秒。仅给模型唯一项目标记，期望记录 ID 与三个事实词只作外部断言；实际工具序列为 Skill → memory_search，返回 `kind=memory` 且匹配本次提炼 ID，最终回答包含全部隐藏事实。无原文/交接读取、写入或权限拒绝。测试没有把 Skill 正文塞进 system 来冒充原生发现。

该宿主验收关闭自动 hooks 和会话持久化，以单独证明原生 Skill/MCP 使用真实长期记忆；不能据此宣称当前相关召回已在真实宿主同一会话下一轮自动注入。已有下一轮原文与 hook 证据仍按各自范围保留，后续只补当前闭环的组合验证。
