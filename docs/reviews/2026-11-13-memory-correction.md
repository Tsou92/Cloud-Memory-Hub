# 记忆纠正

日期：2026-11-13。关联 FR-06、FR-07、FR-12。补丁 `0058-hub-memory-correction.patch`。

## 为什么不能直接用 SDK

上游的 `correct` 走 `/api/v1/memories/{id}/correct`，那是本机 local 命名空间，不适用
Hub 的项目作用域。SDK 底层的 `update(memory_id, content)` **不做作用域校验**——直接
暴露会让一次请求改到别的项目的记录，等于把项目隔离降级成参数。

## 实现

- **新接口** `POST /hub/v1/console/correct`（`console_correct.py`）。流程是
  先 `get_sdk_memory`、要求 `user_id == cmh:{instance}:{scope}`，不匹配时与不存在
  一样返回 404（不泄漏「存在但无权」），然后 `bridge.update`，最后**重新读取**
  并返回服务端实际保存的内容，而不是回显请求体。
- 请求体严格校验：`scope_id` 必须是项目 UUID 或 `global`，`content` 1–16384 字符且
  非空。
- **界面**：`SdkMemoryLibrary` 新增可选 `correct`；只有控制台传入时才在详情里渲染
  编辑框，本地 SDK 路径行为不变。保存后回到列表并重新读取。**不提供删除按钮**——
  删除仍必须走治理的忘记／彻底删除。
- 修正 `more()` 未同步 `draft` 的问题：继续读取的追加内容现在会同步进编辑框。

## 验证

- `test_console_correct.py` 5 项，重点是隔离：
  - 本作用域记录可改，写入内容正确；
  - **别的项目记录返回 404 且 `updates == []`**（跨项目零写入）；
  - 别的 Hub 实例同样拒绝；
  - 记录不存在／SDK 读失败时不写入；
  - 空白内容与超长内容被拒。
- `HubConsole.test.tsx` 24 项通过，含新增两项：可纠正但不出现本地删除；保存请求
  携带准确的 `scope_id`／`record_id`／`content`，且完成后回到列表。
- `SdkMemoryLibrary` 的「继续读取」断言改为读取 textarea 值，覆盖新行为。
- 新增与受影响后端测试 60 项通过；`ruff check`／`format` 干净；56 个补丁从干净检出
  准备通过。

## 未覆盖

- 纠正历史（谁在何时改了什么）尚未展示；当前只能看到最新正文。
- 纠正不改变来源关系；仍按原 SDK 记录身份追溯。
- 批量纠正。
