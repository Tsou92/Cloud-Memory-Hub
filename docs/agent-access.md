# 通用 Agent 记忆与任务接续

这套入口面向跨设备共享记忆和任务交接：Agent 通过同一组 MCP 工具或 CLI 读写 NAS 上的 Hub，Skill 约定何时调用。它复用现有私有连接文件、项目授权、凭证过滤、LocalOutbox、上传和授权读取接口，不新增记忆存储。

本页对应 FR-01、FR-02、FR-03、FR-05、FR-07、FR-12。可安装的 Skill 位于 [cloud-memory-hub/SKILL.md](../packages/agent-skill/cloud-memory-hub/SKILL.md)。当前处于 G2：MCP／CLI／Skill 已实现，MacBook 与 Mac mini 已通过 NAS 的 Tailscale HTTPS 入口完成真实两机 MCP 验收 **7/7**；此前同镜像本地和 NAS 安装探针各 6/6。两台 Mac 的日常 Claude／Codex MCP 与 Skill 均已安装并通过配置保留核验；真实 Claude 模型读写、同会话更新及原生 Skill 读取已有新证据，详见[自动共享记录](reviews/2026-11-10-visible-turn-mvp.md)。Codex 桌面原生调用仍未验证，前序范围见[两机记录](reviews/2026-11-10-two-host-mvp.md)与[安装记录](reviews/2026-11-10-agent-access-mvp.md)。

## Mac 客户端安装

这部分安装连接 NAS 的本机 MCP／CLI，不需要在 Mac 启动 Docker 服务。先安装 Git 和 **Python 3.12**，将本仓库放在之后保持不变的目录；网络须可访问上游 GitHub 仓库和锁定的 Python 依赖。以下命令在仓库根目录执行，首次安装任一步失败都先处理错误，不跳过上游校验。

1. 进入源码目录，检查工具。产品运行环境要求 Python `>=3.12,<3.13`，不能用根工程允许的其他版本代替。

   ```sh
   cd "/path/to/Memory-Hub"
   git --version
   python3.12 --version
   ```

2. 在本仓库的私有工具环境中安装固定版本 uv，不修改系统 Python 包。后续使用此处的 uv 路径。

   ```sh
   python3.12 -m venv .local/install-tools
   .local/install-tools/bin/python -m pip install uv==0.12.5
   ```

3. 准备固定上游与已审核补丁。该命令按 manifest 校验提交、补丁摘要和实际源码；不自动覆盖已有修改。

   ```sh
   python3.12 -m scripts.prepare_upstream
   ```

4. 安装 **apps/hub** 的锁定运行依赖和命令入口。根目录 `uv sync` 只安装工程检查工具，不会生成 MCP 命令。

   ```sh
   .local/install-tools/bin/uv sync --locked --no-dev --python 3.12 --directory apps/hub
   ```

5. 检查命令已生成。后续本文的简写命令可在激活后的当前终端运行；MCP 客户端仍须配置绝对路径。

   ```sh
   source apps/hub/.venv/bin/activate
   cloud-memory-mcp --help
   cloud-memory-agent --help
   ```

6. 按下节准备本机独立的私有配置，仅首次初始化队列；随后将 MCP 配置中的 `command` 设置为 `/path/to/Memory-Hub/apps/hub/.venv/bin/cloud-memory-mcp`，参数仍指向该私有配置。

   ```sh
   cloud-memory-agent --config-file /path/to/private/agent.json init
   cloud-memory-agent --config-file /path/to/private/agent.json status
   ```

本地安装复用 `.local/upstream/hy-memory` 的 editable 源码依赖，须保留仓库、上游和虚拟环境的位置；移动后应重新安装并更新 MCP 路径。另一台 Mac 从源码重复安装，不复制这台机器的虚拟环境。以上不会安装 Skill、修改日常 Agent 配置或启动 NAS 模型。

## 准备连接

