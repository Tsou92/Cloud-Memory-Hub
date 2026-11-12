# Cloud Memory Hub：DeepSeek Harness 采集插件

DSH 没有 shell hook。本插件监听 DSH 的 `agent/pre-step` 和 `agent/turn-stopping`，把每轮的用户文字和最终回答交给已安装的 `cloud-memory-agent ... dsh-hook`，与 Claude Code／Codex 的 hook 走同一条排队、过滤、上传和召回路径。设计见 [ADR-0072](../../docs/adr/0072-deepseek-harness-capture.md)，安装步骤见[设备与 Agent 自助接入](../../docs/client-installation.md)。

插件不含凭证、不读会话文件、不直接联网；只有 `~/.cloud-memory-hub/dsh-capture.json` 中精确登记的工作目录才会采集。测试：`node --test test/plugin.test.js`。
