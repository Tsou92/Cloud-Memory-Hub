# ADR-0065：NAS 远程 MCP 与一次性链接接入

状态：接受，实施中；关联 FR-01、FR-02、FR-03、FR-05、FR-06、FR-07、FR-12、ENG-01。

## 目标和范围

用户于 2026-11-10 明确：产品主体运行在 NAS Docker，其他设备通过一条命令或交给 Agent 一段安装指令接入，使用一次性链接完成配对，无须单独安装图形 App。默认路径应完成配置及真实连接检查，不能把下载 JSON 当作连接成功。用户同时明确先完成功能、缺陷修复和使用体验；后台 UI 视觉风格留待后续指定。

默认路径为远程 MCP 加 Skill；旧 stdio 连接器和 JSON v1 导入保留为高级入口。远程工具本身不读取本地历史文件，也不提供断网时的本地补传；已有 hooks、采集和离线连接器继续保留，分别验收。不会因增加 HTTP 接口而宣布所有 Agent、Windows 或自动逐轮采集已通过。

本轮不新增独立 App、账户服务、OAuth 平台、短数字验证码系统、二维码专用协议或视觉设计系统。链接已经满足用户允许的配对形式；只支持 OAuth 发现而不能配置设备 Bearer 的宿主仍需另验。

## 现有实现及复用

- 固定上游 `b7d9f0e0f417eae30b192882f848b432484c20a6` 的 `UISessionStore` 仅有进程内短时网页登录交换码，没有持久设备、项目权限、原子领取或丢响应恢复。继续复用网页登录，不把它改造成设备身份库。
- `DeviceAccessStore`、`RemoteIngest` 的项目、设备、grant、来源均在同一个身份 SQLite 中，沿用 `BEGIN IMMEDIATE`、现有凭证摘要和撤销机制；只增加一次性邀请表。
- 官方锁定 `mcp==2.3.0` 的 `MCPServer.streamable_http_app()` 已有无状态 Streamable HTTP，继续复用既有七个工具定义和固定错误边界。
- `AgentAccess` 的输入验证、凭证过滤、事件及交接格式，以及 `RemoteIngest`、`RemoteRead` 的授权、幂等、过滤、治理与范围限制继续复用。服务端不另建记忆库或持久 outbox。
- 安装结果复用现有 PairingBundle v1 的身份合同和真实 `session`、`source/access` 检查；客户端安装只生成连接配置和 Skill，不能要求全量 Core/模型依赖作为默认前置。

## 一次性邀请合同

管理员 `POST /hub/v1/pairings` 以 `{scope_id, display_name}` 创建邀请；项目必须已存在且不是隐含全局授权。权限固定为该项目的 `events:write`、`memory:read`、`history:read`，adapter 固定 `cloud-memory-hub.agent`。服务器返回 `pairing_id`、256 位 URL-safe `invitation_secret`、`expires_at`；有效期 10 分钟。生成邀请时不创建设备。查询和撤销接口只返回非秘密状态。

客户端 `POST /hub/v1/pairings/claim` 提交 `{pairing_id, invitation_secret, claimant_secret}`。`claimant_secret` 必须由客户端 CSPRNG 生成 32 字节，先写入本机私有 pending 文件并 fsync，再发请求；它用于构造现有 `cmhdev_v1_<device UUID>_<secret>`，不允许由网页模型生成或显示长期凭证。接口只返回 `schema_version`、`kind`、服务器配置的 `origin`、`instance_id`、`device_id`、`scope_id`、`source_id`，不回显秘密。

领取在一个事务中验证邀请、创建设备、授权、来源并标记 claimed。抽取接收现有 db 的小辅助函数复用旧 API 规则，禁止串联三个独立 public 事务冒充原子领取。同一 claimant 的重试返回同一身份；其他 claimant 不能再次领取。恢复回执必须重新核验原设备凭证摘要、项目 grant 和来源仍有效；轮换、撤销、来源失效后返回固定失败，不重新赋权或创建设备。已提交却丢失响应、进程中断或本地写盘失败时保留 pending，重试原身份。

邀请仅存摘要；已领取记录存 claimant 摘要与结果 ID，不保存明文设备凭证。邀请秘密只放 URL fragment 或 JSON body，不进 query、服务器日志、浏览器持久存储。一次性链接以 Hub 配置的 public_origin 生成，不信任任意 Host。API 沿用严格 JSON、有界请求体、no-store 和安全固定错误码；claim 的邀请授权不会赋予 cmh_admin。