管理员先创建项目，为每台设备签发独立设备身份，再注册来源并授予所需权限：

| 功能 | 项目权限 |
| --- | --- |
| 保存记忆或交接 | `events:write`，且来源属于该设备与项目 |
| 检索提炼记忆 | `memory:read` |
| 精确读取交接原文 | `history:read` |

两台设备指向同一 Hub 的同一个项目 `scope_id`，各用自己的设备凭证和 `source_id`。`adapter_id` 是可扩展的来源标识，不限定 Agent 品牌；不同来源是否读写同一项目由服务端授权决定。来源注册见[远程接纳接口](remote-ingest.md)。

私有 `connection.json` 沿用[连接组件配置](local-connector.md)，包含服务器地址、实例、设备与凭证。文件放在源码与 Skill 包之外，权限 0600、私有目录 0700；不将凭证贴进聊天、MCP 参数或 Git。非回环连接使用 HTTPS。每台设备保留自己的本地状态目录，不能通过复制另一台设备的队列来完成配对。

通用 Agent 配置固定 `connection_file`、`state_dir`、`scope_id` 和 `source_id`，由工具读取；模型调用不传入身份或任意项目范围。项目切换使用另一份已授权配置；需要全局偏好时显式绑定获准的 `global` 范围，不自动混入项目读取。原生历史采集的 `source_root`、`workspace` 属于另一个[来源采集入口](source-capture.md)，使用此 Skill 不需要扫描 Agent 历史目录。

在私有目录创建 `agent.json`（0600），路径字段使用绝对路径，UUID 使用服务器登记的实际值：

```json
{
  "schema_version": 1,
  "connection_file": "/path/to/private/connection.json",
  "state_dir": "/path/to/private/agent-state",
  "scope_id": "<项目 UUID>",
  "source_id": "<来源 UUID>"
}
```

可选 `known_credentials_file` 指向已有格式的私有凭证字符串数组文件，用于本机额外过滤，不是认证配置。初次使用执行 `cloud-memory-agent --config-file /path/to/private/agent.json init`，已有队列不重复初始化。运行环境按上方 Mac 客户端安装步骤准备，本配置不安装系统后台服务。

## MCP 与 Skill

Claude 可另行启用[当前轮自动共享](agent-turn-hooks.md)，自动保存当前用户文字和最终回复，并在下一轮读取最近原文。该入口须显式绑定工作目录，和 MCP／Skill 安装分别配置；无须先开启 NAS 模型提炼。

支持 MCP 的客户端可连接本地 stdio 服务：`cloud-memory-mcp --config-file /path/to/private/agent.json`。工具读取固定配置，不从调用参数接收设备凭证或任意项目范围。客户端可能给工具名添加服务器前缀：

接受 `mcpServers` JSON 的客户端可按以下示例添加一项。替换为已安装命令和私有配置的绝对路径，再合并到客户端现有配置；其他客户端按其配置格式填写同样的 command／args：

```json
{
  "mcpServers": {
    "cloud-memory-hub": {
      "command": "/path/to/Memory-Hub/apps/hub/.venv/bin/cloud-memory-mcp",
      "args": ["--config-file", "/path/to/private/agent.json"]
    }
  }
}
```

这段 JSON 不安装 Python 运行时、Hub 客户端或 Skill，也不会自动修改已有客户端。先准备运行环境、私有配置并执行一次 `init`；不能直接覆盖整个 MCP 配置文件。示例中没有设备凭证，凭证仍只在私有连接文件中。

| 动作 | 行为 |
| --- | --- |
| `memory_status` | 纯本机队列状态，不是网络连通检查或提炼完成证明 |
| `memory_recent` | 可选 `limit`、`offset`，读取本项目最近接纳的原文，无需等待模型提炼 |
| `memory_search` | `query`、可选 `limit`，在配置的项目内检索相关记忆 |
| `memory_remember` | `text`，过滤并保存新事实、决定或关键进度，沿已有队列上传 |
| `handoff_save` | 保存结构清楚的任务交接，成功接纳后给出精确交接 ID |
| `handoff_load` | 按 ID 读取同项目已接纳的交接正文 |
| `memory_sync` | 显式补传一批，最多 32 条事件 |

