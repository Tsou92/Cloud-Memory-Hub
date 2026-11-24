# ADR-0078：T3 Code（Antigravity）原生自动采集、召回与 MCP 接入

日期：2026-11-24。关联 FR-01、FR-02、FR-03、FR-05、FR-07、FR-12、ENG-01。

## 背景

T3 Code 是多 Provider 宿主，可按线程调度 `Claude`（`claudeAgent`）、`Codex`（`codex`）与 `Antigravity`（`antigravity`）。其中：

- 使用 Claude 或 Codex 线程时，沿用现有 `claude` / `codex` 配置与项目钩子，仍需分别实测 T3 Code 宿主透传行为，并注意 worktree 临时路径受精确 `workspace` 绑定限制。
- 使用 Antigravity 线程（含独立 Antigravity 2.0 / CLI）时，没有沿用 `.claude/settings.local.json` 或 `.codex/hooks.json`；其配置面为全局 `~/.gemini/config/mcp_config.json`、`~/.gemini/config/skills/` 与项目级 `<workspace>/.agents/hooks.json`。

## 上游复用调查与差异证据

检查固定上游 `b7d9f0e` 的 `one_memory/live_capture.py` 与 `one_memory/imports/sources/`：

- 现有可用能力直接复用：
  - `one_memory.live_capture._user_text` 与 `_plain`（宿主信封解包、注入上下文去重与空白规范化）。
  - `cloud_memory_hub.agent_hook` 的 `process_message`（入队、`_deliver`、`_recall` 并行 `session-briefs`/`memory-search`、8 秒网络总期限、本地回执复核与非阻断失败处理）及 `_identity` / `_identifier`。
  - `cloud_memory_hub.claude_hook.failure` 的拒绝与不可用提示文本策略。
  - `cloud_memory_hub.client_setup` 的显式 `--workspace` 校验、`Installation` 原子提交、冲突拒绝与 macOS `LaunchAgent` 后台补传。
  - `cloud_memory_hub.onboarding.connect` 的 `Remote`、一次性邀请领取、远程权限与 MCP 工具校验。
- 不能直接复用 `claude-hook` / `codex-hook` 与上游 `live_capture._messages` 的具体证据：
  1. `live_capture.py` 的 `CAPTURE_SOURCES`、`AGENT_ACCESS` 及 `_USER_FIELDS` / `_ASSISTANT_FIELDS` 不包含 `antigravity`，也不识别 Antigravity 的 camelCase 载荷（`hookEventName`、`conversationId`、`lastUserInput`、`initialNumSteps`、`transcriptPath`）和 `PreInvocation` 返回合同（`{"injectSteps": [{"systemMessage": {"systemMessage": "..."}}], "terminationBehavior": ""}`）。
  2. Antigravity `.agents/hooks.json` 采用按钩子名称映射的扁平事件数组（`{"cloud-memory-hub": {"PreInvocation": [...], "Stop": [...]}}`），且宿主执行命令时将进程工作目录设在 `hooks.json` 所在目录（`<workspace>/.agents`），而非 `payload.cwd`（`<workspace>`）。
  3. Antigravity 在同一用户轮次调用多个工具时会多次触发 `PreInvocation`，且 `Stop` 钩子 stdin 不内联最终回复文本。
  4. Antigravity `mcp_config.json` 文档仅声明 Stdio（`command`/`args`/`env`）与无自定义 `headers` 字段的 Streamable HTTP（`url`/`serverUrl`），而 Hub 远程 MCP（`/hub/v1/mcp/{source_id}`）强制校验 `Authorization: Bearer`。

## 决定

新增 `antigravity` 适配（CLI 接受 `--agent t3-code` 别名并规范化为 `antigravity`）：

