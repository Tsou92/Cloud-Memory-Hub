# Hub 专项回归索引

本索引记录新增发布修复的相关测试，不代替全套测试收集或真实客户端验收。

## ADR-0056：忘记后的自动历史召回

- 需求：FR-03、FR-05、FR-10、FR-11；设计见 [ADR-0056](../../../docs/adr/0056-forgotten-history-recall.md)。
- `test_forgotten_history_recall.py`：复用真实 SDK 合成提炼、治理预览／执行、HTTP 与 Agent 读取链；列表和自动 `_recall` 排除已忘记接纳原文及 SDK raw，精确 get 保留正文，未忘记／其他项目原文与分页保持；独立合同覆盖 purged 的 list/get 拒绝。
- `test_governance_execution.py`：保留既有忘记、彻底删除、恢复与来源屏蔽回归。
- `test_remote_read.py`、`test_original_archive.py`、`test_read_transport.py`、`test_agent_hook.py`、`test_recall.py`：授权读取、原文归档、客户端传输与自动召回的相关回归。
- 2026-11-10 执行结果：修复前专项 2 失败／1 通过，两项失败分别复现真实治理后的接纳原文泄漏和受控原生 raw 的过滤缺失；purged 分支保持通过。修复后专项 3 通过；上述七个测试文件合计 118 通过，无跳过。首次组合夹具把 SDK 结果身份误当作原生 raw 列表项，在修复前预检失败；改为复用原生 raw 精确导入后才取得上述有效红绿证据。
- 新增真实组合用例禁用 socket 连接，并使用现有合成提炼替身；合同用例使用受控 raw 返回值验证抑制表边界。合成模型不代表真实模型质量、NAS 安装或两机宿主验证；本轮尚未部署。

## ADR-0057：管理员网页读取

- 需求：FR-01、FR-03、FR-10、FR-12；设计见 [ADR-0057](../../../docs/adr/0057-administrator-console-reads.md)，合同见[管理员网页接口](../../../docs/console-api.md)。
- `test_console_read.py`：真实临时 SDK／Hub 两项目的接纳原文、记忆、来源与设备读取一致；bootstrap 项目分页、显式 global、所选项目来源分页、未知总数；管理员会话同源、设备与伪造身份拒绝、未知项目／越界 ID／输入预算；真实忘记与受控 purged 状态；权威 hydration、凭证保护、安全异常与响应预算；管理员／设备共用查询限额。
- 2026-11-10：专项 7 通过；上述测试与 `test_forgotten_history_recall.py`、`test_remote_read.py`、`test_read_transport.py`、`test_productivity_read.py`、`test_original_archive.py`、`test_agent_hook.py`、`test_recall.py`、`test_agent_access.py`、`test_remote_api.py`、`test_boundary.py`、`test_governance_execution.py` 合计 167 通过，无跳过。32 条 datetime.utcnow 弃用告警来自固定上游生产力存储。
- 首轮 3 项失败中，两项为新独立上游缺内置 embedding 资产，另一项误把可脱敏的显示标签当作禁止改写的记录身份；补齐并校验固定资产、把测试改为 record_id 泄漏断言后重跑。保留安全过滤与真实 SDK 断言，未用假成功替代。
- 验证采用合成提炼、临时存储与后端测试用静态资源；不是浏览器、外部模型、两机或 NAS 验收。以上结果对应读取接口接线；后续问答和页面组合单独验收。

## ADR-0058：项目问答与 Anthropic

- `test_console_qa.py`：使用真实两项目、来源、SDK、治理与加密模型存储，外部模型仅使用 MockTransport；禁止外部 socket。覆盖范围隔离、空结果／摘录、忘记、管理员／Origin、问题凭证拦截、中文标点、模型错误、配置／记录变更复核及完成前无正文、遗忘完成后无后续旧片段。
- 2026-11-10：专项 11 通过；与读取和忘记专项最终组合 21 通过，无跳过。Core 与 Web 的对应补丁测试及安装限制见[项目控制台记录](../../../docs/reviews/2026-11-10-project-console.md)。
