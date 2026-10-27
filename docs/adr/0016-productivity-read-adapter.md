# ADR-0016：工作／编程记忆的统一只读适配

状态：已实现，本地验证通过，安装验证另记。关联 FR-02、FR-03、FR-07、FR-12、ENG-01；不据此关闭 FR-09 原文长期保留。

## 复用证据与缺口

固定 Hy-Memory `b7d9f0e0f417eae30b192882f848b432484c20a6` / SDK `2.0.5.dev3`：

- `client.py:get/async_list_memories` 的普通入口只有主向量／图存储；workspace 列表只有 working 经验。
- `_search_coding` 会初始化生成模型、writer、数据库及集合，并把部分存储异常返回成成功的空结果，不适合作为 Hub 的只读适配入口。
- `coding/store.py` 已有两个 variant 的路径规则、SQLite 解码、经验向量去重、事实 active 核对及词面重排。直接复用这些实现；不另建记忆库、索引或检索算法。
- `coding_trace` 为预处理后的最近 100 次轨迹，不是长期原始记录；本变更不把 trace ID 或事实 ID 冒充 raw ID。

## 决策与合同

统一设备读取通过只读适配器合并主存储、working、coding。原有 v1 字段及权限不变。

1. productivity ID 使用 `cmhp:<w|c>:<e|f>:<native ID>`，分别表示变体、经验／事实。原生 ID 仅允许既有安全字符，整体不超过 v1 的 200 字符；不支持的存量标识返回固定 503，不能截断或碰撞。`cmhp:` 是保留前缀，普通记忆冲突也拒绝。
2. 列表按固定分段顺序：普通、working 经验、working active 事实、coding 经验、coding active 事实。各 productivity 段按创建时间倒序、原生 ID 升序。分页为实时观察，不提供跨请求快照或全局时间排序。过期事实详情统一 404。
3. 搜索复用每个 variant 的经验向量检索及事实向量／词面重排，阈值沿用上游默认 0.40／0.30。三个来源按名次交替合并，每个 variant 内事实与经验交替；不将不同检索通道的分数假定为可比较。结果总数仍受 v1 limit 限制。正文再次按当前 SQLite 记录核对，不输出旧向量中的正文。
4. productivity 正文为可读 JSON，包含事实、详细信息或任务、解决方式、适用环境等语义字段；source 为原生来源。事实链接和经验链接不属于 raw 引用，暂不加入 `source_raw_ids`。来源与历史保留能力仍不完整。
5. 查询精确限定 Hub 的项目 namespace；不能从请求注入任意 SDK user_id。完整正文先脱敏再分块，授权读取前后复核、治理隔离及容量边界保持有效。

## 生命周期和失败

适配固定 SDK 版本。SQLite 使用 `mode=ro`、`query_only`；查询使用现有 Chroma client 的 `get_collection`，不调用 writer、初始化／迁移或 get-or-create。原本没有 productivity 数据库时可视为空；已有库损坏、未知 schema、已有记录缺少集合或检索失败时返回固定错误，不伪装为空。

复用 Bridge 的共享准入及 joined worker；取消请求后，底层操作结束再释放准入和连接。SQLite 连接仅由一次操作持有，跨线程搜索使用上游锁；不得关闭共享 SDK client 或向量连接。

不变更磁盘 schema、依赖、队列回执 ID 或上游源码。回退旧镜像不会改变保存的记忆，但旧版本不能解析新增 `cmhp:` ID、也会再次漏掉 productivity 结果。管理网页仍使用上游入口，完整 UI 接通留待后续。

## 验收与后续

用真实 Core、SQLite、Chroma 及合成记录验证两变体／两种记录、同名 ID、跨项目、全局、分页边界、superseded、删除后的向量残留、缺库／坏库／缺集合、隐私、授权变化、重启和取消。另运行上游存储／检索相关回归及 `make check`。不调用真实生成模型，不导入用户历史。

长期原文保留、版本／纠错历史、完整来源关系、治理清单和删除执行另行接通；现有治理清单遇到 productivity 时继续拒绝不完整范围。
