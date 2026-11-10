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

<a id="在-nas-安装发行版"></a>

## 🚀 三步部署

从下方发行页选择已发布版本，在自己的 NAS 上部署。

1. **准备 NAS。** 使用 Linux amd64 主机，启用 Docker / Compose，并准备电脑可访问的 **HTTPS 入口**；已验证的跨设备方案使用 Tailscale HTTPS。
2. **下载并导入。** 从[发行页](https://github.com/Tsou92/Cloud-Memory-Hub/releases/latest)下载 NAS 镜像、源码包和 `SHA256SUMS`，核对摘要后导入镜像、解压源码。默认接入无需下载 Mac 高级客户端包。
3. **启动自己的 Hub。** 配置服务地址和持久存储，按[发行版首次运行](docs/development-runtime.md#发行版首次运行)启动并登录网页、创建项目。需要记忆自动提炼时，再配置并测试模型接口。

<details>
<summary>查看下载后的校验与导入命令</summary>

将同一发行版本的 NAS 镜像、源码包和 `SHA256SUMS` 放入 NAS 的同一个新目录。下面使用已发布版本作示例；只需将第一行的 `CMH_VERSION` 改为实际下载的版本号（保留 `v` 前缀），再在该目录执行：

```sh
CMH_VERSION=v0.1.2
sha256sum --ignore-missing -c SHA256SUMS &&
docker load -i "cloud-memory-hub-${CMH_VERSION}-linux-amd64.tar.gz" &&
tar -xzf "cloud-memory-hub-${CMH_VERSION}-source.tar.gz" &&
cd "cloud-memory-hub-${CMH_VERSION}" &&
printf 'CMH_IMAGE=cloud-memory-hub:%s\n' "$CMH_VERSION"
```

确认镜像包和源码包都通过校验。把终端最后输出的 `CMH_IMAGE` 配置行写入解压目录的 `.env`，再将 `CMH_PUBLIC_ORIGIN` 设为自己的真实 HTTPS 地址，继续[首次运行](docs/development-runtime.md#发行版首次运行)。已有实例升级前，先按[备份与恢复指南](docs/backup-and-recovery.md)保存配置和数据。

</details>

## 🔌 三步使用

1. **生成连接。** 在 Hub 的「连接 Agent」中选择项目、填写设备名称并选择 Agent。每台电脑分别生成链接，共享同一个项目。
2. **安装连接。** 在目标电脑运行页面生成的一条命令，或把安装话术交给 Agent。按提示重新加载 MCP，并确认实际连接成功；详细步骤见[一次性链接接入](docs/client-installation.md#默认一次性链接接入)。
3. **开始共享。** 让 A 端保存一条测试记忆，再让 B 端读回。准备换设备或 Agent 时，保存任务交接（Session handoff），并在另一端用返回的完整交接 ID 接续。

可以这样告诉 Agent：

> 记住：这个项目的演示页面采用深色主题。
>
> 读取这个项目的相关记忆，接着完成演示页面。
>
> 保存当前任务交接，给我交接 ID；我会在另一台设备继续。

交接传递任务上下文，不会复制另一款 Agent 的原生会话窗口。默认远程 MCP 需要网络，不会自动扫描全部本机历史；需要本地采集或离线补传时，参阅[高级客户端](docs/client-installation.md#高级mac-本地连接组件)。

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
| 安装、升级或排查 NAS 服务 | [运行指南](docs/development-runtime.md) · [发行附件与校验](docs/release-artifacts.md) |
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
