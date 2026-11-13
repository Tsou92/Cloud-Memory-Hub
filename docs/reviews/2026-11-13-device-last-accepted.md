# 设备最近上传时间

日期：2026-11-13。关联 FR-04、FR-07、FR-12。补丁 `0049-hub-device-last-accepted.patch`，后端见 `console_dashboard.last_accepted_times`。

## 问题

需求第 4、7 节要求「查看设备连接与同步状态」和「查看同步失败原因」。实际设备表只有 `active`／`revoked` 授权状态，界面只能写「已登记……不代表实时在线」，用户无法回答「我那台 Mac 现在通不通」。

用户视角评审同时指出总览磁贴（17）、sources（25）、设备列表（25，含撤销）三个口径不一致，且撤销项在总览里被静默剔除。

## 实现

不新增心跳写入、不新增存储或迁移。

- **派生时间**：`last_accepted_times(console, scope_id)` 复用仪表盘趋势已经在用的同一对数据——`cmh_event_receipts` 与该请求在写队列 `memory_write_jobs` 中的 `created_at`。只统计当前项目、已排除被抑制的记录。超时或读取失败返回空表，绝不填 0。
- **接口**：`/hub/v1/console/summary` 的每个 source 增加可选 `last_accepted_at`（Unix 秒）。缺失或无法证明时字段为 `null`／缺省。
- **界面**：设备行显示三态——`暂时无法确认` / `尚无记录` / 实际时间（本地时区），并固定附注「（已接纳上传的时间，不代表设备当前在线）」。summary 读取失败只隐藏时间，不影响设备与来源列表。

显式没有做「在线」状态：ADR-0004 决定普通请求不写 last-seen，凭现有数据无法区分「空闲在线」与「关机」。文案按可证明的事实书写，不发明 presence。

## 验证

- 后端：`test_console_read.py` 增加断言——`last_accepted_at` 只能为 `null` 或合法的过去整数；`tests/test_console_read.py`、`test_console_dashboard.py`、`test_console_qa.py`、`test_remote_api.py` 合计 **39 通过**。
- 前端：`hub-api.test.ts` 增加三项——整数可传递、`null` 与缺省都保留为「无值」、非整数被拒绝。`hub-api`、`HubConnections`、`HubContext`、`HubConsole` 合计 **64 通过**；`tsc -b` 通过。
- 47 个补丁（含本补丁）从干净检出准备通过；本补丁叠加在 0048 之上应用后，4 个文件与开发副本逐字节一致。

## 未覆盖

- 待传条数（本机队列）与失败原因（永久错误码）：本机队列在设备本地，NAS 侧不可见；需要客户端在状态查询时上报，属于新协议，本次不做。
- 总览三处设备计数口径统一：服务端仍分别按「有效授权来源」与「设备列表」返回；界面标签与撤销项展示留待后续单独处理。
