# ADR-0062：客户端只读处理进度与采样时间

状态：接受；关联 FR-01、FR-02、FR-07、FR-12、R2-04。

## 复用与边界

复用固定上游 `one_memory.memory_server.durable_queue.DurableWriteQueue.get()` 的
`DurableWriteRecord.state/created_at/updated_at`。Hub 的事件回执已经读取该对象，无需增加
状态库、迁移数据库、轮询守护进程或离线缓存。客户端继续复用 `LocalOutbox` 的绑定与私有文件
检查，以及 `transport` 的有界请求、错误码和身份校验。不新增提炼或投递实现。

## 回执兼容性

`POST /hub/v1/events` 与 `GET /hub/v1/events/{event_id}` 的现有响应增加两个字段：

- `server_enqueued_at`：上游任务首次持久入队的 UTC ISO 8601 时间；不是源对话发生时间。
- `processing_updated_at`：上游任务最近更新的 UTC ISO 8601 时间；不是另一设备读取时间。

保持 `protocol_version=1` 和既有字段。旧客户端忽略新字段；新客户端访问旧 Hub 时将缺失的
时间显示为 `null`，仍可检查已有处理状态。只投影这些时间和已有回执元数据，不返回
`payload_json`、`job.detail`、SDK 对象或任何源正文。

## 本地采样

`LocalOutbox.recent_metadata()` 按当前配置的项目和来源筛选，按本地 `sequence DESC`
返回最多五条已接纳或待传事件，排除终止拒绝项。SQLite JSON 字段仅用于项目／来源筛选；
选中行在内部校验 JSON、事件绑定、摘要与当前过滤规则，返回对象没有正文。
非法 JSON、元数据损坏或绑定不符拒绝本次检查，不将错误解释为空队列。
现有表与队列版本保持不变。

## 状态命令

`setup status` 不发请求。`setup status --check-remote` 先检查现有设备／项目／来源授权，
再以同一设备凭证查询最多五条采样事件回执。只发 GET；不触发上传、ack、重试或队列修改。
查询复用现有 HTTP 客户端，限制并发与总等待时间，逐项保留安全错误码。

核对实例、设备、事件 ID、摘要、已知回执 ID、处理状态和有时区时间后，才展示元数据。
待传事件收到明确 `event_not_found` 时显示“无服务器回执”；本地已接纳事件收到同一结果时
显示回执缺失异常。路由不存在、权限失败和网络失败不得解释为事件尚未接纳。

输出明确标记本地入队顺序采样、最多五项、检查时间，以及样本中已知的最近首次入队时间和
已完成任务更新时间。它们不是设备全历史最后成功时间；时间缺失不补成本机当前时间。
`completed` 仅表示服务器任务完成，不表示记忆提炼成功、检索可用或另一设备已经读取。
`pending/running/failed/unknown/dead/cancelled/waiting_for_permission/waiting_for_model`
保留其原有状态。样本部分失败仍保留其他已核验项，CLI 返回非零；旧 Hub 缺时间不算失败。

## 验收

验证服务端时间与原任务一致、旧 Hub 缺字段兼容、项目／来源隔离、最多五项和顺序、终止拒绝
排除、非法 JSON／重复键／摘要或绑定损坏、伪造回执、错误时间／状态、明确未找到与路由缺失
的区分、部分网络失败及空样本。合成测试核对命令前后队列字节不变、只有 GET，并确认输出
不含正文、凭证和远端额外字段。真实 NAS 与第二台设备另行验收，不由这些测试替代。
