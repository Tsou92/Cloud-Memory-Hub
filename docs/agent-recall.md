# Claude 每轮在线记忆召回

实验性能力；设计与复用证据见 [ADR-0010](adr/0010-agent-recall-hook.md)。这里只增加在线读取和上下文呈现，采集与上传仍由[来源采集](source-capture.md)中的两个持续进程负责。不会自动修改日常 Claude 配置。

## 配置边界

先准备[私有连接配置](local-connector.md)和[来源配置](source-capture.md)，文件权限均为 0600，使用当前账户所有的普通文件。source 的 workspace 必须等于实际项目目录的规范化绝对路径；hook 输入 cwd 和进程 cwd 均须精确匹配。运行不需要 outbox，也不会读取 hook 提供的 transcript_path。

独立设备须具备绑定项目的 memory:read；添加 --include-global 时还须具备 global 的 memory:read，用于跨项目个人偏好。任一范围无权读取时整批拒绝，不输出部分结果。单纯读取不要求 events:write 或 history:read，也不会扩大到其他项目。

隔离验证时，在专用 settings 文件中合并以下 hook；示例路径均须替换为已安装命令和私有配置的实际绝对路径，不要直接覆盖已有 hooks：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/path/to/cloud-memory-connector' claude-hook --connection-file '/path/to/private/connection.json' --source-file '/path/to/private/source.json' --include-global",
            "timeout": 12
          }
        ]
      }
    ]
  }
}
```

使用隔离的 CLAUDE_CONFIG_DIR 和 --settings 验证，先用合成项目和合成记忆。日常安装器、配置共存与撤销安装仍待完成，不能把此示例当作已完成的正式接入。

每轮会重新检索，包括同一会话重复发出的同一句问题。完整问题先过滤再截取查询；记忆按项目/global 交替选取，最多五条，正文合计最多 6000 字符，上下文最多 24KiB。输出保留记录 ID、来源引用和截断提示，历史被明确标为参考数据。记忆检索继续复用 Hy-Memory SDK 和本地 embedding。

## 状态与失败

stderr 只输出结构化状态：retrieved、empty、denied 或 unavailable；不输出查询和记忆正文。stdout 使用 Claude additionalContext 合同，错误不会阻止正常对话。请求总期限八秒，超过期限取消客户端读取；宿主 timeout 为启动和收尾预留时间。

empty 只表示这次授权查询没有命中。撤销授权、配置错误或离线时，不重新注入旧结果；已进入 Agent 会话的历史上下文无法由 Hub 撤回。当前不提供离线召回缓存，离线读取需求保持不变，将在同步快照、权限代际和删除抑制协议具备后接入。

## 已验证与剩余工作

`make check-claude-recall` 使用实际 Claude CLI、实际 Hub HTTP、SDK 和本地 embedding，验证同一进程四轮、重复问题刷新、项目/global 读取、其他项目隔离、撤销与断网，以及自动采集／投递不重复收集注入上下文。生成模型是本地替身，记忆直接写入合成 SDK 夹具；未验证从对话自动提炼、真实模型回答质量、两 Mac 或 NAS 网络时效。详见[验证记录](reviews/2026-10-25-claude-recall.md)。