先在隔离客户端配置中接入并验证，再自行选择日常客户端的接入位置。各客户端 MCP 配置格式不同，本项目不覆盖其已有设置。工具可单独使用；Skill 补充“会话开始读、关键进度写、切换前交接、下一轮重新读”的工作约定。

要安装 Skill，将整个 `packages/agent-skill/cloud-memory-hub` 目录复制到目标 Agent **已配置的 Skill 目录**。目录内只含通用说明，不含凭证或个人路径。目标已经存在同名 Skill 时先比较内容，不直接覆盖。安装后确认该客户端实际发现 `cloud-memory-hub`，并能看到已配置的七个工具；不支持 Skill 的客户端仍可显式调用 MCP。本文操作说明不会自动修改任何日常配置。

需要 CLI 备用入口时，在已安装的 Skill 内生成私有 `references/local-connection.md`，只记录该机器已安装 CLI 的绝对路径、已配对 Agent 配置文件的绝对路径及 JSON stdin 用法。该文件不含连接凭证，属于本机配置，不进入 Git 或通用 Skill 发布包。这样尚未加载 MCP 的对话也能按明确绑定使用 CLI，不必搜索其他 Agent 配置。

## CLI 备用入口

本地队列 schema 1 升级到 schema 2 时，由管理员先停止使用该队列的旧 MCP／CLI／hook，准备私有备份目录，再显式执行以下命令；不会发送网络请求。随后使用新版本重连 MCP。普通 `init` 或启动不能代替升级，也不能通过恢复旧备份直接覆盖升级后产生的新数据。完整兼容边界见 [ADR-0055](adr/0055-terminal-upload-rejections.md)。

```sh
cloud-memory-agent --config-file /path/to/private/agent.json \
  upgrade-outbox --backup-file /path/to/private/backups/outbox-before-v2.sqlite3
```

具备终端调用能力的 Agent 使用 `cloud-memory-agent --config-file /path/to/private/agent.json <command>`。`init`、`status`、`sync` 不需要正文；`recent`、`search`、`remember`、`handoff-save`、`handoff-load` 接收标准输入 JSON，不把连接凭证或业务正文放到命令行参数。首次配置显式调用 `init`；正常 MCP 启动不会重新创建缺失或损坏的本地状态。

以下为合成内容示例，替换私有配置路径后调用；示例请求 ID 每次新保存应换成新 UUID v4，重试原保存则保持它与正文不变：

```sh
cloud-memory-agent --config-file /path/to/private/agent.json status
cloud-memory-agent --config-file /path/to/private/agent.json recent <<'JSON'
{"limit":5,"offset":0}
JSON
cloud-memory-agent --config-file /path/to/private/agent.json search <<'JSON'
{"query":"当前项目已定方案和下一步","limit":5}
JSON
cloud-memory-agent --config-file /path/to/private/agent.json handoff-save <<'JSON'
{"goal":"完成合成演示","state":"接口测试已通过，尚未执行两机验证","decisions":["沿用现有上传队列"],"next_steps":["在第二台设备按交接 ID 读取"],"blockers":[],"request_id":"11111111-1111-4111-8111-111111111111"}
JSON
```

在 B 端使用自己的私有配置执行 `handoff-load`，标准输入为 `{"handoff_id":"cmhe:<实际回执 UUID>"}`。`remember` 的输入为 `{"text":"需要共享的已确认进展"}`，非空正文最多 16 KiB；两种保存均可显式指定 `request_id`、`session_id`（UUID v4）。省略请求 ID 会新建事件；省略 session 时 session 等于请求 ID。可靠重试应从第一次调用起保留这两个值，或用返回的 `event_id` 作为原请求 ID。

