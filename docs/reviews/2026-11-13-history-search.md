# 历史原文全文搜索

日期：2026-11-13。关联 FR-07、FR-12。补丁 `0056-hub-history-search.patch`。

## 问题

历史原文页固定写「本页暂不提供历史全文搜索」，只能 10 条一页盲翻。上游 SDK 的
`search_sdk_raw` 早已存在（`l1_raw` 层语义检索），Hub 只需把它接进读取合同。

## 实现

- **协议**：`read_contract.py` 新增 `HistorySearchRequest`（`query` 1–4096、
  `limit` 1–10、`offset`），并在 `ReadOperation` 与 `READ_OPERATIONS` 注册
  `history-search → /hub/v1/history/search`。由此自动获得设备路由、控制台路由、
  connector CLI 子命令、`history:read` 权限判定与 `kind="history"` 校验。
  刻意**不继承** `ListRequest`：读取路径先按 `ListRequest` 分派，继承会让搜索走错分支。
- **查询隐私**：`remote_read.query()` 的同构元组加入新类型，继续走 NFKC 规范化与
  `CredentialFilter`，命中即 `query_requires_refiltering`（422）。
- **数据层**：`_read` 在 `ListRequest` 之前处理新类型，调用
  `bridge.search_sdk_raw(query, user_id=namespace, limit=wanted)`。该 SDK 接口没有
  offset，因此按 `offset+limit` 取回后本地切片，并设 `MAX_HISTORY_SEARCH = 50`
  上界，避免一次请求无限放大。`HySdkRawHit` 本身是记录子类，直接使用，不额外
  `get_sdk_raw`。
- **抑制规则**：结果统一走既有 `visible(record, history=True)`，忘记与彻底删除
  的内容不会出现在结果里；与列表一致，不做例外。
- **界面**：`SdkRawHistory` 的 reader 增加可选 `search`；控制台实现走
  `readHub(scope, "history-search")`。只在服务端支持时渲染搜索框，并说明「搜索范围
  是当前项目已接纳的原文；忘记与彻底删除的内容不会出现在结果里」。

## 验证

- `test_history_search_contract.py` 4 项：请求类型不与 `ListRequest` 混淆、
  查询经 NFKC 与凭证过滤、offset/limit 边界、空查询拒绝。
- `SdkRawHistory.test.tsx` 与 `HubConsole.test.tsx` 合计 36 项通过；`tsc -b` 通过。
- 54 个补丁从干净检出准备通过。

## 未覆盖

- 搜索无游标（SDK 限制），只支持「取消搜索 / 重新搜索」，不做连续分页；
  单次最多 50 条候选。
- 无关键词高亮、无时间／来源筛选。
- 搜索不进入自动召回上下文（仅人工查询），避免忘记后的原文经搜索回流。
