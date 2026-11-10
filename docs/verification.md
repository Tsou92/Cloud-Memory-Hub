# 验证计划与需求追踪

2026-11-10 当前主线补充：按 [ADR-0065](adr/0065-one-link-remote-agent-onboarding.md)，
主体运行在 NAS Docker；电脑默认使用一次性链接、一条命令或 Agent 安装指令添加轻量
远程 MCP／Skill，无需另装图形 App。旧 JSON、stdio 与采集连接器保留为高级入口；
远程 MCP 不提供本机离线队列。用户已要求后台视觉风格后续再调整，本轮仅处理必要功能、
接入体验、状态与错误恢复，不以新增视觉方案扩大验收范围。

本轮分项结果：网页 17 个文件、339 项测试及类型检查／构建通过；轻量配置器独立
标准库测试 34 项通过。独立审查确认并修复了提案生成与计划保存间的配置并发覆盖，以及
公开 `curl | sh` 命令下载失败却返回成功的问题，确定性复测保留用户新修改，并核对完整
下载失败命令返回非零且不执行。PowerShell 与缺 Python 环境的对应 CI 仍须单独验证，
不能将平台跳过计作通过。分组证据及真实宿主边界见
[一次性接入记录](reviews/2026-11-10-one-link-onboarding.md)，关联 FR-01、FR-02、
FR-03、FR-05、FR-06、FR-07、FR-12、ENG-01。本轮完整回归暴露的启动顺序及错误传播问题已修复，定向 45 项通过，完整复验进行中；
尚无最终发布绿灯，以下旧验证记录保留原时点与路径边界。

