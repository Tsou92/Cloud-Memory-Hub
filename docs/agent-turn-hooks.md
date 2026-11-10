# Claude 当前轮自动共享

此入口在已配对的 MCP／CLI 之上增加 Claude 当前轮的自动采集和最近原文召回。先按 [Agent 接入说明](agent-access.md)准备私有配置与本地队列；实现依据见 [ADR-0050](adr/0050-mvp-visible-turn-hooks.md)。不需要启用 NAS 模型，不扫描历史会话文件。已在两台 Mac 的指定项目安装并核对真实 CLI 的设置加载，具体版本与证据见[验收记录](reviews/2026-11-10-visible-turn-mvp.md)。

## 工作方式

- 提交消息时，保存本轮用户文字，并重新读取当前项目最近接纳的原文作为参考上下文。
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

服务或配置出错不会阻止正常对话。状态区分采集、投递与召回，正文不会进入诊断输出；`queued` 不代表其他设备已经能读取。读取失败会明确提示本轮未取得共享上下文，不回填旧结果，不扩大权限。最近记录受条数和正文预算限制；完整任务接续继续使用 `handoff_load`。

稳定原生 session 与 prompt_id 用于重放去重，缺失这些字段时不伪造成功。该入口不读取过去会话，不提供离线读取缓存，也不恢复其他 Agent 的隐藏上下文。宿主版本、真实模型和两机链路的实测范围应以本次验收记录为准。
