# ADR-0072：DeepSeek Harness 自动采集

日期：2026-11-11。关联 FR-01、FR-02、FR-03、FR-05、FR-07。

## 背景

Claude Code 和 Codex 通过 shell hook（`UserPromptSubmit`／`Stop`）调用 `cloud-memory-agent`，每轮自动排队、上传可见问答并取回召回上下文。DeepSeek Harness（DSH）只通过 Cordis Profile 接入远程 MCP；它没有 shell hook，因此只能在模型主动调用 `memory_remember` 时写入，无法实时采集。上游 MemHub 的 DSH 适配同样只有 MCP 加 Core 扫描会话目录，没有采集插件。

## 决定

DSH 自带插件事件 `agent/pre-step`（waterfall，可追加本步消息）和 `agent/turn-stopping`（回合结束前）。新增 `packages/dsh-plugin`，把这两个事件翻译成与 Claude／Codex 相同的 hook JSON，交给同一个 `cloud-memory-agent ... dsh-hook --workspace <项目>` 命令。队列、凭证过滤、稳定身份、上传与召回全部复用现有 `agent_hook`，插件不持有凭证、不读写会话文件、不直接访问网络。

- 采集来源 `adapter_id` 为 `deepseek-harness`，与上游 `one_memory` 的来源 ID 一致；回合标识字段沿用 Codex 的 `turn_id`。
- 用户消息使用 DSH 消息 ID 作为 `turn_id`；回答使用 `turn:<n>:assistant`。会话标识为 DSH session ID，经现有 `_identity` 绑定设备、项目与来源后生成。
- 只转发 `source.kind == "user"` 的文字块和本回合最终文字回答；推理、工具调用、子代理与插件注入的消息不采集。
- `pre-step` 等待 hook 返回，并把召回上下文作为 `plugin:cloud-memory-hub-capture` 消息追加到本步；`turn-stopping` 不等待上传，不延长回合。
- 绑定沿用显式工作目录原则：`cloud-memory-setup automation --agent dsh --workspace <项目>` 在 `~/.cloud-memory-hub/dsh-capture.json` 记录精确目录与命令。会话 `cwd` 不等于绑定目录时不采集，也不按目录名猜测项目。该文件不含凭证。

## 兼容性

不改协议、数据库或服务端授权；服务端 `adapter_id` 本就接受任意合法标识。旧 Hub 可直接接纳。未安装插件的 DSH 行为不变。插件需在 Profile 中显式添加（`dsh plugin --profile <name> add <包路径>`），安装器不自动修改日常 Profile。

## 未验证与限制

插件与 hook 适配已有单元测试和本机实测；两机、长时间运行和 DSH 版本升级后的事件契约仍需另行验证。DSH 会话 fork 后的重复回合依靠稳定身份去重，没有专门测试。
