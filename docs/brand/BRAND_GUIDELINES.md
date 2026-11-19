# Cloud Memory Hub 品牌规范

本规范用于 GitHub、项目主页、社交卡片和技术社区配图。管理后台视觉见 [DESIGN.md](../../DESIGN.md)。

## 品牌与受众

对外统一使用 **Cloud Memory Hub**。安装包和命令保留 `cloud-memory-hub`。Hy-Memory / MemHub Core 是项目复用的上游组件，按其原名署名。核心读者是使用多个编码 Agent 或多台开发设备的开发者，以及在 NAS 或本机 Docker 中运行服务的自托管用户。

| 用途 | 中文 | English |
| --- | --- | --- |
| Tagline | 共享项目记忆，接续下一轮。 | Share project memory. Pick up next turn. |
| 一句话介绍 | Cloud Memory Hub 是可部署在 NAS 或本机 Docker 上的开源服务，让授权设备上的 Agent 共享项目记忆，并在支持的宿主中于下一轮接续会话。 | Cloud Memory Hub is an open-source, self-hosted service that shares project memory across authorized devices and agents and brings session context into the next turn on supported hosts. |

## 视觉概念

把会话接续画成项目接线图。图标由两条错位的输入线、中央接纳节点和一条输出线组成，表示多个设备和 Agent 通过同一个 Hub 读写项目上下文。宣传图用靛紫标出接纳与记忆路径，用冰蓝标出会话召回。端点、箭头与文字共同表达方向和状态。

架构图用一条实线从已接纳原文通向会话摘要，用虚线标出异步提炼路径。下一轮可同时读取摘要和已经提炼好的相关记忆。需要更多细节时，再以 `session_ref` 分页读取 Markdown。图中的箭头表示终端发起的提交或读取所产生的数据流；模式是回合触发的读取与注入。

### 色彩

| 角色 | 浅色模式 | 深色模式 | 用途 |
| --- | --- | --- | --- |
| 画布 | `#F4F3FC` | `#090B14` | 社交卡片、横幅 |
| 面板 | `#FFFFFF` | `#111628` | 架构图节点和技术标签 |
| 主文字 | `#16172C` | `#F1F3FB` | 品牌名和主标题 |
| 次文字 | `#505674` | `#A9B1CC` | 说明、设备和流程标签 |
| 主色 | `#5644CB` | `#A495FF` | 接纳、项目范围和长期记忆 |
| 强调色 | `#106E88` | `#83DDF1` | 会话摘要、下一轮召回 |
| 边框与网格 | `#D9D7EB` | `#2C324D` | 卡片边界、定位线 |

浅色用深靛紫和深青保证文字对比。深色以靛黑为底，品牌线条使用较亮靛紫；冰蓝用于接续路径。线条与文字如果同时承担信息表达，两者均需清晰可辨。

### 字体、网格与用法

- 英文标题和正文首选 **Avenir Next**，可回退到 Avenir、DejaVu Sans、Arial。品牌字标已转为轮廓。中文物料建议 **思源黑体 / Noto Sans SC**，可回退到 PingFang SC、Microsoft YaHei。
- 工具名、事件名和协议标签首选 **IBM Plex Mono**，可回退到 Menlo、DejaVu Sans Mono、等宽字体。SVG 中的普通文字可选中，字体从本机回退，无需加载网络字体。
- 使用 8 px 基础网格。大画布四周至少留 48 px，面板圆角为 12 px。技术连线采用 2 px 圆端线，主方向线为 3 px。
- 浅底使用 `logo.svg`，深底使用 `logo-dark.svg`。标志四周留至少图标边长的四分之一。组合标志宽度建议至少 200 px。方形头像与 Favicon 使用 `icon.svg`，可缩到 24 px。
- 窄屏展示大图时保留原始比例。架构图应允许打开原图查看。社交图的品牌名与主标题靠近画布中心区域，主要内容距边缘至少 64 px。

## 核心词汇

