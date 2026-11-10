# ADR-0058：复用 Anthropic Messages 完成记忆问答

日期：2026-11-10。状态：已实现，通过隔离回归、NAS 安装与真实 Anthropic 问答验收；浏览器交互另行验收。关联 FR-07、FR-12、ENG-01。

## 问题与复用

现有 SDK 记忆问答器支持检索、权威记录复核、引用校验和模型配置，但明确拒绝 Anthropic provider。已配置的 Anthropic 兼容模型可以提炼记忆，却不能用于网页“问记忆”。

复用固定上游 `b7d9f0e0f417eae30b192882f848b432484c20a6` 的 `SdkMemoryAnswerer`、JSON 答案解析和引用校验，以及现有补丁中 `hy_memory.agent.anthropic_messages` 的 URL、请求转换和响应解析。仅补现有问答 provider 分支，不另建问答服务、模型配置或密钥存储。

## 行为与兼容

Anthropic 问答发送 Messages 请求，明确要求返回现有 answer/citations JSON。沿用当前模型版本的密钥、timeout、thinking 配置；禁止跳转，网络、响应与格式错误使用已有安全错误码，不返回密钥或模型原始响应。

首版流式入口在完整获得、解析 Messages 响应后，再交给现有流式答案解析器；不新增 token 流协议解析。调用前后与结果发出前继续进行生成授权和上下文复核。截断、工具调用、非 JSON、无效引用均不作成功答案。

OpenAI compatible 和 Ollama 行为不变，不修改数据库或外部 SSE 数据形状。Core 默认检索仍属上游 local 行为；Hub 网页必须另由 ADR-0057 的项目作用域查询提供过滤后的 bridge，并在前后复核作用域和治理状态。单独通过 provider 测试不等于 Hub 网页接线完成。

## 验证

合成 HTTP 截获验证 URL/headers/system/messages/JSON要求；同步与流式回答使用同一引用结果。覆盖截断、错误状态、网络失败、错误 JSON/引用、上下文变化和授权复核。保留既有 SDK QA 与一般 QA 回归。真实 NAS 模型与网页验证另行记录，不把 MockTransport 当外部服务通过。

## Hub 网页接线

新增管理员专用 `POST /hub/v1/console/ask` 与 `/ask-stream`，请求必须包含明确的 `scope_id` 和非空、最多 4096 字符的 `question`，继续由 Hub 会话与同源规则保护。bootstrap 声明 `scoped_console_ask` 才让网页启用；设备凭据不能调用此管理员入口。

Hub 使用薄的 `ScopedAnswerBridge` 将既有控制台 memory-search/get 结果交给上游回答器。每次搜索和生成前后 get 都重新通过项目、active/latest、忘记/清除、权威 hydration 与凭证过滤，不能直接绕回 Core local 检索。最多使用 6 个记忆片段，每条沿用 4096 字符预算，引用可由当前项目记忆详情继续查阅。问题含可识别凭证时在出站前拒绝，答案如触发过滤或超出响应预算则返回固定错误。

模型 profile/revision 在每次生成复核时重新读取；同时检查当前记忆投影和运行治理状态。一次问答持有现有 SDK operation admission，治理独占维护等到本次调用退出；限制同时进行 2 次问答，取消和错误均释放槽位。没有模型时仍允许上游 extractive 回答，空项目返回 empty，模型调用失败不能伪装为空库。

网页流式端点沿用原 SSE 事件形状，但完成整份答案和引用验证后才发送 recall/delta/final，避免在失败前显示未经核实的部分答案。错误发送固定 error 事件，不附模型原文。这里不声称逐 token 实时生成。

三个成功事件合并在一次输出中，避免事件之间完成遗忘后继续发送旧答案。过滤比较采用相同的 Unicode 规范形式，普通中文全角标点不当作凭证替换；正常回答保留原标点，实际凭证仍拒绝。实现与验证边界见[项目控制台记录](../reviews/2026-11-10-project-console.md)。
