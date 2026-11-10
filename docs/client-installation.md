# Mac 自助接入

本页说明新的安装包和网页配对流程。当前为发布候选开发，公开下载和完整新机验收尚未完成；实际支持的版本与架构以发行说明为准。原有源码安装保留在 [Agent 接入说明](agent-access.md)。

每台 Mac 安装一个客户端版本，为同一 Hub 项目分别创建独立设备。Mac 不需要运行 Docker 或本地模型；需已安装本机架构的 Python 3.12，并能通过 HTTPS 访问 NAS。首次安装还需联网下载锁定的 Python 依赖。

## 安装与配对

1. 核对发行包摘要，解压对应 Mac 架构的安装包，在解压目录执行 `sh install.sh`。不要使用 sudo。安装器打印客户端命令目录；默认位于当前用户的 `Library/Application Support/CloudMemoryHub/client-releases`，已有版本不会被覆盖。
2. 登录 Hub 网页，创建或选择项目，进入设备接入页，为这台 Mac 创建一个易区分的设备名称并下载配对文件。另一台 Mac 使用同一个项目、另一个设备。
3. 用安装器打印目录中的命令导入文件：

   ```sh
   "/path/to/installed/bin/cloud-memory-setup" import --pairing-file "/path/to/downloaded-pairing.json"
   ```

   将示例路径替换为本机实际路径。成功返回 `config_file`，后续所有命令使用它。导入会真实检查连接、项目授权和来源，创建独立私有配置与队列；无需再次运行 `agent init`。相同文件可重试，不会清空队列。不同来源或轮换后的凭证不会静默覆盖已有绑定。

配对文件包含持续有效的设备凭证，请私有保存，接入成功后可自行删除下载副本。不要发到聊天、提交 Git 或放入共享目录。页面关闭后不能再次显示旧凭证；重新签发会使该设备旧连接失效。发现创建结果未知时，刷新并选择已有设备／来源后继续，避免重复创建。

## 连接 Agent

接受 `mcpServers` JSON 的 Agent 使用：

```sh
"/path/to/installed/bin/cloud-memory-setup" mcp-config --config-file "/path/to/private/agent.json"
```

Codex 桌面所用 TOML 配置使用同一命令加 `--format codex`。将输出新增到该 Agent 的 MCP 配置中，保留其他连接；不要覆盖整个已有配置文件。输出只有命令和私有配置路径，不包含设备凭证。重新连接 MCP 或开始新会话后，检查 `memory_recent`、`memory_remember` 和 `handoff_save` 等工具是否可用。

安装目录中的 `skill/cloud-memory-hub/SKILL.md` 是公共 Skill 模板，可按 Agent 的 Skill 安装方式添加。仅使用 Skill 的终端 Agent，还需按 [CLI 备用入口](agent-access.md#cli-备用入口)绑定已安装命令和 `config_file`；没有 MCP 或明确的本机绑定时，Skill 不会自动猜测凭证。MCP 提供通用读写工具，Skill 约定调用时机；每轮自动触发由各 Agent 的实际能力决定。

## 后台补传与状态

先安装该设备的后台服务，再启动：

```sh
"/path/to/installed/bin/cloud-memory-setup" service install --config-file "/path/to/private/agent.json"
"/path/to/installed/bin/cloud-memory-setup" service start --config-file "/path/to/private/agent.json"
"/path/to/installed/bin/cloud-memory-setup" service status --config-file "/path/to/private/agent.json"
"/path/to/installed/bin/cloud-memory-setup" status --config-file "/path/to/private/agent.json" --check-remote
```

后台服务在当前 Mac 用户登录后运行，负责已入队内容的补传。暂时断网自动退避重试；凭证撤销等永久错误会停止，修复配置后显式启动。它不会扫描历史对话，也不代替 Agent 保存记忆。用户退出登录或 Mac 休眠时不能保证运行。

| 状态 | 含义 |
| --- | --- |
| `queue.queued` | 已保存在本机、待发送的条数 |
| `queue.accepted` | NAS 已持久接纳的条数，不代表全部已完成模型提炼 |
| `queue.rejected` | 明确拒绝的原因和数量，原有队列保留 |
| 服务 `installed/loaded/running` | 本机服务配置、加载与进程状态，不代表在线 |
| `remote.authorized` | 本次真实检查时，设备及当前项目／来源授权可用 |
| `remote.unavailable` | 本次检查失败，结合固定错误码处理，不能解释为没有记忆 |

排障先运行带 `--check-remote` 的状态命令。`device_authentication_required` 检查网页设备是否被撤销或重新签发；`source_permission_required` 检查项目授权与来源；暂时网络错误检查 NAS 和 HTTPS 网络连接。不要通过删除队列或重建身份消除错误。

若服务显示 `needs_attention`、退出码为 `126`，同时远程授权检查正常，应检查本机运行目录。实际验收中，macOS 系统保护拒绝后台进程读取 Desktop 内的自定义安装目录。使用默认的 `Library/Application Support/CloudMemoryHub/client-releases` 重新安装，并按下述方式切换服务注册；原配对配置和待传队列保留。

停止服务使用 `service stop`，移除后台服务使用 `service uninstall`，参数同上；两者都不删除配对信息或待传队列。升级到新客户端版本前，用旧版本停止并卸载旧后台服务，再用新版本的命令重新安装、检查并启动；保留旧版本可供回退。不同版本的 MCP 进程也需重连后才会切换。

## 两台设备验证

在 A 端让 Agent 保存一条不含敏感信息的合成进展；收到 `accepted` 后，在 B 端用 `memory_recent` 读取。交接使用 `handoff_save` 返回的完整 `handoff_id`，再由 B 端 `handoff_load` 读取。原文和交接不必等待模型提炼；语义搜索由 NAS 模型处理后可用。

已在本机排队的内容不等于已经共享。不要将实际 Agent 未调用工具的问题与网络问题混为一谈；当前也不提供离线记忆读取缓存。对应设计与验收边界见 [ADR-0060](adr/0060-self-service-pairing.md)。
