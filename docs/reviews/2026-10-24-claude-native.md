# Claude Code 原生入口验证

日期：2026-10-24。最终运行源码 `93fc63d`，分支 `codex/claude-source-adapter`。关联 FR-02、FR-03、FR-05、FR-06、FR-07、FR-08、ENG-01。开发者自审，非独立审查。

## 实测范围

`make check-claude` 使用本机安装的 Claude Code CLI 2.1.308，在临时配置目录和合成项目中启动真实进程。显式关闭普通 settings 来源、MCP、工具和 slash commands；不修改日常配置，不读取真实历史。模型端是仅绑定本机回环地址的合成 Anthropic 协议服务，没有调用外部模型。

同一进程连续完成两轮，真实触发 `UserPromptSubmit` 和 `Stop`。第一轮注入 V1；轮间改为 V2；第二轮模型请求的最新用户消息包含 V2，无须重开会话。Stop 的 prompt ID 与对应提交一致，并带有最后一条 assistant 消息。

原生 JSONL 交给固定上游 `b7d9f0e` 的 `create_claude_code_adapter` 和 `AuthorizedSourceRoot`。读取到两条 user、两条 assistant，均保留原生消息身份；注入的上下文没有重新进入采集结果。不变文件再扫描产生零条新增；清空扫描游标后完整重扫，四条幂等标识保持一致。

命令成功报告 `status=passed`。脱敏报告与合成原始证据留在本机受控临时目录，仓库不保存机器路径或对话文件。首次探针只验证宿主合同，产品采集增量见下文；两者都不是模型质量、两设备同步或一分钟时效验收。

## 产品采集增量

后续扩展同一探针，另起真正的 `cloud_memory_hub.connector collect-watch` 进程。在同一 Claude 会话的两轮中，产品采集进程自动获得四条原生消息并事务入队。停止采集进程后重新调用 collect，没有新增事件；记录身份唯一，正文与上游原生解析结果一致，仍不含注入上下文。报告 `product_capture_validated=true`、`product_capture_native_records=4`、`product_recall_validated=false`；本地模型仍为替身。

复用模块的原有测试：`test_primary_source_adapters.py`、`test_source_access.py`、`test_source_readers.py`、`test_jsonl_snapshot_tail.py`，通过固定上游隔离执行器运行，145 项通过。新增 Hub 测试覆盖真实上游解析与队列事务组合，包括追加、未结束尾行、重启、全量重扫、工具拆分失败后恢复、过滤、项目绑定、密钥／游标异常和文件根替换。

最终 `make check-runtime` 为 162 项通过，`make check` 的格式、静态检查、19 项工程门禁、链接和凭证扫描通过。实际子进程在插入事件后调用 os._exit，恢复后事件与游标均回滚，再采集只产生一条事件。开发者自审检查了授权绑定、追加扩展兼容、加密游标、事务边界、元数据过滤及错误输出；没有将其记为独立审查。

自审另发现无原生消息 ID 的纯文本回退身份会合并同会话重复句子。采集映射加入上游原生位置，新增回归验证两条同文记录分别保留且重扫幂等。带原生 ID 的正常 CLI 路径保持原有身份。

首轮发现同长度／同 mtime 的原地改写被跳过；复现测试保留，修复使用实际 ctime，复用上游读取／锚点和身份去重。一次中间实现误把上游 birth time 字段当作 ctime，测试继续失败，已纠正。另一个初始失败是测试的脱敏标记预期与现有过滤器不一致，修正预期，未放宽不含原始凭证的断言。

## 容器与 NAS 制品

`cloud-memory-hub:dev-93fc63d` 在本地和 NAS 独立容器完整通过 `scripts.smoke_container`。新增 `probe_source_capture.py` 使用合成 Claude 格式文件，运行镜像内安装的 capture/send CLI、真实 HTTP 接口和 Core 持久队列：两条初始记录接纳，服务重启后原记录不重采，再追加一条回复，最终恰好三个处理任务。原有离线 embedding、身份／配置、发送重放、真实 SDK 检索和撤销检查继续通过。容器没有运行 Claude 客户端，这项不替代上面的 Mac 真机测试。

旧 `dev-45f15c1` 镜像另在禁网临时容器中，打开真实 CLI 探针新建的合成队列副本；实际读取四条待发事件并提交一条原格式回执，结果三条待发、一条已接纳。它验证旧发送器存储兼容性，不代表旧版实现了新采集能力。

| 制品 | SHA-256 |
| --- | --- |
| 镜像配置 | `a8739d6fd57a64497d1a8e66fb5de19ae9dc0b6dfb63fba75ee609a0bddd7500` |
| OCI manifest-list | `99d7517fb43ee5917a547523b48a6626dc510f0908708c84936c8259c47a44bc` |
| 归档（261182464 字节） | `a076a0762ccdb9f670074e2b2f0dcd109004ff9fd6c99470e0852f8656da1d79` |

初次加载校验误将本机 inspect 返回的 manifest-list ID 与 NAS 返回的配置 ID 直接比较，断言失败。随后从已校验归档读取 manifest/config，计算配置 SHA-256，并核对 NAS 加载结果、架构和完整 revision；各项一致。保留该验证错误的说明，没有用标签名称替代内容校验。

NAS 常驻实验实例完成 `45f15c1 → 93fc63d → 45f15c1 → 93fc63d`。升级前 0600 的 config/data 归档分别为 74240/553472 字节；原凭证、实例 ID、SDK 权威状态、空记忆库、无模型／设备／来源授权状态保持一致。最终镜像健康，3GiB／2CPU、只读根、cap-drop ALL、127.0.0.1:8800 均实测保留；镜像设置已持久化，备份与旧镜像保留。没有修改 SSH 或日常 Agent 设置。

证据存于忽略目录 `.local/claude-native-probe/`：`native-93fc63d.log`、`capture-runtime-identity.log`、`capture-upstream.log`、`capture-container.log`、`capture-nas.log`、`old-sender-compatibility.log`、`capture-upgrade.log`；制品信息存于 `.local/nas-transfer/93fc63d/`。实验实例仍无真实历史或激活的外部模型；空实例镜像回退不等于完整备份／删除规则恢复验收。

## 对实现的约束

- 复用上游原生解析、授权文件读取和游标，不另外编写一套 Claude JSONL 解析器。
- Stop 与文件最终写入存在时间差；持续扫描必须能补采后写入的末条回复，不能把 Stop 时的 EOF 当作最终完成。
- hooks 上报和历史扫描不能分别创建重复消息身份。下一步以原生扫描作为持久采集入口，hook 负责下一轮读取。
- 每个来源显式绑定本地目录和项目 scope；不能自动扫描日常用户目录或把不同项目的记录归入同一 scope。
- 上游 parser 会处理工具块与宿主包装；附件、超限和解析失败仍需在 Hub 边界单独验证，不由本次四条文字记录推定覆盖。

宿主行为依据：[官方 hooks 文档](https://code.claude.com/docs/en/hooks)、[CLI 参考](https://code.claude.com/docs/en/cli-reference)。本次结论以实际运行结果为准，其他版本需复测。
