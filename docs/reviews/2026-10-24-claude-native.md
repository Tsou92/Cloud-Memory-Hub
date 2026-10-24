# Claude Code 原生入口验证

日期：2026-10-24。关联 FR-02、FR-05、FR-08、ENG-01。开发者自审，非独立审查。

## 实测范围

`make check-claude` 使用本机安装的 Claude Code CLI 2.1.308，在临时配置目录和合成项目中启动真实进程。显式关闭普通 settings 来源、MCP、工具和 slash commands；不修改日常配置，不读取真实历史。模型端是仅绑定本机回环地址的合成 Anthropic 协议服务，没有调用外部模型。

同一进程连续完成两轮，真实触发 `UserPromptSubmit` 和 `Stop`。第一轮注入 V1；轮间改为 V2；第二轮模型请求的最新用户消息包含 V2，无须重开会话。Stop 的 prompt ID 与对应提交一致，并带有最后一条 assistant 消息。

原生 JSONL 交给固定上游 `b7d9f0e` 的 `create_claude_code_adapter` 和 `AuthorizedSourceRoot`。读取到两条 user、两条 assistant，均保留原生消息身份；注入的上下文没有重新进入采集结果。不变文件再扫描产生零条新增；清空扫描游标后完整重扫，四条幂等标识保持一致。

命令成功报告 `status=passed`。脱敏报告与合成原始证据留在本机受控临时目录，仓库不保存机器路径或对话文件。探针是可重复执行的宿主合同验证，并非产品连接器，也不是模型质量、两设备同步或一分钟时效验收。

## 对实现的约束

- 复用上游原生解析、授权文件读取和游标，不另外编写一套 Claude JSONL 解析器。
- Stop 与文件最终写入存在时间差；持续扫描必须能补采后写入的末条回复，不能把 Stop 时的 EOF 当作最终完成。
- hooks 上报和历史扫描不能分别创建重复消息身份。下一步以原生扫描作为持久采集入口，hook 负责下一轮读取。
- 每个来源显式绑定本地目录和项目 scope；不能自动扫描日常用户目录或把不同项目的记录归入同一 scope。
- 上游 parser 会处理工具块与宿主包装；附件、超限和解析失败仍需在 Hub 边界单独验证，不由本次四条文字记录推定覆盖。

宿主行为依据：[官方 hooks 文档](https://code.claude.com/docs/en/hooks)、[CLI 参考](https://code.claude.com/docs/en/cli-reference)。本次结论以实际运行结果为准，其他版本需复测。
