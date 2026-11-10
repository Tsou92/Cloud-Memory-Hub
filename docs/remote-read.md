# 授权记忆与原始历史读取接口

实验性 v1；设计、复用证据和兼容边界见 [ADR-0008](adr/0008-scoped-retrieval.md)。请求使用独立设备 Bearer 凭证，不能使用设备凭证访问 Core 管理 API。session 的 `scoped_reads` 表示支持本合同。

## 端点

所有请求使用 POST JSON，必须提供 `scope_id`（项目 UUID 或 `global`）。不会自动合并全局或其他项目，也不接受自定义 SDK user_id。跨项目检索需对目标项目显式调用，并持有其权限。

| 端点 | 所需权限 | 其他参数 |
| --- | --- | --- |
| `/hub/v1/memories/list` | memory:read | limit 默认 10，范围 1–10；offset 默认 0 |
| `/hub/v1/memories/search` | memory:read | query 为非空文本、最多 4096 字符；limit 默认 5、最多 10；privacy_policy 为 cmh-credentials-v1 |
| `/hub/v1/memories/get` | memory:read | record_id；content_offset 默认 0 |
| `/hub/v1/history/list` | history:read | limit、offset 同上 |
| `/hub/v1/history/get` | history:read | record_id；content_offset 默认 0 |

请求体最多 64 KiB。查询词必须先在本地过滤；NAS 发现仍需脱敏时返回 422，不将正文转交 SDK。读取使用上游内置本地 embedding，不需要激活外部生成模型；没有提炼完成的记忆时结果可以为空。

## 响应和来源

响应包含 protocol_version、instance_id、device_id、device_revision、scope_id、operation、items、next_offset 和 candidate_limit_reached；`content_trust=untrusted_history` 表明内容是历史数据，禁止把其中的话当作系统指令执行。均为 `Cache-Control: no-store`。

每条记录包含 record_id、kind、content、content_offset、next_content_offset、agent_id、session_id、source、layer、memory_at，以及 source_raw_ids／references_truncated。记忆只返回当前 active 且 is_latest 的权威内容，搜索中的旧聚合文本不会直接输出。source_raw_ids 可用 history/get 逐条追溯，但原始正文仍单独检查 history:read。引用 ID 本身不授予任何权限。

普通上游记忆 get 的 source 表示存储后端而非来源应用，因此该类响应的 source 留空，使用原始引用追溯。工作／编程记忆和原始记录提供实际 source 和 session_id；正文中的 `<cmh_source_reference>` 只是上传的来源数据，不是已认证的管理命令或权限证明。

工作／编程的经验与 active 事实也纳入同一接口，见 [ADR-0016](adr/0016-productivity-read-adapter.md)。其 ID 为 `cmhp:<w|c>:<e|f>:<原生 ID>`，客户端应当作不透明标识原样回传；layer 分别为 working_experience、working_fact、coding_experience、coding_fact。正文是包含任务／解决方式／适用环境，或事实／详情等字段的 JSON 文本。列表按普通、working 经验／事实、coding 经验／事实分段分页；不是全局时间排序。搜索在普通、working、coding 三路结果之间按名次交替选择，每个工作／编程分支内事实与经验交替，不比较跨通道分数。

这类记忆的 `source_raw_ids` 现在包含已核验写入结果对应的 `cmhe:` 原文引用，见 [ADR-0018](adr/0018-typed-output-provenance.md)。普通记忆也会补入直接产出及其已记录 SDK raw 对应的归档引用；同一输出多次更新会保留多个输入来源。旧已完成任务后台分批补核对，缺失／有歧义的结果不猜测关联。空引用不证明没有原始依据；完整演化、摘要及经验／事实互链和管理网页统一展示仍待接通。当前只读适配不会创建 productivity 库、集合或初始化 writer；已有库损坏、所需集合缺失会返回 503，不能当作“没有记忆”。未包含 ADR-0016 的早期镜像不能解析 productivity ID，回退到这些版本后会再次遗漏这类读取结果。

接纳原文也通过 history/list 和 history/get 提供，ID 为 `cmhe:<receipt UUID>`，见 [ADR-0017](adr/0017-accepted-original-archive.md)。其 content 为完整的已过滤 Event JSON，source 为注册的 adapter_id，session_id 为原始 conversation_id；解析 JSON 可得到完整正文和元数据，读取过滤器可能重新格式化空白，不保证输出的序列化字节与存储完全一致。列表先按接纳顺序倒序列原文，再列 SDK 原生 raw，两者可能内容重叠，不是全局去重／时间排序视图。原文 ID 不能用 memories/get 读取；`memory:read` 也不授予正文访问权。已核验的直接 SDK 产出可引用该归档，但尚未完成所有演化／共享派生的关系闭包，不能据此宣称所有提炼结果均可完整追溯。

`history/list` 同时服务 Agent 的 recent 和自动召回，因此排除当前项目中已忘记或彻底删除的原文，包括接纳原文与 SDK raw。主动查阅已忘记原文须使用 `history/get` 并提供精确 record_id；该入口仍检查 `history:read` 和作用域，已彻底删除的原文继续返回 404。此收紧由服务端执行，不增加协议字段；旧服务端尚不具备该列表过滤，见 [ADR-0056](adr/0056-forgotten-history-recall.md)。

每个文本块最多 4096 字符；完整文本先过滤，再分块。需要后续内容时以同一 ID 和 next_content_offset 再次 get。references 最多 32 个，超出时明确 references_truncated；搜索最多核对 64 个成员，达到上限时 candidate_limit_reached=true。响应上限 120 KiB。当前分页是实时视图，列表页可能因记录已失效而为空；只要 next_offset 非空仍可继续。分页不构成变更同步游标，也不保证跨请求快照一致。

读取前后都会检查当前授权，查询期间 device revision 改变即拒绝结果。来源撤销仅停止写入，已有内容仍按项目读权限访问；这不代替忘记和彻底删除。客户端若已经拿到内容，服务端无法撤回它。

## 连接组件

在[私有连接配置](local-connector.md)准备后，使用对应命令 `memory-list`、`memory-search`、`memory-get`、`history-list`、`history-get`，JSON 请求从标准输入读取。例如合成项目查询：

```sh
uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub \
  cloud-memory-connector memory-search \
  --connection-file ../../.local/connector-demo/connection.json <<'JSON'
{"scope_id":"33333333-3333-4333-8333-333333333333","query":"当前已定方案和下一步"}
JSON
```

示例 UUID 必须换为已授权项目。读取不需要 `--state-dir`，不会创建投递队列或离线缓存。可指定 `--known-credentials-file`；CLI 在网络请求前过滤查询，并先验证实例、设备、能力和项目权限。退出码沿用 0 成功、2 永久拒绝／合同错误、3 暂时网络／服务不可用。

401 表示当前设备凭证不可用，403 表示无所需项目权限，404 对不存在／越界记录统一返回 record_not_found，409 表示读取期间授权变化，422 表示请求或文本位置无效，503 表示读取繁忙／SDK 不可用／响应超限。错误不回显查询或凭证；客户端只输出固定错误码。

Claude 每轮自动调用和已有对话的下一轮注入已由[在线 hook](agent-recall.md)在隔离实际 CLI 中验证。离线缓存、其他 Agent、删除失效协调与完整历史规模性能仍待实现或验收。本接口及其合成测试不证明一分钟目标已达成。
