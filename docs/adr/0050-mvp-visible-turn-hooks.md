# ADR-0050：MVP 本轮可见消息采集与最近原文召回

日期：2026-11-10。状态：已实现并通过两机隔离验收，指定项目日常安装已验证，范围见[执行记录](../reviews/2026-11-10-visible-turn-mvp.md)。关联 FR-01、FR-02、FR-03、FR-05、ENG-01；接续 [ADR-0049](0049-mvp-agent-access-and-handoff.md)。

## 目的与复用

已安装的 MCP／Skill 依赖模型选择工具。为支持 Claude 每轮共享，增加显式工作目录绑定的薄 hook：提交本轮用户文字和最终回复，下一轮自动读取已接纳原文。它不要求 NAS 先完成模型提炼，不扫描历史或工具输出。

复用固定上游 `b7d9f0e` 的 `memhub/apps/core/src/one_memory/live_capture.py::_messages` 提取可见文字与去除上游注入回声；不调用会回退读取 transcript 的完整 `capture_hook`。上游完整入口依赖本机 Core、EvidenceStore 和本地授权，不能替代远程 NAS 项目绑定。继续复用 AgentSettings、prepare_event、LocalOutbox、send_pending、read_remote/history-list、ReadReply、render_context 和现有 Claude 输出合同，不增加队列、数据库、搜索引擎或上游补丁。

## 合同与兼容

新增 `cloud-memory-agent --config-file <私有配置> claude-hook --workspace <已授权工作目录>`。旧 MCP、CLI 命令、agent.json、服务器协议和数据库格式保持兼容。hook payload 的 cwd 与进程实际 cwd 都必须与显式目录一致，不因全局安装而采集其他项目。

只处理 UserPromptSubmit 和 Stop；只读取 prompt 或 last_assistant_message，忽略 transcript_path，不读取文件。稳定事件身份由版本化域、已绑定来源、原生 session、prompt_id 和角色产生，映射为协议要求的 UUID v4 形式；同一 hook 重放复用身份，正文改变产生冲突。缺失可靠身份时报告不可采集，不用随机身份或正文去重掩盖。原生会话只映射到本项目内的稳定身份，不作为服务端授权依据。

采集使用既有 conversation_turn 及 messages/role/text 形式，保留自动可见消息来源标记，不冒充模型提炼出的事实。内容先过滤再持久入现有队列；限额超出明确拒绝，不静默截断后宣称完整采集。网络动作有总期限；离线保留待传，后续 hook 或显式 sync 可重放。接纳不等于提炼成功，也不提供后台常驻保证。

UserPromptSubmit 每轮重新读取绑定项目的最近原文，以现有预算呈现为不可信历史并保留截断提示；空结果表述为本次最近记录为空。只读当前权限范围，失败不回填旧上下文，不扩大权限。Stop 不注入上下文或阻止宿主结束。hook 不使日常对话因服务故障而失败；状态与错误仅输出固定元数据，不回显正文、路径或凭证。

## 安装与验收

先在隔离合成工作区验证；此前两机 MCP／Skill 安装不视为对全局自动采集的授权。日常启用前提供具体目录绑定、配置合并和撤销方式，不启动真实历史导入。

覆盖：同轮角色身份、重放幂等、不同轮次区分、缺失身份、错误工作目录、凭证过滤、离线后补传、大小上限、最近原文更新、读取拒绝及非阻断输出。保留上游 live_capture 回归；真实 Claude 同进程两轮与两 Mac／NAS 读写单列实测，不用模型替身证明真实模型自主选择或提炼质量。
