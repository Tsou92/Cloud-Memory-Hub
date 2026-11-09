# 远程来源与事件接纳接口

协议 v1，实验阶段。设计与兼容性见 [ADR-0006](adr/0006-remote-ingest.md)，设备签发／授权见 [ADR-0004](adr/0004-remote-device-access.md)，本地过滤事件见 [连接组件说明](local-connector.md)。使用合成记录验证，不接入真实历史。

## 来源管理

| 路径 | 权限与方法 | 合同 |
| --- | --- | --- |
| `/hub/v1/sources` | 管理员 GET | 返回 `{sources:[...]}`；不含设备凭证 |
| `/hub/v1/sources` | 管理员 POST | `{display_name,device_id,scope_id,adapter_id}`；201 返回服务器生成的 source_id、输入字段、revision=1、status=active |
| `/hub/v1/sources/{source_id}/revoke` | 管理员 POST | `{expected_revision}`；200 返回新 revision 和 revoked 状态 |
| `/hub/v1/sources/{source_id}/access` | 设备 GET | 返回当前 protocol_version、instance_id、device_id／device_revision、source_id／source_revision、scope_id、allowed=true；不匹配／无授权统一 403 |

`device_id` 必须是已签发的活动设备，`scope_id` 必须是已创建的项目 UUID 或 `global`。注册来源不授予权限；设备还需该 scope 的 `events:write`。`adapter_id` 是 1–64 个小写字母、数字、点、下划线或连字符，首字符为字母／数字；它是扩展标识，不是固定的 Agent 品牌列表。

来源不能转移设备或项目。需要重新关联时新建来源；撤销终止自动上传和后续处理授权，不代表彻底删除先前合法接纳的内容。

## 上传与状态

`POST /hub/v1/events` 使用设备 Bearer，提交本地队列产出的完整事件，字段见连接组件说明。须为 `application/json`；HTTP 正文上限 1 MiB + 4096 字节，过滤前后的 content 各自最多 1 MiB。来源所属设备、项目和当前授权必须同时匹配。管理凭证不能代替设备身份上传。

- `conversation_turn`：content 为 `{messages:[{role,text,metadata?}],metadata?}`，1–200 条消息；role 仅 user／assistant／tool。
- `operation`：content 为 `{text,metadata?}`，关键命令结果或操作证据存于 text 和 metadata。
- text 非空；metadata 为 JSON 对象。未知字段、重复 JSON 键、非法类型、未知媒体和未过滤凭证拒绝。禁止静默截断正文或丢弃元数据。
- ID 使用 UUID v4，revision 为正整数；`occurred_at` 可省略或使用带时区时间。未提供时间时保留“时间不精确”的处理语义。

成功返回 HTTP 202：

```json
{
  "protocol_version": 1,
  "instance_id": "11111111-1111-4111-8111-111111111111",
  "device_id": "22222222-2222-4222-8222-222222222222",
  "event_id": "33333333-3333-4333-8333-333333333333",
  "payload_digest": "<已过滤规范事件的 SHA-256>",
  "receipt_id": "44444444-4444-4444-8444-444444444444",
  "status": "accepted",
  "processing_state": "pending"
}
```

这些 UUID 是示例，不是有效身份。客户端只能在实例、设备、事件和摘要全部匹配后确认本地队列。丢失响应使用完全相同的事件重放；同 ID 不同内容，或同来源／会话／记录修订另发新事件，返回 409。同一记录真正修订时使用新的 event_id 和 revision，不覆盖原记录。

`GET /hub/v1/events/{event_id}` 使用同一设备身份，返回相同回执和当前上游处理状态。其他设备看不到此回执。设备／来源撤销或写权限移除后拒绝读取；状态接口不返回原文、模型错误详情或记忆内容。

`accepted` 表示上游任务和完整已过滤规范事件均已持久保存；原文与回执在同一事务提交，详见 [ADR-0017](adr/0017-accepted-original-archive.md)。它只表示接纳，`processing_state` 保留上游状态，未配置模型时可以一直为 pending。即使显示 completed，也需要另行验证检索结果和另一端可用性。客户端不得把 202 当作 60 秒目标达标。

错误为固定 `{detail: code}`，不会回显正文。401 身份无效，403 来源／项目未授权，404 回执不存在，409 实例／事件／记录修订冲突，413 超限，422 合同或过滤复核失败，503 持久库／回执／运行时不可用。请求与响应均不得记录认证凭证或原始正文，响应标记 `Cache-Control: no-store`。

忘记或删除规则在入队前检查。稳定来源记录命中返回 409 `governance_source_suppressed`；任一消息经 NFC 和空白规范化后至少 16 个字符且内容命中，返回 409 `governance_content_suppressed`，整条事件均不接纳。内容规则按项目隔离，换来源、会话或记录 ID 不能绕过。旧治理操作无法证明规则时返回 503 `governance_source_rules_unavailable` 或 `governance_content_rules_unavailable`，不会静默放行。设计与兼容细节见 [ADR-0034](adr/0034-inflight-ingest-and-content-reimport.md)。

治理进入独占阶段时，新上传返回可重试的 503 `governance_maintenance_in_progress`；已经进入接纳的线程先完成。客户端取消请求不撤销已持久接纳的事件，应继续使用同一事件 ID 查询或重放，不另生成事件。

原文存于接纳扩展 schema 2；旧 schema 1 必须按[运行指南](development-runtime.md)停服备份并显式迁移。无法从旧冻结输入逐项证明原文时整次拒绝迁移。`original_archive_invalid` 表示原文完整性核验失败，重放、状态和历史读取均拒绝成功响应；不能用重放覆盖损坏归档。原文可通过带 `history:read` 授权的[历史读取](remote-read.md)查阅，任务清理和来源停止写入不会删除它。

## 当前边界

握手 features 公布 device_identity、source_registration、event_upload、source_preflight；并未公布检索或同步能力。来源访问回执使用当前设备身份、来源归属、活动状态和 events:write 授权，不返回其他设备的来源信息。它供客户端在正文出站前预检；接纳事务仍复核权限，预检不能撤回已发出的网络请求。

连接 CLI 已提供 send／watch，见 [配置说明](local-connector.md)，自动配对界面仍待完成。上传端直接使用已有 Core 持久队列，不另设模型调度；服务端过滤只是复核，不能补救已上传的秘密。授权读取及 Claude 隔离采集／在线召回已有验证；其他客户端、跨源历史去重、忘记与删除传播、完整备份及防复活仍需完成。原始字段在接纳归档中长期保留，并随 SDK 输入保存来源资料；后续召回必须将其当作历史数据，不能当作系统指令或操作授权。