底层现有 `cloud-memory-connector` 仍可使用：`enqueue` 本机入队、`send` 单次补传、`watch` 持续补传，`memory-search` 与 `history-get` 做授权读取；具体参数见[连接组件](local-connector.md)与[读取接口](remote-read.md)。不要把 `status` 当作后台同步进程。

## 跨设备接续

1. A 端会话开始时查询状态，先读本项目最近原文，再检索相关记忆；有交接 ID 时读取完整交接。
2. 产生有效进展后保存决定、验证结果和下一步；切换前另存完整交接，包含目标、限制、已完成内容、阻碍和继续步骤。
3. 只有服务端接纳后才把返回的 `handoff_id` 交给 B 端。ID 形如 `cmhe:<回执 UUID>`，应原样传递，它本身不授予读取权限。
4. B 端使用自己的设备授权和相同项目配置，按 ID 读取已过滤的原文并继续。它无需等待模型提炼，也无需恢复 A 端原生 session。
5. B 端同一个会话的后续轮次先读 `recent`，再 `search`；Skill 要求主动这样做，但模型是否调用必须由实际宿主验证。

交接字段为 `goal`、`state`（必填，各不超过 4096 字符）及 `decisions`、`next_steps`、`blockers`（可选数组，各最多 8 条，每条最多 1024 字符），整体最多 32 KiB。内容分清事实和计划；文件使用仓库相对路径、分支或提交来定位。Hub 共享的是上下文，源码和附件原文件仍通过已有文件同步方式获取。

`queued` 是本地待传，`handoff_id` 为空；`accepted` 是 NAS 已保存，原文可按授权读取；`rejected` 是明确拒绝且本地保留，无接纳回执或交接 ID，不能说已经共享。明确的内容抑制拒绝按原因计数，并允许后续合法项继续；未知冲突与权限错误仍停止。`processing_state=not_checked` 不代表提炼完成。`handoff_load` 在总预算内取得所有正文页并验证归档内容，不把第一页当完整交接；返回的 `content_trust=untrusted_history` 要求 Agent 把内容当历史数据，不能当作新的系统指令或授权。

Agent 可以用 `remember` 保存自己已总结的项目记忆，另一端立即通过 `recent` 读取已接纳原文，不以 NAS 模型提炼为前置。`recent` 沿用历史列表的分页和正文分块标识，不是全量快照，也不是语义搜索；`search` 用于后续已提炼记忆。读取最近原文同样需要 `history:read`。

保存失败或回执不确定时，保留本地状态和请求身份。待传可显式 `sync`，然后用原 `request_id`、相同 session 和正文重放保存，取得已接纳状态和交接 ID；不靠换 ID 消除错误。连接拒绝、项目越权或服务故障也不能显示成“无记忆”。

## 最小验收与边界

使用合成项目、合成正文和独立测试配置逐项验证：

| 场景 | 必须观察到的结果 |
| --- | --- |
| 两个独立设备／来源，同一项目 | A 保存交接，B 按返回 ID 读回其目标、进度和下一步 |
| 已打开的 B 会话 | 下一轮重新调用工具后能看到 A 的新内容，无需重开 |
| 隔离项目 | 同一个 ID 在无相应项目授权的设备上读取失败 |
| 断网与恢复 | 待传和已接纳明确区分，重试不重复创建同一事件 |
| 凭证过滤 | 合成凭证在出站前被排除，交接正文其他内容仍能读回 |
| 记忆检索 | 处理完成的合成记忆能在另一端检索；不能用原文读取代替这一项 |

Skill 和 MCP 提供统一接入方式，不保证各客户端每轮自动调用。此入口也不提供离线读取缓存、全量历史导入或原生会话状态迁移。当前待实现／待验收项继续保留在[需求说明](requirements.md)和[里程碑](roadmap.md)，不会因 MVP 接入而删除。
