# Mac 自助接入

本页说明新的安装包和网页配对流程。当前为发布候选，已在两台 Apple Silicon Mac 的全新运行目录完成安装、双向原文与交接、单端撤销验收；首次网页配对整体流程及其他架构仍需另验，见[客户端实测记录](reviews/2026-11-10-self-service-clients.md)。公开下载及实际支持的版本与架构以发行说明为准。原有源码安装保留在 [Agent 接入说明](agent-access.md)。

每台 Mac 安装一个客户端版本，为同一 Hub 项目分别创建独立设备。Mac 不需要运行 Docker 或本地模型；需已安装本机架构的 Python 3.12，并能通过 HTTPS 访问 NAS。首次安装还需联网下载锁定的 Python 依赖。

先分清三个名字：**项目**是要共享的事情，例如“工作计划”；**设备**是每一台 Mac；**AI 应用（Agent）**是你使用的 Claude Code、Codex 等工具。两台 Mac 选择同一个项目，但分别创建设备、下载各自的配对文件。

上手顺序是：安装 Mac 连接组件 → 导入网页下载的配对文件 → 连接 AI 应用 → 保存并读回一句测试内容。网页显示“配对文件已准备”只完成了生成连接文件这一步，还不代表应用已连通。

## 安装与配对

1. 核对发行包摘要，解压对应 Mac 架构的安装包，在解压目录执行 `sh install.sh`。不要使用 sudo。安装器打印客户端命令目录；默认位于当前用户的 `Library/Application Support/CloudMemoryHub/client-releases`，已有版本不会被覆盖。
2. 登录 Hub 网页，创建或选择项目，进入设备接入页，为这台 Mac 创建一个易区分的设备名称并下载配对文件。另一台 Mac 使用同一个项目、另一个设备。
3. 用安装器打印目录中的命令导入文件：

   ```sh
   "/path/to/installed/bin/cloud-memory-setup" import --pairing-file "/path/to/downloaded-pairing.json"
   ```

   上面是路径示例，不能原样复制执行。第一段带引号的路径要换成安装器实际显示的 `cloud-memory-setup` 完整路径，第二段换成刚下载的 JSON 文件路径；保留引号，路径中的空格才不会被拆开。安装器不会把命令加入系统 PATH，因此不要只输入 `cloud-memory-setup`。成功返回 `config_file`，请保留这个配置文件路径，后续所有命令使用它。导入会真实检查连接、项目授权和来源，创建独立私有配置与队列；无需再次运行 `agent init`。相同文件可重试，不会清空队列。不同来源或轮换后的凭证不会静默覆盖已有绑定。

配对文件包含持续有效的设备凭证，请私有保存，接入成功后可自行删除下载副本。不要发到聊天、提交 Git 或放入共享目录。页面关闭后不能再次显示旧凭证；重新签发会使该设备旧连接失效。发现创建结果未知时，刷新并选择已有设备／来源后继续，避免重复创建。

## 连接 Agent

安装组件和导入文件后，还要把连接添加到你使用的 AI 应用。MCP 是应用读写共享记忆的连接方式；Skill 是提醒应用何时调用记忆的说明。请保留应用已有的其他配置。

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
| `processing.not_checked` | 尚未查询事件处理进度；不加 `--check-remote` 时不会联网 |
| `processing.checked` | 已核对当前来源最近最多五条本地事件；空样本只代表没有待核对项 |
| `processing.partial/unavailable` | 部分／全部回执查询失败，已验证的其他项仍保留 |

带 `--check-remote` 时，`processing.events` 展示按本地入队顺序采样的最多五条事件。
每项区分 `local_status` 与 `remote_status`：本地待传事件也可能已有服务器回执（例如上次
响应丢失），状态检查不会修改或确认队列。明确没有回执显示 `no_receipt`；路由、权限、网络
和本地已接纳回执缺失分别报告错误，不当作尚未上传。

服务器 `processing_state` 保留 `pending`、`running`、`waiting_for_model`、
`waiting_for_permission`、`completed` 等实际任务状态。`completed` 只表示服务器任务完成，
不证明记忆提炼、检索或另一端读取已经成功。`server_enqueued_at` 为任务首次入队时间，
`processing_updated_at` 为任务最近更新时间；旧 Hub 未提供时间时为 `null`。
`latest_known_enqueued_at_in_sample` 和 `latest_known_completed_update_at_in_sample` 只汇总
这份样本中已知的时间，不是该设备全历史最后同步成功时间。`checked_at` 只是此次查询时间。

此命令只读回执元数据，不发送原文、触发上传、确认队列或建立状态缓存。
部分失败也返回非零退出码：存在可重试失败时为 `3`，其余检查失败为 `2`。

排障先运行带 `--check-remote` 的状态命令。`device_authentication_required` 检查网页设备是否被撤销或重新签发；`source_permission_required` 检查项目授权与来源；暂时网络错误检查 NAS 和 HTTPS 网络连接。不要通过删除队列或重建身份消除错误。

若服务显示 `needs_attention`、退出码为 `126`，同时远程授权检查正常，应检查本机运行目录。实际验收中，macOS 系统保护拒绝后台进程读取 Desktop 内的自定义安装目录。使用默认的 `Library/Application Support/CloudMemoryHub/client-releases` 重新安装，并按下述方式切换服务注册；原配对配置和待传队列保留。

停止服务使用 `service stop`，移除后台服务使用 `service uninstall`，参数同上；两者都不删除配对信息或待传队列。升级到新客户端版本前，用旧版本停止并卸载旧后台服务，再用新版本的命令重新安装、检查并启动；保留旧版本可供回退。不同版本的 MCP 进程也需重连后才会切换。

## 两台设备验证

在第一台 Mac 的已连接 AI 应用中发送：“请用 Cloud Memory Hub 记住：这是我的第一次共享测试。”确认应用实际调用了保存工具，结果为 `accepted`（NAS 已收到）。然后在管理网页选择相同项目，打开“历史原文”，应能找到这句话。

在第二台已连接同一项目的 Mac 上发送：“请从 Cloud Memory Hub 读取刚才的第一次共享测试内容。”确认应用调用 `memory_recent`，并读回同一句话。只显示“连接成功”或回复“我记住了”，不能代替实际读写结果。

如果“历史原文”已经收到，而“记忆库”暂时没有内容，请检查模型是否已配置并开启整理；原文接纳与提炼长期记忆是不同阶段。如果读取报错，按连接或权限错误处理，不把它当作没有数据。

交接使用 `handoff_save` 返回的完整 `handoff_id`，再由第二台 Mac 使用 `handoff_load` 读取。原文和交接不必等待模型提炼；语义搜索由 NAS 模型处理后可用。

已在本机排队的内容不等于已经共享。不要将实际 Agent 未调用工具的问题与网络问题混为一谈；当前也不提供离线记忆读取缓存。对应设计与验收边界见 [ADR-0060](adr/0060-self-service-pairing.md) 与[处理状态合同](adr/0062-client-processing-status.md)。
