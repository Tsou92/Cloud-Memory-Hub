# 管理员网页接口

本页为 schema_version 1 的 Hub 控制台合同；设计见 [ADR-0057](adr/0057-administrator-console-reads.md)。使用现有管理员 Bearer 或 `/hub/login` 创建的浏览器会话。设备凭据不能调用；浏览器 POST 必须带当前 Hub 的 Origin。响应不缓存，错误只返回固定 `detail`。

## 启动与项目选择

`GET /hub/v1/console/bootstrap?limit=50&offset=0`。limit 范围 1–50，offset 非负；未知参数拒绝。响应：

```json
{
  "schema_version": 1,
  "product": "Cloud Memory Hub",
  "mode": "hub",
  "instance_id": "<instance UUID>",
  "version": "0.1.0",
  "build_revision": null,
  "features": ["scoped_console_reads"],
  "projects": [{"scope_id": "<project UUID>", "display_name": "示例项目", "created_at": "<timestamp>"}],
  "next_offset": null,
  "global_scope": {"scope_id": "global", "display_name": "Global preferences"}
}
```

version 来自已安装的 Hub 包，无法取得时为 null；build_revision 暂无可信构建来源，返回 null。项目列表不包含 global，也不默认选择首个项目；global 是明确的独立选择。刷新所选项目时若已不存在，后续请求返回 404，不能显示旧数据或当作空项目。

## 所选项目摘要

`POST /hub/v1/console/summary`，请求复用 ListRequest：`{"scope_id":"<project UUID 或 global>","limit":10,"offset":0}`。响应：

```json
{
  "schema_version": 1,
  "instance_id": "<instance UUID>",
  "scope": {"scope_id": "<project UUID>", "display_name": "示例项目", "created_at": "<timestamp>"},
  "sources": [{"source_id":"<source UUID>","device_id":"<device UUID>","scope_id":"<project UUID>","adapter_id":"mcp","display_name":"示例来源","revision":1,"status":"active","device_display_name":"示例设备","device_status":"active"}],
  "sources_total": 1,
  "next_offset": null,
  "visible_memory_total": null,
  "visible_history_total": null,
  "count_state": "not_computed"
}
```

sources_total 是所选项目中包含 active/revoked 的来源记录数；状态表示授权／撤销，不表示设备在线。记忆和历史的可见总数未统计，null 不得转为 0；前端显示实际加载的数量。

## 读取项目内容

`POST /hub/v1/console/read/{operation}` 支持 `memory-list`、`memory-search`、`memory-get`、`history-list`、`history-get`。JSON 请求及 limit、正文分块、查询长度等预算复用[授权读取合同](remote-read.md)，scope_id 必填且必须存在。返回：

```json
{
  "schema_version": 1,
  "authority": "administrator",
  "instance_id": "<instance UUID>",
  "scope_id": "<project UUID 或 global>",
  "operation": "memory-list",
  "content_trust": "untrusted_history",
  "items": [],
  "returned_count": 0,
  "next_offset": null,
  "candidate_limit_reached": false
}
```

items 完全沿用 ReadItem，包括 record_id、kind、content、分页位置、来源引用与截断标识。returned_count 仅为本次返回项数。SDK 候选总数不发布为可见总数；页面可能因抑制过滤为空，next_offset 非空时仍可继续。memory-get 和 history-get 的不同入口不可互换。忘记原文排除于历史列表，明确按 ID history-get 仍可查看；彻底删除原文返回 404。

## 错误与边界

无管理员权限返回 403 `administrator_required`；会话缺少同源证明返回 403 `origin_required`，跨 Origin 为 `origin_not_allowed`。未知项目为 404 `console_scope_not_found`；不存在或越界记录统一 404 `record_not_found`。缺少／非法 scope、额外字段、超限分页为 422 `invalid_request`；非 JSON 为 `json_required`，请求超过 64 KiB 为 413 `request_too_large`。

读取沿用 `read_busy`、`read_unavailable`、`read_response_too_large`、`read_metadata_requires_review` 和 `query_requires_refiltering`；控制台摘要／项目元数据无法安全输出时为 503 `console_metadata_requires_review`，内部失败为 503 `console_unavailable`。存储异常沿用其固定错误。前端只映射已知错误为说明，未知错误显示通用失败，不回显远端正文，不把失败转换成 0 条。

上述读取接口提供项目和来源展示，不生成新身份、不配对设备或执行治理。下述问答继续使用相同的显式项目与共享过滤。

## 项目问答

`POST /hub/v1/console/ask` 和 `POST /hub/v1/console/ask-stream` 接收 `{ "scope_id": "<project UUID or global>", "question": "当前问题" }`。仅管理员可用，浏览器需要同源 Origin。同步接口返回上游 schema_version=2 的 answer、memories、mode（generated/extractive/empty）；流式沿用 recall/delta/final，完成验证后统一发送，失败为 error/code。

bootstrap 的 `scoped_console_ask` 声明这组能力。问答仅基于当前项目的有界可见记忆片段，继续走同一读取与抑制规则；不会通过旧 local 问答端点补充上下文。问题、模型失败、未知项目或治理错误均返回安全错误，不归为空库。设计与限制见 [ADR-0058](adr/0058-anthropic-memory-question-answering.md)。