最新远程 CI：`e4d3fb0` 的[运行 42778272106](https://github.com/Tsou92/Cloud-Memory-Hub/actions/runs/42778272106)
两项 quality 通过；Hub 2,163 项通过、6 项跳过、1 项失败，失败发生在 HNSW `length.bin`
残留扫描。本地固定 Linux 镜像已复现原文字节位于预分配未使用区；新扫描只排除完整布局
验证通过的未用区，已用区、跨边界及未知布局仍须失败。CI 没有保留原文件，不能声称已验证
原 CI 的残留偏移。分类修正、原始失败和已接受限制见
[HNSW 扫描记录](reviews/2026-11-10-hnsw-residue-scan.md)；它不代表磁盘物理擦除完成，
也不替代本轮最终目标版本的完整远程门禁。

2026-11-10 首次网页使用与新手上手：独立空卷实例实际完成网页登录、项目／设备创建、
下载配对与 preview.4 客户端导入、合成模型自动列表和连接测试、网页备份、双项目摘录
问答与普通忘记。补丁 0035／0036 的 306 项网页测试、固定 Linux Docker web-tests、
类型检查、构建及真实桌面／390 像素浏览器复验通过；细节与合成边界见
[首次网页记录](reviews/2026-11-10-browser-first-use.md)。当前真实 Codex 桌面对话也完成
preview.4 Skill／CLI 与独立 MCP 进程的接续读回，非原生 MCP 热加载，见
[客户端记录](reviews/2026-11-10-self-service-clients.md)。最终发行镜像与远程 CI 仍单独验证。

2026-11-10 Linux 来源授权补充：真实远程 CI 暴露目录 inode 复用，NAS 原镜像重现失败；
补丁 0034 增加根目录创建时间与授权对象身份绑定，并保留旧授权的明确确认要求。
最终源码覆盖层在 NAS 的 166 项定向测试和同卷跨容器重开通过，2 项原有文件级测试跳过。
设计、原始失败与最终完整门禁状态见[来源目录授权记录](reviews/2026-11-10-source-root-generation.md)，
关联 FR-07、FR-08、ENG-01，不代表最终镜像或远程 CI 已通过。

2026-11-10 冷环境与候选客户端补充：首次真实 GitHub CI 暴露 Core runner 加载未打补丁的 SDK，原始失败、保留原测试集的修复和复测结果见[冷环境 CI 记录](reviews/2026-11-10-cold-core-ci.md)，关联 ENG-01。干净源码 `c872037` 的 preview.4 安装包在两台 arm64 Mac 完成真实 MCP 原文／交接共享 6 项与撤销后 4 项检查，关联 FR-01、FR-02、FR-04、FR-12；[客户端记录](reviews/2026-11-10-self-service-clients.md)保留原 shim 安装失败与修复证据。双机 MCP 成功不代替模型自主调用、自动采集或登录重启。

2026-11-10 受控备份恢复补充 NAS 真机证据：固定镜像源码 `ae9cb2e` 的正常恢复 11 项与单次真实 `SIGKILL` 中断恢复 7 项阶段检查通过，关联 FR-11、FR-13、ENG-01 及 R3-02／R3-03。覆盖空三卷导入后的非空业务／身份／配置读回、历史回滚保留较新普通忘记和设备撤销、损坏归档拒绝，以及复制途中实际杀进程后的启动／备份拒绝和保护版本空卷恢复。独立项目事件始终保留为 `pending`，不计作模型提炼成功；两组隔离资源均已清理。镜像、步骤、保护性拒绝和未覆盖断电／硬盘故障的边界见[NAS 恢复演练记录](backup-and-recovery.md#nas-恢复演练记录)，不代替最终发行制品或两机宿主验收。

2026-11-10 MVP 主线新增通用 Agent 入口验证：`test_agent_access.py` 使用真实 Hub HTTP 验证跨设备接纳原文、项目隔离、过滤和交接；`test_agent_mcp.py` 使用官方 MCP 客户端与真实子进程验证初始化、工具调用、两设备立即读取、完整交接及安全错误。关联 FR-01、FR-02、FR-03、FR-05、FR-07、FR-12；设计见 [ADR-0049](adr/0049-mvp-agent-access-and-handoff.md)，使用方式见[Agent 接入](agent-access.md)。单机双设备身份不代表两台 Mac 或两个真实宿主已完成验收。

2026-11-10 四域覆盖增加 `test_core_records.py`、`test_core_file_inventory.py`、`test_core_file_execution.py` 及 `test_core_coverage_capability.py`、`test_core_coverage_store.py`、`test_core_coverage_binding.py`、`test_core_coverage_activation.py`；原文件安装探针增加真实停服激活、Core 原生严格输出、跨进程四域恢复及无授权正文拒绝。实际旧镜像还需对激活前对照可读、对新能力精确拒绝并保持文件/数据库不变。关联 FR-10、FR-11、ENG-01，当前实测与未完成范围见[记录](reviews/2026-11-10-core-file-coverage.md)及[ADR-0047](adr/0047-core-file-coverage-and-physical-authority.md)。

2026-11-10 正式恢复接线继续增加 `test_execution_binding.py`、`test_repair_source_rules.py`、`test_authority_execution_binding.py`、`test_diagnostic_requests.py`、`test_diagnostic_review.py`、`test_core_diagnostic_maintenance.py` 及 `make probe-core-diagnostic-maintenance`；原文件安装探针增加真实 PTY 签署与新进程重试。关联 FR-10、FR-11、ENG-01，当前边界和证据见[记录](reviews/2026-11-10-runtime-binding-and-core-maintenance.md)与[ADR-0046](adr/0046-governance-runtime-and-output-coverage.md)。完整运行门槛和输出域仍须继续接通。

状态：完整产品用例尚未通过。工程工具、认证边界、凭证存储、Core 集成和容器验证结果单独记入[运行时记录](reviews/2026-10-22-runtime.md)，不能代替六入口与 NAS 真机验收。

## 需求追踪

| 编号 | 需求来源 | 核心验收 | 验证层次 |
| --- | --- | --- | --- |
| FR-01 | 需求 §1、§6 | 跨设备任务接续；背景、决策、进度与下一步有来源 | 质量评测＋真机 |
| FR-02 | §2 | 统一双向读写；额外的参考适配器无需改核心 | 契约＋端到端 |
| FR-03 | §3 | 全局偏好共享、项目默认隔离、明确跨项目请求 | 权限单元＋集成 |
| FR-04 | §4 | 正常联网，本轮结束至另一端可用不超过 60 秒 | 性能＋两机真测 |
| FR-05 | §4 | 已开对话的下一轮使用新记忆，无需重开 | 六入口真机 |
| FR-06 | §4 | 离线可读可记；重连自动补传、重复与冲突可控 | 故障注入＋真机 |
| FR-07 | §5、§10 | 文字／附件凭证在任何出站请求前被排除 | 隐私样例＋出站截获 |
| FR-08 | §6 | 两机历史全量导入、重复导入幂等、来源可查 | 集成＋负载 |
| FR-09 | §6 | 长期保存；明确纠正优先，未决冲突可见 | 治理单元＋集成 |
| FR-10 | §6 | 忘记与彻底删除行为不同，所有派生内容受约束 | 状态机＋集成 |
| FR-11 | §6、§8 | 重连、重导入和旧备份恢复都不自动复活 | 恢复演练＋故障注入 |
| FR-12 | §7 | 网页与 Agent 均可搜索、追溯、治理和查看同步 | UI／MCP 端到端 |
| FR-13 | §8 | 自动版本备份、回滚、完整导入导出 | 真实存储恢复演练 |
| FR-14 | §9 | 网络可达；初次导入与日常模型用量分开统计 | 网络验证＋账目核对 |
| FR-15 | §10 | 图片、PDF、附件、设计稿有效信息可检索追溯 | 解析契约＋质量评测 |
| ENG-01 | §13 | 上游来源／测试保留、增量补丁有据、可复现构建 | PR 审查＋CI |

## 数据流和失败覆盖

```text
授权来源
  ├─ 不允许／已撤销 -> 拒绝，无内容出站
  └─ 读取 -> 解析／过滤
       ├─ 失败／无法判定 -> 本地隔离
       └─ 安全事件 -> 本地队列
            ├─ 离线 -> 缓存可用、队列保留
            └─ 发送 -> 认证／项目／删除状态检查
                 ├─ 不允许 -> 拒绝或抑制，审计不含正文
                 └─ 幂等落库 -> 确认游标 -> 提炼任务
                      ├─ 崩溃／重试 -> 不丢失、不重复发布
                      ├─ 模型异常 -> 可见失败、保留可重试状态
                      └─ 治理核验 -> 发布 -> 他端缓存／检索
                           ├─ 宿主不支持自动注入 -> 明示部分支持
                           └─ 下一轮注入 -> 来源与实际回答核验

忘记／删除 -> 各存储和队列清理／抑制 -> 离线重连／导入／恢复 -> 防复活检查
```

不能用纯模拟替代模型质量或实际宿主行为，也不能让昂贵模型调用成为普通单元测试的必需条件。CI 使用合成数据、隔离存储和受控模型替身；真实模型与客户端在专门验证阶段执行并记录成本。

## 关键实验

2026-11-03 普通记忆合并来源新增 test_memory_evidence.py、test_memory_graph.py 和 make probe-memory-graph，覆盖跨会话原地更新的历史版本、真实来源、方向性范围、故障补档、兼容性和预算；关联 FR-08、FR-09、FR-10、FR-11、FR-12，见[安装与 NAS 记录](reviews/2026-11-03-memory-lineage.md)。完整输入覆盖和删除执行仍待完成。

2026-10-30 普通聊天历史增加 `test_chat_history_capture.py` 和 `make probe-chat-history`，覆盖真实选择窗口、跨节点片段／姓名来源、不可变快照、故障／预算、SDK 持久结果重开及幂等重放，关联 FR-08、FR-09、FR-10、FR-11；见[SDK 验证](reviews/2026-10-30-chat-history.md)。随后增加 `test_chat_graph.py` 和 `make probe-chat-graph`，核验原文对应、权限边界、事务补档与预览方向；见[Hub 接线记录](reviews/2026-10-30-chat-lineage.md)。其他聊天输入和删除执行仍待完成。

2026-10-29 编程链观察增加 `test_chain_inventory.py` 并扩展 `probe_input_context.py`，覆盖真实轮次、线程、跨项目隔离、方向性范围及缺失证明拒绝，关联 FR-08、FR-09、FR-10、FR-11；见[验证记录](reviews/2026-10-29-chain-inventory.md)。

2026-10-29 合并输入增加 `test_reconcile_input_capture.py` 和 `make probe-reconcile-inputs`，覆盖实际候选快照、历史摘要／细节继承、异常／超限、并发隔离及 SDK 结果重开，关联 FR-08、FR-09、FR-10、FR-11；见[验证记录](reviews/2026-10-29-reconcile-inputs.md)。

2026-10-30 来源标记增加 `test_record_origins.py` 和 `make probe-record-origins`，覆盖同 ID 多轮写入、不可变来源、原生写入时间／文本归一化、直接修改清除标记及旧写入检测，关联 FR-08、FR-09、FR-10、FR-11；见[验证记录](reviews/2026-10-30-record-origins.md)。来源标记尚不等于完整 Hub 来源图。

2026-10-29 历史输入追溯新增 `test_sdk_context_capture.py`、`test_input_context.py` 和 `probe_input_context.py`，关联 FR-02、FR-08、FR-09、FR-10、FR-11。实际窗口、链提交、原子保存、跨域拒绝和跨进程来源读取的证据与剩余范围见[验证记录](reviews/2026-10-29-input-context.md)。

1. **上游基线**：固定提交和 SDK；保存原有测试名称、命令、版本、退出状态及失败归因。先测原版，再测补丁，区分既有失败和回归。
2. **真实下一轮**：在目标 B 预先打开会话；来源 A 产生合成项目事实；从完成时间 t0 到 B 可检索 t1，再在同步后的 B 下一轮核验实际注入版本和回答。保存脱敏证据。不得仅测一次 MCP 调用。
3. **时效与争用**：记录 t1−t0 的分位数和最大值，以及下一轮是否使用新版本。覆盖无积压、全量导入同时进行、模型限流、大附件、NAS 重启和真实网络。任何超过 60 秒的样例明确报告；阈值例外不可自行加入需求。
4. **同步状态机**：两端同时修订、响应丢失、重复消息、乱序、时钟偏移、游标损坏、设备撤销，检查来源和用户纠正优先规则。
5. **隐私**：合成密码、API 密钥、Cookie、私钥，放入正文、工具参数、日志、PDF 文本和图片；截获所有 Hub 出站请求及重试路径，验证不含原始凭证。无法解析的附件必须隔离。
6. **删除／恢复**：忘记后重导入、删除后离线旧端回连、恢复旧快照、索引重建、备份文件清理；测试最新删除记录丢失时禁止自动恢复召回。
7. **安装和升级**：已有 hooks／MCP 配置共存、用户编辑、卸载、升级失败回滚。任何安装操作先在隔离配置中验证，不直接改日常环境。
8. **恢复完整性**：在非生产数据上执行备份、破坏、恢复，比对证据、派生记忆、项目、权限、删除状态及引用；包含中途断电模拟、磁盘满及不兼容格式。

## 证据模板

每次实验记录：关联需求、代码提交、操作系统与应用版本、数据规模、网络条件、步骤、期望／实际结果、时间与模型用量、脱敏证据位置、未通过项和复测条件。

真机表初始全部为“未测”。CLI 的测试结果不能替代桌面入口，单机不能替代两机，公开文档不能替代运行证据。

## 当前工程检查

2026-11-10 副作用观察增加 `test_sdk_write_effect_observation.py` 与 `make probe-write-effects`：原生非空写入、业务结果遗漏的旧节点/画像、批量部分写、异常/取消、结果重开/重放、请求隔离、旧结果及字节预算。它只验证观察记录，不作为完整副作用证书。`test_purge_diagnostic_recovery.py`、`test_purge_history_recovery.py` 和治理安装探针补齐同请求诊断表首次/重试/仅缓存恢复、独立 history 恢复及无关行保留，关联 FR-10、FR-11、ENG-01。设计边界见 [ADR-0039](adr/0039-sdk-write-effect-observation.md) 和 [ADR-0035 补充](adr/0035-purge-cache-recovery.md)。

同日诊断后台写入增加 `test_diagnostic_write_quiescence.py`、`test_diagnostic_search_admission.py` 与 `make probe-diagnostic-quiescence`：真实 HTTP 删除等待 JSONL/trace、取消/超时后的重试、无关记录保留、签名操作不重复、搜索并发与关闭等待、空启动不创建原生存储。原生回归另覆盖 SQL 线程已运行后超时/取消、跨 loop 派发、异常消费、暂停租约和关闭失败重试。安装探针与 pytest 共用场景；模型提炼/embedding 为合成替身，真实 SDK/存储/HTTP 参与。关联 FR-10、FR-11、ENG-01，见 [ADR-0040](adr/0040-diagnostic-write-quiescence.md)。既有文件清理与所有副作用证明不由这些测试替代。

严格诊断输出新增 `test_diagnostic_content.py` 与 `make probe-diagnostic-content`：直接 SQLite/JSONL 保留计数与规范请求身份，SDK/Core 文件、第三方日志、坏输出流、实际 Uvicorn HTTP 500、CLI 启动错误及 debug 切换均不输出合成正文；调用者仅由 logger 持有的 FileHandler 仍保留对象和文件描述符。旧正文缓存恢复测试改用独立 legacy 子进程的原生 emitter，Hub 持续 strict，不把新元数据伪装成旧正文；省略两诊断表清理的负对照必须失败。原 HTTP 等待探针改用确切请求 ID 定位阻塞 I/O，并实际验证业务原文和记忆正文保留。关联 FR-10、FR-11、ENG-01，合同见 [ADR-0041](adr/0041-strict-diagnostic-content-policy.md)。

旧文件范围前置检查新增 `test_diagnostic_selectors.py`、`test_diagnostic_records.py`、`test_diagnostic_inventory.py`、`test_diagnostic_inventory_probe.py` 与 `make probe-diagnostic-inventory`：真实已接纳任务的无行写入证明、精确物理行 grants、原生三种旧格式及 strict 元数据、实际共根和分根布局、未选/读取/未知拒绝、路径/预算/并发变化、完整数据库与文件字节不变。关联 FR-10、FR-11、ENG-01，见 [ADR-0042](adr/0042-diagnostic-file-governance.md) 与[本步记录](reviews/2026-11-10-diagnostic-inventory.md)。这是只读前置检查，尚未接通物理清理、签名范围预览和恢复完成条件。

物理执行组件新增只读 Authority/Admissions/Ingest、精确确认、ExecutionStore2、实际文件替换、真实 handler gate 与停服 CLI 测试，以及 `make probe-diagnostic-file-execution`。覆盖原样/旧进度恢复、新 strict 尾行、正常轮转、真实进程中断、逐文件替换失败、阶段文件损坏、迁移提交间隙及实际旧制品 capability 拒绝。关联 FR-10、FR-11、ENG-01，见 [ADR-0043](adr/0043-diagnostic-physical-execution.md) 与[本步记录](reviews/2026-11-10-diagnostic-file-execution.md)。独立组件测试不得作为公开执行接口或全部副作用域完成证据。

运行时恢复隔离新增 `test_governance_recovery_fence.py`、`test_governance_fault_boundary.py`，并扩展既有 `probe_governance_execution.py`：真实已完成删除重试失败、精确恢复原生记录、正常 worker 排空、资源退出及取消竞争、许可不消耗、Core 正文与流式输出隔离、完整恢复再开放。关联 FR-10、FR-11、ENG-01，见 [ADR-0044](adr/0044-governance-runtime-recovery-fence.md) 与[验证记录](reviews/2026-11-10-runtime-recovery-fence.md)。

联合文件恢复新增 `test_diagnostic_recovery_graph.py`、`test_cleanup_contributors.py`、`test_diagnostic_recovery_files.py`、`test_diagnostic_legacy_preview.py`，扩展原 `probe_diagnostic_file_execution.py` 的多 repair、中断与跨进程重放。覆盖真实主链图、冻结授权、跨 scope 共根、全部旧 stage 先证明、旧格式与维护入口兼容；文件 verified 不等于主 purge complete。关联 FR-10、FR-11、ENG-01，见 [ADR-0045](adr/0045-diagnostic-recovery-graph.md) 和[验证记录](reviews/2026-11-10-diagnostic-recovery.md)。

2026-11-07 治理执行增加 test_governance_execution.py 和 make probe-governance-execution，关联 FR-09、FR-10、FR-11、FR-12。覆盖忘记的范围、抑制、幂等、崩溃续做和篡改拒绝，彻底删除的逐字节核验、无关数据保留、中断续做与恢复旧数据后重新清理；已知 HNSW 预分配缺口单独固定。见 [ADR-0032](adr/0032-governance-execution.md) 与[验证记录](reviews/2026-11-07-governance-execution.md)。

2026-11-09 稳定来源屏蔽补充：上述治理测试和跨进程探针新增原文删除后新修订被拒绝、队列未新增任务、恢复旧业务数据后规则仍有效、旧操作兼容和屏蔽行缺失拒绝。关联 FR-10、FR-11；设计与验收边界见 [ADR-0033](adr/0033-durable-source-suppression.md)。

2026-11-09 删除缓存恢复增加 test_purge_recovery.py，并扩展原中断测试和跨进程探针：原文已清除仍可定位 SDK 结果，后续输入快照的缓存副本同步清除，仅恢复旧缓存也重放，启动与后台恢复串行；关联 FR-10、FR-11，见 [ADR-0035](adr/0035-purge-cache-recovery.md) 和[验证记录](reviews/2026-11-09-purge-cache-recovery.md)。在途队列的 8 个预期失败继续单列，不计通过。

同日后续 [内容及任务协调验证](reviews/2026-11-09-content-quiescence.md) 新增内容规则/真实治理、底层 Drain 和 Hub 生命周期测试；G-B 两项已验证修复，预期失败减为 6 项。跨进程治理探针增加换来源与全部 ID、混合消息整事件拒绝，另有独立安装队列合同探针；各自的模拟范围和未完成清扫边界单列。

2026-11-10 在途执行增加 `test_governance_inflight.py`、`test_governance_inflight_integration.py`、`test_inflight_execution_recovery.py`、`test_purge_legacy_targets.py`、`test_purge_request_budget.py` 和 `make probe-inflight-governance`，关联 FR-10、FR-11、ENG-01。原 G-A/G-C 六个预期失败转为实际成功断言；新增非空 SDK 对照、同意图恢复、旧终态兼容、累计正文预算和独立进程仅队列恢复。当前证据及未覆盖旧／已许可任务的限制见[在途执行记录](reviews/2026-11-10-inflight-execution.md)。

同轮覆盖审计补充 `test_reaped_queue_vacuum_recovery.py`，固定已清理标记但未压缩的 Core 恢复：逻辑不存在仍须清理 SQLite 旧页，不以 `present=False` 跳过。旧治理安装探针修正初始化多余 pending 夹具，并核验同项目修订真实取消与合法的终态正文回收。

同日治理请求重试与实体恢复增加 `test_governance_request_identity.py`、`test_entity_inventory_guard.py`，并扩展安装治理探针的来源修订和同 UUID 重试验证。关联 FR-10、FR-11、ENG-01；请求冲突、日志前后中断、旧操作兼容、实体开关关闭后残留以及旧 planned/completed 操作恢复边界见[本轮记录](reviews/2026-11-09-governance-retry-and-entities.md)。

同轮补充 `test_forget_recovery.py` 9 项与 `test_productivity_forget_guard.py` 20 项：真实旧业务恢复后的下一轮 SDK 提炼输入不再使用已忘记普通原文；生产力范围暂不能停止内部合并输入，须在新执行、重试及旧操作恢复时明确拒绝。安装探针分别覆盖实体副本、生产力拒绝和跨进程组合恢复；保护性拒绝不计作 FR-10 完整能力通过。

2026-11-09 组合探针继续写入时发现 Chroma 日志最高序号/未消费删除丢失，新增 `test_chroma_purge_sequence.py`、`test_chroma_delete_replay.py`、`test_chroma_governance.py`，共 16 项通过；关联 FR-10、FR-11、ENG-01。实际 Core/SDK 读回、1000 节点跨进程索引重放、旧损坏状态拒绝、映射/集合/事务/预算及独立审查范围见[验证记录](reviews/2026-11-09-chroma-purge-sequence.md)。

2026-11-03 基础画像增加 test_profile_evidence.py 和 make probe-profile-evidence，关联 FR-08、FR-09、FR-10、FR-11、FR-12。覆盖原生差异/迁移、实际旧输入冻结、部分副作用、取消/故障/预算、结构化 KV 摘要、Chroma 空身份格式，以及只有画像产出的 SDK 跨进程持久结果与禁止写入重放。见 [ADR-0028](adr/0028-basic-profile-evidence.md) 和[验证记录](reviews/2026-11-03-profile-evidence.md)；尚不等于 Hub 画像来源图或删除闭环。

后续 Hub 接线新增 test_profile_archive.py、test_profile_graph.py 和 make probe-profile-graph，验证同作用域跨 Agent 的独立产出索引、原始来源、当前画像来源校验、事务中断/无重写补档与隔离；普通合并快照同时补齐本次聊天历史父边，避免遗漏快照中的派生内容。见 [ADR-0029](adr/0029-basic-profile-lineage.md) 和[记录](reviews/2026-11-03-profile-lineage.md)。不据此宣称完整输入时序、安全删除或恢复已完成。

2026-11-04 派生任务生命周期增加 test_derived_task_lifecycle.py 和 make probe-derived-lifecycle，关联 FR-02、FR-04、FR-10、FR-11、ENG-01。覆盖会话摘要与图投影的正常／失败／提前返回、重复取消、请求隔离、实际 SDK 回执和维护屏障、跨进程重开与禁止重新提炼的重放。生命周期收尾不等于摘要输入来源或删除范围已完整，见 [ADR-0030](adr/0030-derived-task-lifecycle.md) 与[记录](reviews/2026-11-04-derived-lifecycle.md)。

`make check` 运行仓库工具的格式和静态检查、门禁行为测试、文档相对链接检查、禁止入库路径检查、detect-secrets 扫描以及 Git 空白检查。凭证扫描禁止联网验证候选凭证，检测输出只显示文件位置和类型。

该扫描只覆盖当前 Git 跟踪文件及未忽略的新文件，不是历史提交扫描或运行时凭证过滤。公开发布前另做完整 Git 历史与制品扫描。早期记录时远程 CI 尚未运行；后续实际结果见本文开头，不能由本机通过推定最终远程 CI 已通过。

2026-10-30 输入来源图增加 `test_reconcile_graph.py` 和 `make probe-reconcile-graph`，覆盖原文、精确类型产出与快照的同事务存档，连续更新／事实历史的阶段关系、无未来来源、自引用／循环防护及完成任务补核对；关联 FR-08、FR-09、FR-10、FR-11，见[验证记录](reviews/2026-10-30-reconcile-graph.md)。