1. **Hook 命令（`cloud-memory-agent ... antigravity-hook --workspace <项目>`）**：
   - 新增 [antigravity_hook.py](../../apps/hub/src/cloud_memory_hub/antigravity_hook.py)，仅处理 `PreInvocation` 与 `Stop`；忽略其他事件且不打开私有配置；`cursor_version` 出现时忽略。
   - 工作区校验要求 `payload.cwd == workspace`、非空 `workspacePaths` 包含 `workspace`，且进程实际工作目录 `Path.cwd().resolve()` 严格属于 `{workspace, workspace / ".agents"}`，阻止跨项目、符号链接或伪造 `cwd` 触发采集。
   - `PreInvocation`：提取可见 `lastUserInput`，若 `transcriptPath` 已在当前 `USER_INPUT` 后出现 `PLANNER_RESPONSE`，或通过 `LocalOutbox.event_state` 命中同一 `(conversationId, invocation:<initialNumSteps>)` 的同摘要事件，则判为单轮工具循环续步并静默返回 `{}`，不重复入队或注入；摘要冲突则返回 `request_identity_conflict`；首次触发则入队、尝试发送并注入 `injectSteps.systemMessage`。
   - `Stop`：遇到 `USER_CANCELED` 或 `ERROR` 不采集；通过 `os.O_NOFOLLOW` 与同用户/单硬链/64 MiB 上限安全读取 `transcriptPath`，仅从最后一个 `USER_INPUT` 之后取最后一个非工具调用的已完成 `PLANNER_RESPONSE`；若该记录 `truncated_fields` 含 `"content"`，按同一 `step_index` 从同目录 `transcript_full.jsonl` 读取完整文本，过滤并入队为 `step:<step_index>`，返回 `{}` 不阻塞结束。
2. **项目自动化与 MCP 安装**：
   - `client_setup.py` / `setup_cli.py` 支持 `--agent antigravity`（别名 `t3-code`），在 `<workspace>/.agents/hooks.json` 合并 `"cloud-memory-hub"` 钩子并保留其他既有钩子；`mcp-config --format antigravity` 输出无凭证 Stdio 配置。
   - `onboarding/connect.py` 支持 `--agent antigravity` / `t3-code`：写入 `~/.gemini/config/mcp_config.json` 与 `~/.gemini/config/skills/<name>/SKILL.md`；并在私有状态目录保存经校验的 Stdio 桥接副本 `mcp-bridge.py`（`0600`），通过 `connect.py mcp-stdio --state-dir <私有目录>` 读取 `0600` `pending.json` 中的设备凭证与已验证网络策略转发到 `/hub/v1/mcp/{source_id}`，保证 `mcp_config.json` 不含长期凭证且跳过临时 `/tmp` Python 路径。
   - `onboarding_api.py`、`install.sh`、`install.ps1`、`scripts/install_hub.py` 与前端控制台 `hub-invitation.ts` 同步增加 `T3 Code (Antigravity)` 选项，且不再展示仅适用于 Codex 的信任提示。

## 兼容性与限制

- 不修改 Hub 服务端数据库、事件协议、鉴权或 `LocalOutbox` schema（`OUTBOX_VERSION` 保持 `2`，仅新增只读 `event_state` 查询）。
- **历史导入与增量采集（`source_capture.py`）暂不纳入**：Antigravity `transcript.jsonl` 记录不含 `cwd` / `workspace` 字段，在无可靠项目归属证明前不猜测路径导入历史目录。
- **worktree 限制**：T3 Code 使用独立 git worktree 时工作路径不同于主仓库，受精确 `workspace` 绑定约束，自动钩子仅在显式绑定的目录触发；其他目录仍可通过全局 MCP 与 Skill 显式读写。
- **未验证边界**：本轮验证覆盖隔离单元测试、合成 `transcript.jsonl`/`transcript_full.jsonl`、真实 Hub HTTP / `LocalOutbox` 集成与标准库安装器测试；真实 T3 Code 客户端多轮会话、Mac arm64 客户端完整打包与控制台界面端到端仍需另行实测后方可记为宿主通过。
