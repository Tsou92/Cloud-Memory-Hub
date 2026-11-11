# Cloud Memory Hub

**让不同设备和 AI Agent，共享项目记忆、接续任务。**

服务通过 **Docker 部署在自己的 NAS**，电脑无需安装独立 App。

运行 Hub 生成的一条命令，或把安装话术交给 Agent，即可配置 **MCP 和 Skill**。

[![最新发行版](https://img.shields.io/github/v/release/Tsou92/Cloud-Memory-Hub?style=flat-square&color=8b5cf6&logo=github)](https://github.com/Tsou92/Cloud-Memory-Hub/releases/latest)
[![Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-64748b?style=flat-square)](LICENSE)
[![Docker 自托管](https://img.shields.io/badge/self--hosted-Docker-2496ed?style=flat-square&logo=docker&logoColor=white)](docs/development-runtime.md#发行版首次运行)
[![GitHub Actions](https://img.shields.io/badge/checks-GitHub_Actions-64748b?style=flat-square&logo=githubactions&logoColor=white)](https://github.com/Tsou92/Cloud-Memory-Hub/actions/workflows/quality.yml)

![Cloud Memory Hub：NAS 上的共享记忆中枢，连接不同设备与 Agent](docs/assets/readme-overview.svg)

[产品主页](https://memoryhub.tsounas.cn) · [下载发行版](https://github.com/Tsou92/Cloud-Memory-Hub/releases/latest) · [接入指南](docs/client-installation.md) · [问题反馈](https://github.com/Tsou92/Cloud-Memory-Hub/issues)

安装前请确认发行页已有公开的正式版本；发行包更新期间，请等待新版，不使用旧归档或测试制品代替。

<a id="在-nas-安装发行版"></a>

## 🚀 三步部署

**推荐让电脑上的 Agent 帮你安装，不需要自己输入 Docker 命令。**

1. **在 NAS 开启 Docker 和 SSH。** Docker 通常在「应用中心／容器管理」中；SSH 通常在「控制面板／终端」中。记下 NAS 地址、登录用户名和 SSH 端口。当前发行镜像适用于 Intel / AMD NAS（Linux amd64）。
2. **复制下面的话给 Agent。** 使用电脑上能够执行终端命令的 Claude Code、Codex 等。替换三个括号里的信息；首次登录和必要授权按 Agent 提示在本机完成，密码不要发进聊天。
3. **打开 Agent 返回的管理页面。** 在网页中配置模型、创建项目，然后连接各台设备上的 Agent。

```text
请帮我把 Cloud Memory Hub 部署到 NAS。
NAS 地址：[填写地址]；SSH 用户名：[填写用户名]；SSH 端口：[填写端口，通常为 22]。
请先阅读 https://github.com/Tsou92/Cloud-Memory-Hub/blob/main/docs/nas-installation.md ，
按官方指南使用最新已发布版本，完成下载校验、Docker 部署、可访问的 HTTPS 入口和健康检查。
已有实例请先备份，保留原数据和配置。需要我登录或授权时，请指导我在本机完成，不要让我把密码或密钥发到聊天。
完成后给我管理页面地址，并指导我安全登录和连接 Agent。
```

[不清楚 SSH 在哪里？查看安装说明](docs/nas-installation.md) · [熟悉 Docker？查看手动安装](docs/nas-installation.md#手动安装)

## 🔌 三步使用

1. **生成连接。** 在 Hub 的「连接 Agent」中选择项目、填写设备名称并选择 Agent。每台电脑分别生成链接，共享同一个项目。
2. **安装连接。** 在目标电脑运行页面生成的一条命令，或把安装话术交给 Agent。按提示重新加载 MCP，并确认实际连接成功；详细步骤见[一次性链接接入](docs/client-installation.md#默认一次性链接接入)。
3. **开始共享。** 支持自动接续的版本在项目接入后，正常对话即可共享进展。另一端先收到会话摘要，需要细节时再读 Markdown 原文。具体版本和宿主见[接入指南](docs/client-installation.md)。

会话接续传递任务上下文。通用 MCP 也提供显式读取和交接工具；本机旧历史与其他宿主的采集范围见[接入指南](docs/client-installation.md)。

## ✅ 当前支持范围

| 能力 | 已做到什么 |
| --- | --- |
| 跨设备共享与交接 | 已有真实 NAS、两台 Mac 的 MCP 读写与任务交接验证；不同版本的复验范围见[验收记录](docs/reviews/2026-11-10-final-artifact-acceptance.md)。 |
| Claude Code CLI / Codex 桌面 | 已有真实宿主的 MCP 读写与交接实测；**自动采集、逐轮触发和已有对话召回，仍按 Agent 分别配置与验证**。 |
| 更多 Agent | 提供通用 MCP / Skill 接口；OpenCode、Hermes 等可按接口接入，但尚未完成所有宿主的读写实测。 |
| 网页与备份 | 管理项目、记忆、原文、设备连接和备份；恢复操作与演练范围见[备份与恢复](docs/backup-and-recovery.md)。 |

记忆提炼需要可用的模型接口。使用外部 API 时，相关内容会发送给你配置的服务商；密钥只填在自己的 Hub 设置页，不要放进共享记忆或公开仓库。

## 📚 常用文档

| 想做什么 | 去这里 |
| --- | --- |
| 首次部署、升级或排查 NAS 服务 | [让 Agent 帮你安装](docs/nas-installation.md) · [运行指南](docs/development-runtime.md) |
| 连接 Agent、启用高级本地采集 | [设备与 Agent 接入](docs/client-installation.md) · [自动召回](docs/agent-recall.md) |
| 配置备份、恢复历史版本 | [备份与恢复](docs/backup-and-recovery.md) |
| 查看已完成与待完成能力 | [发布计划](docs/release-plan.md) · [需求说明](docs/requirements.md) |
| 参与开发或报告安全问题 | [贡献指南](CONTRIBUTING.md) · [工程规范](docs/engineering.md) · [安全说明](SECURITY.md) |

<details>
<summary>实测证据、架构与开发检查</summary>

历史记录保留各自的版本与测试边界，不等同于每个新发行包都已覆盖全部场景。

| 内容 | 入口 |
| --- | --- |
| 正式制品与真实设备验收 | [最终制品验收](docs/reviews/2026-11-10-final-artifact-acceptance.md) · [一次性链接接入](docs/reviews/2026-11-10-one-link-onboarding.md) |
| 双机、Claude CLI 与自动召回 | [客户端实测](docs/reviews/2026-11-10-self-service-clients.md) · [下一轮与重连](docs/reviews/2026-11-10-next-turn-and-reconnect.md) |
| 网页、模型与恢复 | [首次网页使用](docs/reviews/2026-11-10-browser-first-use.md) · [模型配置](docs/reviews/2026-11-10-model-catalog.md) · [自动备份](docs/reviews/2026-11-10-managed-backup.md) |
| 架构与上游复用 | [架构](docs/architecture/overview.md) · [Hy-Memory / MemHub 复用清单](docs/research/upstream-assessment.md) |
| 完整测试与历史设计记录 | [验证计划](docs/verification.md) · [开发及运行检查](docs/development-runtime.md#可复现验证) |

开发前阅读[贡献指南](CONTRIBUTING.md)。基础检查为 `make check`；运行时、上游、容器与真实 Agent 验收按变更分别执行，基础检查通过不代表所有产品能力都已验收。

</details>

## 许可证与致谢

自有源码采用 **Apache-2.0**，见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。记忆核心与管理网页复用 [Hy-Memory / MemHub](https://github.com/Tencent-Hunyuan/Hy-Memory)，固定上游版本、补丁及第三方许可随发行保留。
