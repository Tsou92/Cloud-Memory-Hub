# 通用 Agent 入口与任务交接 MVP 验证

日期：2026-11-10。关联 FR-01、FR-02、FR-03、FR-05、FR-07、FR-12、ENG-01；设计见 [ADR-0049](../adr/0049-mvp-agent-access-and-handoff.md)，接入见 [Agent 使用说明](../agent-access.md)。

## 阶段与实现

按用户明确的优先级，当前已进入 **G2 MVP 实现和接入验收**：先让多设备、多 Agent 共享记忆，再提供通用 MCP／Skill 和 session handoff。G1 遗留的桌面可行性继续作为单列风险，不再阻断整个 MVP 主线；旧日志治理等系统性完善后置。

已实现共用 Agent 服务、JSON 标准输入 CLI、本地 stdio MCP 和可安装 Skill。业务复用 RemoteConnection、CredentialFilter、LocalOutbox、现有上传及授权读取，服务端继续使用原有 Core 队列和接纳原文归档，没有建立第二套记忆存储。

- `remember` 保存 Agent 已总结的事实或进度，另一端通过 `recent` 读取已接纳原文；模型提炼不是读取这些更新的前置。
- `search` 读取已提炼记忆，与立即可读的原文分开报告。
- `handoff_save` 保存目标、进度、决定、下一步与阻碍；接纳后返回 `cmhe:` ID，另一端 `handoff_load` 完整分页读取已过滤正文。
- 本机待传、NAS 接纳和提炼状态分别呈现。重试沿用相同请求身份，凭证过滤和项目授权继续有效。

Skill 规定会话开始和下一轮读取、关键进度保存及切换前交接。它依靠模型实际调用工具，不构成宿主 hook 的每轮执行保证，也不恢复原 Agent 的隐藏状态或原生 session。

## 同镜像安装验证

使用 `mvp-preflight` 镜像在本地和 NAS 分别运行安装探针。Docker save/load 后，两端镜像的 Architecture、Os、RootFS 与 Config 逐项相同；不同 image store 呈现的镜像 ID 不作为内容不一致的依据。本轮 6 个生产模块的源码与两端安装内容逐项 SHA-256 相同。探针使用安装后的 Hub 和官方 MCP 客户端，通过真实 stdio／HTTP 运行；输入、项目和设备身份均为合成数据，未启用外部模型。

| 安装探针检查 | 本地 | NAS |
| --- | --- | --- |
| MCP 初始化和完整工具列表 | 通过 | 通过 |
| 保持同一个读取连接，读取另一身份的新接纳原文 | 通过 | 通过 |
| 相同请求重试返回同一回执 | 通过 | 通过 |
| 长交接完整分页读取与本机凭证过滤 | 通过 | 通过 |
| 异项目交接读取拒绝 | 通过 | 通过 |
| 离线入队、恢复补传和同身份重放 | 通过 | 通过 |
| 合计 | **6/6** | **6/6** |

本地探针总耗时 11.624 秒，合成更新读取耗时 0.345 秒；NAS 分别为 12.560 秒和 0.443 秒。两次运行各自使用同一机器上的多个合成设备身份，**不是两台 Mac 的网络延迟或一分钟同步目标验收**。保持 MCP 读取连接也不等于实际 Agent 已有对话的下一轮自动调用。

可复用探针为 [probe_agent_access.py](../../scripts/probe_agent_access.py)，需与 `probe_claude_recall.py`、`probe_claude_native.py` 同目录，使用已安装 Hub 的 Python 运行：

```sh
/path/to/runtime/bin/python /path/to/probes/probe_agent_access.py
```

它建立独立合成测试状态，不使用日常 Agent 配置。成功报告包含 `checks_passed`、`synthetic_update_seconds`，并明确 `external_model_used=false`、`two_physical_macs_verified=false`、`automatic_extraction_verified=false`。正式 Git 提交号待提交后补记，当前不把工作区镜像标成已发布版本。

## NAS 实例与真实接入

新独立 MVP 实例 `cmh-mvp-hub-1` 已部署并处于 healthy；重启后健康检查、项目及设备读取均返回 200，部署配置已持久保存。旧 `cmh-nas-validation-hub-1`／`c0467f5` 保持不变；本轮没有用新实例替换旧验证实例，也未启用外部模型。

已通过新实例的现有管理 API 创建独立 MVP 项目及两个客户端身份，分别用于 MacBook 预览和合成对端。本机已生成私有连接、队列及标准 MCP 配置，Skill 已打包为 ZIP；私有路径和凭证不进入仓库。使用已安装的 Codex CLI，在隔离 `CODEX_HOME` 中执行 `mcp add` 成功，证明配置可注册；未修改日常配置，也未验证 Codex 桌面实际调用。

Tailscale Serve 检查仍为未启用，现有 Serve 配置为空；启用链接已发送给用户，尚未据此宣称远程入口可用。第二台 Mac mini 当前离线，实际两 Mac 接入待其联机后验证。

Codex 桌面隔离启动失败，未取得真实桌面 MCP 调用或已有对话下一轮读取证据。本轮不继续扩展桌面故障调查，也不以 MCP 安装探针替代宿主验证。六入口的其他已有依据与限制见[能力清单](../research/g1-client-capabilities.md)。

## 工程检查

本轮 `make check` 已通过。首次全量 Hub 回归结果为 **1787 通过、5 跳过、1 失败**。连续启动真实 Hub 的新增测试改变了 pytest 共享日志捕获器，导致后续 SDK 测试日志断言失败；最小组合可复现。修复仅在新增测试 fixture 中恢复捕获器状态，不改变产品日志策略或旧断言。

修复后，新增两文件加原失败单测 **30 通过**；随后覆盖通用 Agent、MCP、读取传输、本地投递、发送及 SDK 接纳恢复的定向回归 **154 通过**，`make check` 再次通过。未重复全量运行，不把定向结果写成全量全绿，5 项跳过也不计为通过。

```sh
PYTHONDONTWRITEBYTECODE=1 apps/hub/.venv/bin/python -m pytest -q \
  apps/hub/tests/test_agent_access.py apps/hub/tests/test_agent_mcp.py \
  apps/hub/tests/test_read_transport.py apps/hub/tests/test_local_delivery.py \
  apps/hub/tests/test_transport.py apps/hub/tests/test_sdk_reconcile.py
```

## 下一步

先完成实际网络入口、第二台 Mac 联机和日常客户端接入验收，分别验证跨机更新、精确交接及已有会话下一轮调用。离线读取缓存、全量历史、六入口自动化和系统性治理仍按原需求后续推进，不纳入本轮已完成声明。
