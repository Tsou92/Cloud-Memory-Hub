# Codex 桌面隔离接入调查

日期：2026-11-09。状态：只读调查完成，桌面探针尚未执行。关联 FR-01、FR-02、FR-05、ENG-01 及 [G1 下一步第 3 项](../roadmap.md)。

## 结论与证据等级

已找到本机 Codex 桌面构建中的独立数据目录、独立后台进程和 Hooks 入口，可以据此准备隔离探针。尚未证明这些入口足以隔离全部用户状态，也未证明真实桌面已有对话的下一轮能够自动读写 Cloud Memory Hub。CLI、app-server、安装器测试和官方文档均不能替代桌面运行证据。

本次只读取仓库、固定上游源码、应用公开安装元数据及打包资源，并执行内置 CLI 的 `--version`、`--help`、`app --help`、`app-server --help`、`plugin --help`。没有启动新的桌面实例、app-server 或模型会话，没有读取日常配置、认证文件、聊天库或 Keychain 内容，没有修改用户设置、替换应用或运行客户端探针。

## 本机公开安装事实

| 项目 | 本次观察 |
| --- | --- |
| 标准安装位置 | `/Applications/ChatGPT.app`，并非 `/Applications/Codex.app` |
| Bundle ID | `com.openai.codex` |
| 产品名／可执行文件 | Codex／`Contents/MacOS/ChatGPT` |
| 应用版本／构建号 | `26.1103.60933`／`25894` |
| 内置 CLI | `0.167.0-alpha.3.2`，`aarch64-apple-darwin` |
| 版本来源 | `Contents/Info.plist`、`Contents/Resources/codex-cli/codex-package.json`、内置 CLI `--version` |
| 桌面代码来源 | `Contents/Resources/app.asar` 内的 `package.json`、主进程及设置页资源 |

[早期候选调查](2026-10-22-upstream-candidate.md) 中的版本属于当时观察，不能当作本次版本。本次未读取应用运行日志或用户状态来补证。

## 桌面隔离入口的源码证据

下列资源名均相对于 `app.asar`，哈希后缀只用于复核本次安装构建，不作为产品依赖。

| 入口 | 观察到的实现 | 不能据此宣称的结果 |
| --- | --- | --- |
| `CODEX_HOME` | `.vite/build/early-bootstrap-824-8c6252e0131a7fe3.js` 的路径解析优先读取该环境变量，否则回落到用户主目录的 `.codex` | 不代表所有用户级资源、系统配置和认证服务均已隔离 |
| `CODEX_ELECTRON_USER_DATA_PATH` | `.vite/build/early-bootstrap-339-cb7e73ca5a71df7c.js` 先设置 Electron `userData`，随后处理单实例锁；显式数据目录会参与锁策略判断 | 未实测两实例是否完全分离，也未证明窗口、后台及退出操作互不影响 |
| `--user-data-dir` | `.vite/build/early-bootstrap-815-e030c49be4b9c405.js` 的内部 demo 启动流程同时传递该参数、`CODEX_HOME` 和 `CODEX_ELECTRON_USER_DATA_PATH` | 内部流程不是公开稳定的测试 profile 合同 |
| `CODEX_APP_SERVER_FORCE_CLI=1` | `824` 和 `815` 两个资源块据此跳过 WebSocket 路由；本地 daemon 复用条件也要求该变量不为 `1` | 未启动验证，不能断言已排除所有后台共享 |
| 无 OpenAI 账号的 provider | `339` 资源块的连接认证判断允许 `authMethod=null` 且 `requiresOpenaiAuth=false` | 尚未验证首次启动界面、模型选择或其他功能是否另有登录要求 |
| 桌面 Hooks | 设置资源包含 `UserPromptSubmit` 等事件和信任提示；主进程请求集合包含 `hooks/list` | 页面与方法存在不等于真实提交事件、执行及上下文注入已通过 |

内置 CLI 的 `--profile` 帮助说明它把指定 profile 配置叠加到基础用户配置上，因此不能单独充当不读取日常配置的隔离机制。`app --help` 也没有给出独立桌面 profile 参数。

打包代码仍有用户主目录 `.agents/skills` 的发现路径。仅重定向两个数据目录，无法证明不会读取其他用户层。Keychain、系统／MDM 配置、后台认证及其访问边界未实测；本次没有尝试读取这些内容。

## 官方合同与复用边界