```text
管理员选项目 -> 创建10分钟邀请 -> 复制链接/安装指令
                                      |
客户端私有 pending 落盘 -> claim -> 同事务: 设备 + grant + 来源 + claimed
              |                       |
              +---- 同身份重试 <---- 响应丢失
                                      |
                        验证绑定 -> 备份并写 Agent 配置/Skill -> MCP 实测
```

## 远程 MCP 合同

端点 `/hub/v1/mcp/{source_id}` 使用设备 Bearer，每请求核验来源属于该设备且仍有效，由来源决定项目。七个工具参数不新增 scope、device 或 source 覆盖能力。每次工具调用的业务授权仍经过既有读写边界，不能依赖初始化时的身份或 MCP session。管理员 cookie 不能充当设备凭证。

使用官方无状态 HTTP transport 和 JSON 响应；先启动 Core 和 MCP session manager，再完成串行治理恢复，才向服务端报告启动完成；关闭时先退出 MCP。传输启动中的异步让出不能破坏原有就绪保证。继续检查配置的 Host/Origin，不关闭传输检查。请求上下文只能存活于本次调用，不能共享一个设备的 service 给其他并发设备。

传输退出作为独立资源回调执行，不将 Hub 的启动／运行异常注入 MCP TaskGroup；保留原错误和取消传播。MCP 自身退出失败仍必须报告，不通过展平或忽略异常组制造成功。

从 `AgentAccess` 提取必要的共享验证/交接操作，客户端仍走原 outbox；服务端传输适配复用内部 REST ASGI 应用或既有带 admission 的入口。远程保存的成功只表示既有 Hub 持久接收；失败明确返回可重试状态，不伪造本机 queued。远程 status/sync 需明确没有客户端离线队列，不能把这一差异藏在旧 stdio 文案中。

参考官方 [Streamable HTTP 规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。当前凭证是显式配置的设备 Bearer，不宣称实现 [OAuth 发现和授权规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)。

## 安装、迁移及兼容性

现有 JSON v1、token v1、stdio 配置不变。新增邀请采用身份库独立版本化扩展表，不改既有 PRAGMA user_version。备份恢复时取消所有恢复出的邀请（包括已领取状态），避免旧备份复活一次性授权；设备与撤销仍沿用原安全合并。旧版本不提供邀请入口；回退前停止新接入并保留升级前备份，不通过丢弃表或恢复已用邀请来回退。

一行入口和 Agent 安装话术必须对应发布制品中的固定脚本；脚本不从任意回复 URL 下载代码。既有配置先备份、仅合并 Cloud Memory Hub 对应连接和 Skill；重跑保留其他连接、当前绑定和历史。失败应说明是网络、配对过期、宿主不支持还是需要重新加载，不能要求新手检查原始栈。只有真正测试过的宿主和系统标为支持；Windows 脚本的存在不是 Windows 真机通过证据。

后台接入在没有指定已有项目目录时可用 `--managed-workspace`。连接器核验远端绑定后，按实例、项目和来源标识在用户主目录下创建私有专用目录，只在该目录安装项目钩子。同一邀请重试使用相同目录。新邀请创建独立来源时使用另一目录，避免覆盖已安装的钩子或更改旧绑定。调用方仍可用 `--workspace` 明确选择已有目录，旧调用省略这两个选项时仍只配置 MCP 和 Skill。主目录及其祖先始终不能用作采集范围。Codex 仍由宿主审查新钩子，安装器报告已安装和等待信任的状态。

## 测试计划与失败路径

```text
邀请: 有效 / 过期 / 撤销 / 非法范围 -> 身份库+HTTP契约测试
领取: 同人重试 / 双人并发 / 提交后丢响应 / 重启 -> SQLite集成测试
恢复: token轮换 / 设备或来源撤销 / 备份恢复 -> 旧邀请不可复活测试
MCP: 初始化 / 七工具 / 两身份并发 / 跨项目 / 撤销 -> 官方客户端端到端
安装: 新装 / 重跑 / 冲突 / 写盘失败 / 未知宿主 -> 隔离配置和制品测试
产品: NAS真机 / 两Mac共享 / Agent加载 / 下一轮读取 -> 逐项记录真实证据
```

新增状态均提供固定错误与可恢复路径；授权变更必须失败关闭。邀请查询有界、claim 主键查找，业务查询沿用已有并发限制；不增加后台轮询守护服务或全库扫描。工作并行分为身份/恢复、MCP适配、安装/引导；共用装配文件由主任务集成。单元、契约、既有回归、make check 和真实目标验收分别记录，缺项不得算通过。
