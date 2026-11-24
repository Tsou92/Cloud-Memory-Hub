# ADR-0079：T3 Code 跨 Provider 统一线程采集与历史导入（`t3-code` 来源）

日期：2026-11-24。关联 FR-01、FR-02、FR-03、FR-06、FR-07、FR-08、FR-14、FR-15、ENG-01。

## 背景

T3 Code 是多 Provider 宿主，会在同一项目下按线程切换 `codex`、`claudeAgent` 与 `antigravity`（含中转模型），且支持在独立 git worktree 目录中新建并行线程：

1. [ADR-0078](0078-t3code-antigravity-capture.md) 已提供 Antigravity 线程的逐轮 hook（`PreInvocation` + `Stop`）与 MCP 接入，但 `antigravity-hook` 仅作用于 Antigravity 驱动的精确 `--workspace` 目录，无法覆盖切换至 `codex` / `claudeAgent` 或独立 worktree 的线程，也不能导入过去已经完成的 T3 Code 历史会话。
2. 另一方面，T3 Code 将所有 Provider 与所有 worktree 线程统一持久化到宿主本地只读投影库 `statev2.sqlite`（及其 WAL 日志 `statev2.sqlite-wal`）：`projection_projects.workspace_root` 保存主项目目录，而 `orchestration_v2_projection_threads` / `orchestration_v2_projection_messages` 统一记录跨 Provider 的最终对话文本。

## 上游复用调查与差异证据

检查固定上游 `b7d9f0e` 的 `.local/upstream/hy-memory/memhub/apps/core/src/one_memory/imports/`：

- 现有可用能力直接复用：
  - `AuthorizedSourceRoot` 与 `discover_source_files`（目录权限、根目录身份 `root:v3`、禁跟随符号链接、单文件门禁）。
  - `open_sqlite_readonly`（遇到 `-wal` 时先复制至临时镜像再执行 `wal_checkpoint(TRUNCATE)` 与 `query_only = ON`，字节级不修改并存写回宿主库与 `-shm`）。
  - `parse_timestamp`、`is_host_generated_user_turn`、`strip_host_wrappers`、`strip_injected_memory`、`opaque_workspace_id`、`safe_project_label`、`source_cursor`、`touched_cursor`。
  - `media_text.py` 复用统一的 PDF 文本层与纯文本解码和摘要头格式化逻辑（抽取出 `extract_raw_document` 共享辅助函数）。
- 不能直接复用 `opencode.py`、`cursor.py` 与 `JsonlSourceAdapter` 的具体证据：
  1. 上游 `imports/sources/` 不含 `t3-code` 来源或 T3 Code `statev2.sqlite` 表结构（`projection_projects`、`orchestration_v2_projection_threads`、`orchestration_v2_projection_messages`）。
  2. T3 Code 在流式回复开始时先以 `streaming = 1` 插入行，再在结束时原行更新 `streaming = 0` 与 `updated_at`；并发多标签页下若按单调 `rowid > last_rowid` 推进游标，晚结束的流式回复会被前移的 `rowid` 永久跳过。
  3. 现有 `source_capture._capture_listing` 与 `is_unchanged` 仅检测主文件尺寸/时间；SQLite WAL 模式下，写入先进入 `statev2.sqlite-wal` 而不改动主库 `statev2.sqlite`，导致 `collect-watch` 会漏扫未 checkpoint 的新消息，且主库同尺寸 `ctime` 变化时会错误清空 SQLite 游标。
  4. `statev2.sqlite` 为跨项目单库；必须先在 bound workspace 外跳过他项目与软删除线程，避免无关项目里的截图或异常附件阻断当前项目导入。

## 决定

在 `source_capture` 中新增 `adapter_id = "t3-code"` 适配器（[t3_source.py](../../apps/hub/src/cloud_memory_hub/t3_source.py)）：

1. **统一线程归属与跨 Provider 增量采集**：
   - `source_root` 显式授权指向 T3 Code 用户数据目录（通常 `~/.t3/userdata`），只扫描 `statev2.sqlite`；支持标准 `capture-init`、`collect` 与持续增量 `collect-watch`。
   - 经 `orchestration_v2_projection_threads -> projection_projects` 解析 `workspace_root`；`thread_deleted_at IS NOT NULL`、`project_deleted_at IS NOT NULL` 或其他项目的行计入 `workspace_mismatch` 并先于媒体守卫跳过。
   - **同时支持 Local 与 Worktree 线程**：即使线程在 `payload_json.worktreePath` 使用了临时 worktree，其所属主项目仍通过 `projection_projects.workspace_root` 精确命中对应 Hub `scope_id`，并记录非路径元数据（`provider`、`worktree: true`）。
2. **流式并发安全的增量游标与 WAL 变更感知**：
   - 只扫描 `streaming = 0` 的已完成消息，按归一化 UTC 时间戳 `(updated_at, rowid)` 排序，使用 `t3v2:<rowid>:<updated_at>` 游标与 `(message_id, updated_at)` 校验锚点；若存在仍在流式输出的记录（`streaming != 0`），保留 `read_to_end = False` 以确保持续轮询直至流式结束。
   - 改写同一消息且刷新 `updated_at` 时自然触发重扫，由 `CaptureStore` 生成递增 `revision` 而不篡改历史事件。
   - [capture_store.py](../../apps/hub/src/cloud_memory_hub/capture_store.py) 在现有加密 `capture_cursors` 载荷中向后兼容保存可选 `-wal` 元数据指纹（`wal_stat`），不改动队列 schema 版本；空闲时仅 `lstat` 即可跳过重读，WAL 一旦变化立即触发增量扫描，且 SQLite `ctime` 同尺寸变化不再丢弃游标。
3. **消息角色、通知与多模态边界**：
   - 仅收集 `role in {"user", "assistant"}` 且已完成文本：`user` 消息要求 `notification IS NULL` 且 `createdBy == "user"`，由宿主/代理注入的 `role="user"` 通知消息计入 `not_written_by_person`。
   - 委托子线程（`lineage.parentThreadId`）遵循环境变量 `ONE_MEMORY_IMPORT_SUBAGENT_TRACE`；开启时保留独立 `conversation_id` 且提示行记为 `WrittenBy.AGENT`，关闭时计入 `subagent_trace_disabled`。
   - **默认遵循 ADR-0075 fail-closed**：当前项目消息若含 `attachments` 或非文本/图片类的 `context.records`，默认停止于该消息并返回 `source_media_not_supported`；开启 `media_text_extraction: true` 时支持从 `<source_root>/attachments/` 通过 `AuthorizedSourceRoot` 安全读取 `.txt`/`.md`/PDF 文本层，图片与不安全路径仍拒绝。

## 兼容性与未纳入边界

- 不修改 Hub 数据库、事件协议、`agent.json` 或 `LocalOutbox` 核心表结构。
- 本适配器与 [ADR-0078](0078-t3code-antigravity-capture.md) 互补：本适配器统一解决跨 Provider 与 worktree 的**自动采集写入与历史导入**（应分配独立的来源 UUID）；发消息前的上下文主动召回注入仍由各 Provider 原生 hook / MCP / Skill 负责。
- **未纳入与待验边界**：暂不采集 `orchestration_v2_projection_turn_items` 中的原始终端命令输出（避免壳层噪音与控制字符导致过滤中止）；暂未为 `collect-watch` 新增 macOS `LaunchAgent` 后台托管子命令；以上均基于构造合成 `statev2.sqlite` + WAL 验证，尚未对用户真实 `~/.t3/userdata/statev2.sqlite` 执行导入与多机端到端验收。