| 中文 / English | 代码术语或用法 |
| --- | --- |
| 项目记忆 / project memory | 在授权的 `scope_id` 内共享。MCP 工具 `memory_search` 查询已可检索的记忆。 |
| 当前轮采集 / turn capture | Claude Code 和 Codex 使用 `UserPromptSubmit` 与 `Stop`。DSH 插件适配 `agent/pre-step` 与 `agent/turn-stopping`。采集范围为获准的可见消息。 |
| 本地待传队列 / local outbox | `LocalOutbox` 在客户端先过滤、持久排队，再投递；网络恢复后可补传。 |
| 接纳 / accepted | Hub 通过项目与来源授权检查并返回持久回执。模型提炼可在之后完成。 |
| 会话摘要 / session brief | `session_briefs` 从已接纳原文生成有界摘录，可用于支持宿主的下一轮召回。 |
| 会话 Markdown / session Markdown | `session_markdown` 根据 `session_ref` 分页返回已授权、已采集的可见原文。 |
| 会话接续 / session handoff | 从共享项目上下文继续工作。支持的宿主可在下一轮自动召回，通用 MCP 也可显式交接。 |
| 显式交接包 / handoff package | `handoff_save` 提交目标、当前状态、决定、下一步和阻塞；接纳后用返回的 `handoff_id` 调用 `handoff_load`。 |

如使用「实时共享」或 “real-time handoff”，紧邻说明「按回合上传，Hub 接纳后可读，支持的宿主下一轮召回」。涉及延迟时注明实测环境、版本与范围。介绍「完整原文」时注明范围是已授权、已采集的可见正文。提及多 Agent 时，用「支持的宿主可启用自动采集与召回，其他 Agent 可经 MCP 显式读写」。自托管描述落在 Docker 与数据部署位置；涉及外部模型时按实际模型配置说明数据流向。

## 可直接使用的宣传文案

### GitHub

> 共享项目记忆，接续下一轮。Cloud Memory Hub 在你的 NAS 或本机 Docker 中运行。把两台设备配对到同一个项目，支持的 Agent 就能在下一轮读到相关会话摘要。需要细节时，再取已采集的 Markdown 原文。通用 MCP 支持显式读写及交接。

### X / Twitter

> Switch devices or coding agents and pick up next turn. Cloud Memory Hub shares project memory from your own NAS or Docker host. Supported hosts recall session briefs. MCP tools let you save and load handoffs explicitly.

### 技术社区

> 我们在做 Cloud Memory Hub，一个跑在 NAS 或本机 Docker 上的开源项目记忆服务。比如，在一台电脑用 Codex 定下方案，另一台电脑上的 Claude Code 可以在同项目的下一轮收到相关会话摘要。接入自动化后，可见消息经本地凭证过滤进入 `LocalOutbox`，Hub 以稳定事件身份接纳。摘要直接从已接纳原文生成，长期记忆由 MemHub Core 的队列另行提炼。其他 MCP 客户端也可使用 `handoff_save` 和 `handoff_load` 显式交接。

公开文案可附上 [GitHub 仓库](https://github.com/Tsou92/Cloud-Memory-Hub)。实现与验证范围见 [自动会话接续](../adr/0069-automatic-session-handoff.md)、[客户端接入](../client-installation.md)、[Agent 工具](../agent-access.md) 和 [数据与隐私](../data-and-privacy.md)。

## 文件与导出

| 文件 | 画布 | 用途 |
| --- | --- | --- |
| [logo.svg](../../assets/brand/logo.svg) / [logo-dark.svg](../../assets/brand/logo-dark.svg) | 420 × 88 | 透明背景组合标志 |
| [icon.svg](../../assets/brand/icon.svg) | 64 × 64 | 方形头像和 Favicon |
| [banner.svg](../../assets/brand/banner.svg) | 1280 × 400 | README 首屏横幅 |
| [architecture-handoff.svg](../../assets/brand/architecture-handoff.svg) | 1280 × 704 | 会话接续与长期记忆概念图 |
| [social-preview.svg](../../assets/brand/social-preview.svg) | 1280 × 640 | 社交卡片源文件 |
| [social-preview.png](../../assets/brand/social-preview.png) | 1280 × 640 | 可直接上传至 GitHub Social Preview |

浏览器可以直接打开 SVG。用 Inkscape 从仓库根目录导出，`--export-width` 只需指定宽度，高度自动等比计算。

```sh
inkscape assets/brand/banner.svg --export-width=2560 --export-filename=/tmp/cloud-memory-hub-banner.png
inkscape assets/brand/social-preview.svg --export-width=1280 --export-filename=/tmp/cloud-memory-hub-social.png
```

GitHub 的 Social preview 设置接收 PNG、JPG 或 GIF，建议上传 1280 × 640、体积小于 1 MB 的社交卡片 PNG。其余平台需要 2 倍图时，可将社交卡片的导出宽度设为 2560。 [GitHub Social Preview 文档](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview)
