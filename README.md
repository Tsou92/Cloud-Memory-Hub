# Cloud Memory Hub

部署在 NAS 上、连接不同设备与 Agent 的共享记忆服务。

**当前阶段：G1 复用与可行性验证。实验性管理入口已在本地和 NAS 容器验证，尚未发布完整产品。**

实验入口直接复用固定版本的 MemHub Core 和管理网页，增加 NAS 认证边界及加密凭证存储。跨设备自动采集、同步、离线缓存、删除传播等能力仍在开发，当前不应接入真实历史或用作唯一记忆存储。

已增加版本化的设备身份与上传接口：独立凭证、项目授权、轮换、撤销、来源注册和持久接纳。上传直接复用上游处理队列；接纳回执不等于完成提炼、检索或客户端同步。身份兼容性见 [ADR-0004](docs/adr/0004-remote-device-access.md)，上传合同见 [远程接纳接口](docs/remote-ingest.md)。

本地连接组件已提供凭证过滤、持久投递队列及隔离 CLI，自动采集与网络发送器仍待接通；使用与限制见 [连接组件说明](docs/local-connector.md)。

项目默认提供跨 Agent 的任务接续，同时保留原始记录追溯能力。首批目标入口为 Claude Code CLI、Codex 桌面版、OpenCode 终端、Hermes 终端、Cherry Studio 客户端和 Open Design 客户端；通过通用接口持续扩展其他 Agent。

## 开发原则

- 需求、复用调查、架构决策、实现、测试、审查和发布均进入 Git。
- **优先复用 Hy-Memory / MemHub 已有代码及测试，不重复开发已有能力。**
- 全自动读写、已有对话刷新等能力必须在实际客户端验收，不能用 MCP 已连接代替。
- 源码仓库不保存真实对话、记忆库、备份、密钥或个人机器配置。

## 项目导航

| 内容 | 入口 |
| --- | --- |
| 已确认产品需求 | [需求说明](docs/requirements.md) |
| 开发流程与质量门槛 | [工程规范](docs/engineering.md) |
| 上游代码复用清单及差距 | [Hy-Memory / MemHub 评估](docs/research/upstream-assessment.md) |
| 架构方向与备选方案 | [架构草案](docs/architecture/overview.md) |
| 决策记录 | [ADR-0001：复用优先](docs/adr/0001-reuse-first.md) |
| 容器运行与配置 | [实验运行指南](docs/development-runtime.md) · [ADR-0002：NAS 边界](docs/adr/0002-nas-runtime-boundary.md) |
| 上游升级与数据边界 | [ADR-0003：SDK 权威存储基线](docs/adr/0003-sdk-authority-baseline.md) |
| 测试与需求追踪 | [验证计划](docs/verification.md) |
| 分阶段交付 | [里程碑](docs/roadmap.md) |
| 本轮工程评审状态 | [评审记录](docs/reviews/2026-10-21-foundation.md) |
| 运行时验证进展 | [容器与上游验证记录](docs/reviews/2026-10-22-runtime.md) |
| NAS 与设备身份验证 | [NAS 运行记录](docs/reviews/2026-10-23-nas-preflight.md) · [设备身份记录](docs/reviews/2026-10-23-device-access.md) |
| 贡献与安全 | [贡献指南](CONTRIBUTING.md) · [安全说明](SECURITY.md) |

## 本地质量检查

工程检查需要 Git、Python 3.12–3.14，以及 uv。运行时使用 Python 3.12、uv 0.12.5 和上游约束版本；各自使用锁文件。

```sh
uv sync --locked
make check
make check-runtime
make check-upstream
# 以下两项需要 Docker
make check-web
# 构建 Linux amd64 并在独立临时卷中验证
make check-container
```

检查包括格式、静态检查、工程门禁测试、文档链接、禁止入库的数据路径和源码凭证扫描。当前通过这些检查**不代表产品功能已通过验收**。

## GitHub 与发行状态

目前为本地 Git 仓库，尚未创建远程仓库、配置远程分支保护或发布产品。
仓库提供 GitHub Actions 工作流、Issue / PR 模板和依赖更新配置；上传后才能验证远程执行。

项目自身的许可证尚未选定。公开发布前应确定仓库可见性、许可证及第三方声明。
上游代码引入时保留原始许可证、NOTICE、来源提交和修改记录；当前尚未复制上游业务源码。