截至调查日，[官方 Hooks 文档](https://learn.chatgpt.com/docs/hooks) 说明：可在活动配置层旁使用 `hooks.json` 或内联 hooks；`UserPromptSubmit` 可通过 `hookSpecificOutput.additionalContext` 提供额外上下文，`Stop` 可携带 `last_assistant_message`。非托管 hook 需要信任具体定义，项目层还受项目信任约束。多个来源的 hooks 可以同时加载，不能认为新配置会覆盖其他来源。

[官方配置参考](https://learn.chatgpt.com/docs/config-file/config-reference) 提供自定义 `model_provider`、`requires_openai_auth=false`、`cli_auth_credentials_store`、`sqlite_home`、`log_dir` 和 `model_catalog_json` 等配置。可据此准备本地合成 provider 和独立存储；配置字段存在不证明整个桌面进程不会访问 Keychain，也不证明已禁止外网。

固定上游基线为 `b7d9f0e0f417eae30b192882f848b432484c20a6`，具体版本和增量补丁以 [manifest](../../upstream/manifest.json) 为准。以下路径相对于上游仓库：

| 能力 | 复用位置 | 当前证据限制 |
| --- | --- | --- |
| Codex 历史与增量解析 | `memhub/apps/core/src/one_memory/imports/sources/codex.py`；`memhub/apps/core/tests/test_codex_header_provenance.py` | 解析器和合成样例存在；没有本次桌面实际生成记录的验证 |
| hook 可见消息提取 | `memhub/apps/core/src/one_memory/live_capture.py`；`memhub/apps/core/tests/test_live_capture.py`、`memhub/apps/core/tests/test_capture_deadline.py` | 复用 `prompt`、`last_assistant_message` 处理；其直接写 Core Evidence 的路径不等同于 Hub 远程设备接纳 |
| Codex 配置安装与回滚 | `memhub/packages/host-bridge/src/recall-installer.ts::codexLayout`；`memhub/packages/host-bridge/src/codex-installer.test.ts` | 使用可传入的独立根目录；已有冲突保护、安装日志和回滚合同，未证明桌面热加载 |
| Capture Hooks 安装 | `memhub/packages/host-bridge/src/capture-hooks.ts`、`memhub/packages/host-bridge/src/capture-hooks.test.ts` | Codex 注册 `UserPromptSubmit` 和 `Stop`；不能据安装成功推定 hook 已获信任并运行 |
| 召回协议与预算 | `memhub/docs/contracts/host-context-transport-v1.md`、`memhub/apps/core/src/one_memory/context_auto_recall.py` | 上游传输／协议验证不等于真实桌面或真实模型验收 |

上游 `memhub/docs/harness-support/codex.md` 明确说明：当前安装包括 TOML MCP 配置、Skill、Capture Hooks 和授权；模型主动调用 MCP 才读取记忆，没有 Prompt 前自动预取，也不保证正在运行的 Codex 热加载。这一限制不能以“一键插入已开放”掩盖。

本仓库可继续复用：

- [source_capture.py](../../apps/hub/src/cloud_memory_hub/source_capture.py) 的授权读取、游标、过滤及投递组合，但当前配置和适配器注册只支持 `claude-code`，Codex 接线尚未实现。
- [claude_hook.py](../../apps/hub/src/cloud_memory_hub/claude_hook.py) 与 [recall.py](../../apps/hub/src/cloud_memory_hub/recall.py) 的输入边界、授权召回、预算和上下文输出。需显式适配 Codex 身份与配置，不能用 Claude 来源冒充 Codex。
- [probe_claude_native.py](../../scripts/probe_claude_native.py)、[probe_claude_recall.py](../../scripts/probe_claude_recall.py) 的临时实例、真实 Hub HTTP、观察记录及收尾逻辑。现有模型替身使用 Anthropic Messages 流式格式，Codex 需要对应 Responses 合同的小型替身，不能原样复用协议。
- 现有 LocalOutbox、过滤、传输、SDK 写入和读取能力；不另建第二套事件存储或召回后端。

## 待执行的最小隔离探针

以下是准备方案，不是已执行结果。先验证桌面宿主合同，再接产品链路；具体实现进入后续可审查变更。

### 1. 建立并验证隔离边界

创建唯一的临时根目录，分别容纳 Codex home、Electron 数据、SQLite、日志、合成项目、hook 文件、Hub 实例和本地模型替身。不要复制日常配置、认证、历史或插件状态。配置选择专用合成 provider，模型地址只指向回环接口，关闭更新和遥测，并显式指定临时存储位置。

在启动桌面前，先验证进程级文件与网络限制：允许应用运行所需的公开程序／系统资源及临时目录；限制日常 Codex 数据、其他用户配置和凭证服务访问；网络只允许探针需要的本机回环端点。清理继承环境中的外部 provider、远程 app-server 和认证入口，避免默认回落。只改模型 URL、代理或 Agent 工具权限，不构成桌面整体禁外网的证明。

本机存在 `/usr/bin/sandbox-exec`，但限制策略与此应用构建的兼容性尚未测试。若采用它，应直接启动实际桌面可执行文件，使后台子进程处于同一受限进程树；不能把 `sandbox-exec` 包住 `open -n` 就宣称 LaunchServices 启动的应用继承了限制。

预定桌面入口为标准安装路径下的 `Contents/MacOS/ChatGPT`，同时使用独立 `CODEX_HOME`、`CODEX_ELECTRON_USER_DATA_PATH`、`--user-data-dir` 及 `CODEX_APP_SERVER_FORCE_CLI=1`。先核验窗口、进程树、存储位置、后台连接和限制日志，再允许发送合成消息。无账号启动或本地 provider 若被拒绝，应保留失败证据，不引入真实凭证或切换付费模型来制造通过。

### 2. 只验证真实桌面宿主合同

在隔离桌面中审查并信任预先生成的合成 hooks；不使用日常实例执行信任操作，也不依赖绕过 hook 信任的全局设置。

第一轮由真实桌面提交合成提示，观察 `UserPromptSubmit`、`Stop` 的事件字段和本地模型实际请求。模型替身记录有界合成输入，并依据收到的上下文标记生成响应。改变探针记忆标记后，在同一个桌面对话提交相同问题，证明新一轮触发重新读取、模型请求包含新标记、回答反映该标记。不能预先固定回答，再把回答文本当作成功证据。

这一阶段最多证明宿主事件及下一轮注入合同。合成 hook 不得冒充产品连接组件；模型替身也不证明真实模型的回答质量。

### 3. 接入产品最短闭环

在显式 Codex 适配接线完成后，复用现有 Hub、连接组件和 SDK：

1. 先打开并保留桌面对话 B，完成一次基线交互。
2. 来源 A 产生合成事实，通过产品采集、过滤、持久投递、接纳和 SDK 提炼进入记忆；不能只手工种入最终记忆后声称自动写入通过。
3. B 的下一轮自动召回并使用新事实，无需重开对话、手动刷新或明确要求调用 MCP。
4. 再更新来源 A，B 在同一对话下一轮读到新版本；分别记录接纳、提炼可检索和下一轮读取时刻。
5. 验证桌面消息自动写回、重扫不重复、注入上下文不被当作用户原文重复采集，以及撤销授权／断网时不注入旧结果。

宿主探针优先使用 hook 提供的当前消息字段。若需要观察桌面实际记录，仅允许探针创建的独立目录，并先验证格式。内置 CLI 已有 `migrate-rollouts` 命令，因此不能假定当前桌面必然继续使用固定上游解析器所支持的旧式 JSONL；格式不符须明确报告，不能转而读取真实历史。

### 4. 停止、回收与证据保存

启动器记录本次隔离实例的 PID、子进程关系及唯一临时根。先停止探针的采集和投递，再结束该隔离进程树，确认后台退出后回收其临时资源。不要按应用名批量终止进程，也不要向 bundle ID 发全局退出，以免关闭日常实例。

只保存版本、合成事件顺序、请求／回执关联、次数、状态、耗时及必要的合成断言；不保存真实凭证或日常文件内容。失败现场保留在隔离目录，不能删库重建后把失败样例从结果中移除。

## 验收缺口与本次检查范围

仍未证明：独立桌面与后台实际隔离、无账号桌面对话可用、hook 信任仅作用于隔离状态、全部子进程禁外网、当前桌面记录格式、完整自动提炼链路，以及已有对话下一轮使用新版本。两台 Mac、NAS 网络与一分钟目标、离线读取、六入口闭环继续按 [验证计划](../verification.md) 验收。

本次没有运行或重跑上述上游测试、桌面探针、付费模型或 NAS 检查；这里只引用其源码位置与已有合同，不计入新的通过数量。文档交付已检查本文件的链接、个人路径排除和差异；`make check` 通过，包含 19 项工程门禁测试、格式／静态检查、仓库链接、凭证扫描和差异空白检查。这些检查不改变 [来源采集](../source-capture.md) 或 [Claude 召回](../agent-recall.md) 当前已声明的能力。
