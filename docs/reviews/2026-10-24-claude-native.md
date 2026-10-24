# Claude Code 原生入口验证

日期：2026-10-24。关联 FR-02、FR-05、FR-08、ENG-01。开发者自审，非独立审查。

## 实测范围

`make check-claude` 使用本机安装的 Claude Code CLI 2.1.308，在临时配置目录和合成项目中启动真实进程。显式关闭普通 settings 来源、MCP、工具和 slash commands；不修改日常配置，不读取真实历史。模型端是仅绑定本机回环地址的合成 Anthropic 协议服务，没有调用外部模型。

同一进程连续完成两轮，真实触发 `UserPromptSubmit` 和 `Stop`。第一轮注入 V1；轮间改为 V2；第二轮模型请求的最新用户消息包含 V2，无须重开会话。Stop 的 prompt ID 与对应提交一致，并带有最后一条 assistant 消息。

原生 JSONL 交给固定上游 `b7d9f0e` 的 `create_claude_code_adapter` 和 `AuthorizedSourceRoot`。读取到两条 user、两条 assistant，均保留原生消息身份；注入的上下文没有重新进入采集结果。不变文件再扫描产生零条新增；清空扫描游标后完整重扫，四条幂等标识保持一致。

命令成功报告 `status=passed`。脱敏报告与合成原始证据留在本机受控临时目录，仓库不保存机器路径或对话文件。探针是可重复执行的宿主合同验证，并非产品连接器，也不是模型质量、两设备同步或一分钟时效验收。

## 产品采集增量

后续扩展同一探针，另起真正的 `cloud_memory_hub.connector collect-watch` 进程。在同一 Claude 会话的两轮中，产品采集进程自动获得四条原生消息并事务入队。停止采集进程后重新调用 collect，没有新增事件；记录身份唯一，正文与上游原生解析结果一致，仍不含注入上下文。报告 `product_capture_validated=true`、`product_capture_native_records=4`、`product_recall_validated=false`；本地模型仍为替身。

复用模块的原有测试：`test_primary_source_adapters.py`、`test_source_access.py`、`test_source_readers.py`、`test_jsonl_snapshot_tail.py`，通过固定上游隔离执行器运行，145 项通过。新增 Hub 测试覆盖真实上游解析与队列事务组合，包括追加、未结束尾行、重启、全量重扫、工具拆分失败后恢复、过滤、项目绑定、密钥／游标异常和文件根替换。

完成采集实现后的 `make check-runtime` 为 161 项通过，`make check` 的格式、静态检查、19 项工程门禁、链接和凭证扫描通过。实际子进程在插入事件后调用 os._exit，恢复后事件与游标均回滚，再采集只产生一条事件。开发者自审检查了授权绑定、追加扩展兼容、加密游标、事务边界、元数据过滤及错误输出；没有将其记为独立审查。

自审另发现无原生消息 ID 的纯文本回退身份会合并同会话重复句子。采集映射加入上游原生位置，新增回归验证两条同文记录分别保留且重扫幂等；补充后 Hub 为 162 项通过，工程检查再次通过。带原生 ID 的正常 CLI 路径保持原有身份。

首轮发现同长度／同 mtime 的原地改写被跳过；复现测试保留，修复使用实际 ctime，复用上游读取／锚点和身份去重。一次中间实现误把上游 birth time 字段当作 ctime，测试继续失败，已纠正。另一个初始失败是测试的脱敏标记预期与现有过滤器不一致，修正预期，未放宽不含原始凭证的断言。

容器验证由 `probe_source_capture.py` 使用合成 Claude 格式文件，运行镜像内安装的 capture/send CLI、真实 HTTP 接口和 Core 持久队列，并跨服务重启验证。此项与真实 Claude CLI 测试分别记录；容器并没有运行 Claude 客户端。其执行结果待制品验证完成后补记。

## 对实现的约束

- 复用上游原生解析、授权文件读取和游标，不另外编写一套 Claude JSONL 解析器。
- Stop 与文件最终写入存在时间差；持续扫描必须能补采后写入的末条回复，不能把 Stop 时的 EOF 当作最终完成。
- hooks 上报和历史扫描不能分别创建重复消息身份。下一步以原生扫描作为持久采集入口，hook 负责下一轮读取。
- 每个来源显式绑定本地目录和项目 scope；不能自动扫描日常用户目录或把不同项目的记录归入同一 scope。
- 上游 parser 会处理工具块与宿主包装；附件、超限和解析失败仍需在 Hub 边界单独验证，不由本次四条文字记录推定覆盖。

宿主行为依据：[官方 hooks 文档](https://code.claude.com/docs/en/hooks)、[CLI 参考](https://code.claude.com/docs/en/cli-reference)。本次结论以实际运行结果为准，其他版本需复测。
