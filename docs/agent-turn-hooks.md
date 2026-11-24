# 当前轮自动共享（Claude / Codex / T3 Code Antigravity）

此入口在已配对的 MCP／CLI 之上增加当前轮的自动采集和召回。先按 [Agent 接入说明](agent-access.md)准备私有配置与本地队列；实现依据见 [ADR-0050](adr/0050-mvp-visible-turn-hooks.md)、[相关记忆召回设计](adr/0051-mvp-relevant-recall.md)与 [T3 Code（Antigravity）适配设计](adr/0078-t3code-antigravity-capture.md)。最近原文读取不需要启用 NAS 模型；按问题检索较早记忆，需要 NAS 已生成可搜索记忆。Claude／Codex 两条路径都不扫描本机会话文件；仅最近原文的版本已在两台 Mac 的指定项目安装并核对真实 Claude CLI 的设置加载，具体版本与证据见[验收记录](reviews/2026-11-10-visible-turn-mvp.md)；新联合召回与 T3 Code 真实宿主端到端须分别实测核对。

## 工作方式

- 提交消息时，保存本轮用户文字，并行读取当前项目最近接纳的原文，以及与当前问题相关的已提炼记忆。较早决定无需仍在最近五条中；相关记忆优先，与最近原文交错呈现。
- 回答结束时，只保存最终可见回复，不读取工具输出或 transcript 文件。
- 写入先在本机持久排队，再尝试发送。离线时保留待传；后续 hook 或 `memory_sync` 可补传。安装这两个 hook 不等于安装后台常驻服务。

它保存的是可见消息，而非已确认的长期事实或已经完成模型提炼的记忆。MCP／Skill 仍可用于明确保存关键决定和跨 Agent 交接。其他 Agent 继续使用通用入口，此 Claude hook 不冒充其他宿主的自动适配。

## 显式工作目录绑定

命令参数中的工作目录必须是用户已选择的真实项目目录。hook 的 cwd 和进程实际 cwd 均须匹配；其他目录不采集，也不根据目录名自动选择项目。两台设备可以分别绑定各自的项目路径，但私有配置必须具有同一 NAS 项目的相应权限。

以下为待合并的示例，不是可覆盖现有设置的完整配置：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/path/to/cloud-memory-agent' --config-file '/path/to/private/agent.json' claude-hook --workspace '/path/to/project'",
            "timeout": 12
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/path/to/cloud-memory-agent' --config-file '/path/to/private/agent.json' claude-hook --workspace '/path/to/project'",
            "timeout": 12
          }
        ]
      }
    ]
  }
}
```

首次先在隔离合成工作区用专用 `--settings` 文件验证，再决定日常工作区范围。日常启用前备份现有设置，仅追加这两项并保留其他 hooks。撤销时只移除本项目添加的精确命令；若安装后还发生过其他配置变化，不用旧整文件覆盖新设置。此前 MCP／Skill 安装不代表这些自动采集 hooks 已启用。

## 状态与限制

服务或配置出错不会阻止正常对话。状态区分采集、投递与召回，正文不会进入诊断输出；`queued` 不代表其他设备已经能读取。仅一路临时不可用时，召回标为 `partial`，上下文明确说明缺失的读取；任何权限拒绝、无效回执或权限版本变化均拒绝整批上下文。两路失败或总期限耗尽时明确提示本轮未取得共享上下文，不回填旧结果，不扩大权限。

相关记忆与最近原文共用最多 5 条、6000 正文字符和 24 KiB 的上下文预算，截断与分页仍有提示。上传加联合读取的网络总期限仍为 8 秒。未启用 NAS 提炼或搜索为空不表示所有历史均已检索，也不能据此推断没有记忆；完整任务接续继续使用 `handoff_load`。

稳定原生 session 与 prompt_id 用于重放去重，缺失这些字段时不伪造成功。Claude／Codex 入口不读取过去会话；Antigravity（T3 Code）入口使用 `.agents/hooks.json` 的 `PreInvocation` 与 `Stop` 事件调用 `antigravity-hook`，仅在 `Stop` 阶段按 `O_NOFOLLOW` 安全读取当前轮最后一个无未决工具调用的 `PLANNER_RESPONSE`（以及同目录 `transcript_full.jsonl` 中对应 `step_index` 的被截断长文本），不扫描其他会话目录，也不提供离线读取缓存或恢复其他 Agent 的隐藏上下文。若需在 T3 Code 下跨越所有 Provider 及独立 git worktree 统一导入历史并做后台增量采集，请改用/配合 [来源采集](source-capture.md) 的 `t3-code` 适配器（[ADR-0079](adr/0079-t3code-unified-source-capture.md)）。宿主版本、真实模型和两机链路的实测范围应以实际验收记录为准。

## T3 Code（Antigravity）`.agents/hooks.json` 示例

```json
{
  "cloud-memory-hub": {
    "PreInvocation": [
      {
        "type": "command",
        "command": "'/path/to/cloud-memory-agent' --config-file '/path/to/private/agent.json' antigravity-hook --workspace '/path/to/project'",
        "timeout": 12
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "'/path/to/cloud-memory-agent' --config-file '/path/to/private/agent.json' antigravity-hook --workspace '/path/to/project'",
        "timeout": 12
      }
    ]
  }
}
```
