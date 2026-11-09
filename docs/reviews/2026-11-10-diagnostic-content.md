# 严格诊断输出验证记录

日期：2026-11-10。状态：源码、独立审查、本地/NAS 隔离安装及本地 Compose 验收完成。关联 FR-10、FR-11、ENG-01；设计见 [ADR-0041](../adr/0041-strict-diagnostic-content-policy.md)。本轮不升级 NAS 常驻实例。

## 改动与兼容

0023 在固定上游 20 个补丁之后增加 Hub 显式启用的 strict-v1。复用原诊断存储、wrapper、处理器与队列，在 SQLite/JSONL/Core 文件入口作封闭投影，Python 日志使用无正文 formatter；SDK 默认 legacy 不变。debug 可以保留更多安全诊断，但不能打开提示、响应、异常消息或任意自由字段。业务原文、提炼记忆、模型实参与写入恢复证据维持原合同。

Hub 在导入业务前接管进程 Python logging；分离调用者 handlers 并保留强引用，避免最后一个 logger 引用释放后间接关闭文件。Uvicorn 使用明确的 log_config=None；实际 HTTP 500 和 CLI 启动异常分别验证。该工厂用于专用进程，不支持同时运行不同日志策略的应用。

## 缺陷复现与审查

此前 0017 可经任意键、reason/error/owner、标识后缀、伪 redacted 占位符、异常输出及直接 writer 调用泄漏。新投影不保留这些自由字段。原生/UI 消费关系已核对：业务原文不是诊断数据，不能为避免日志副本而删减业务保留。

独立审查与真实负例还发现并修正：

- 安全 formatter 遇到 stream.write/flush 失败，默认 logging.handleError 仍会输出原 msg/args；受控错误路径不再复制这些字段。
- 移开 FileHandler 后若不强持有对象，其文件描述符会被间接关闭。仅保留 weakref 和 fd 的负例已复现；修复后对象/fd 仍存在，原文件字节不变。
- handler 在严格判断与挂载之间可被激活穿插，导致 strict 已成功而文件仍用旧 formatter。创建/挂载与策略共锁后独立复放通过。
- 已开始的旧 emit 即使更换 formatter，仍可晚写正文。启用先非阻塞取得全部待升级 handler 锁，忙或同线程重入时原子拒绝；旧输出结束后重试成功。避免持策略锁阻塞等任意旧回调。
- Core 曾在 native policy 模块初始化中途读取到不完整接口。现在只使用已完成初始化的模块；期间创建的 legacy 资源仍被登记并阻止后续激活，关闭后可以重试。

旧正文恢复测试继续使用独立进程的原生 legacy emitter：逐字段确认旧库确有正文，只复制两张诊断表，其他 cache 表逐行不变。被测 Hub 始终 strict。省略两张诊断表清理的独立负对照三例全部失败，正常三例通过；没有把新元数据当作旧正文验收。

首轮 Hub 全量为 1110 通过、5 跳过、1 失败：旧观察测试使用自由请求标签，而新诊断正确省略它。测试现使用真实 SDK 规范 UUID，继续保留入队前观察、未完成后台写入、partial 状态及结果冻结断言，不放宽实现策略。

最终冻结源码上的 Hub 全量为 **1111 通过、5 个可选后端跳过、0 xfail**，190.99 秒；290 条既有弃用/语法告警单独保留，不计失败或修复。日志为 `.local/diagnostic-content-final-hub.log`。独立源码探针三个 phase 全部通过（logging、persistence、server），`make check` 包含 19 项工程测试、格式、代码检查、路径链接、敏感信息及 staged diff 检查，通过；未执行的 NAS 完整 Compose、双镜像迁移及真实客户端验证不计通过。

原生 SDK 148 项通过（新增 30），覆盖 diagnostic_content_policy、log_setup、log_quality、pipeline_observability、pipeline_log_lifecycle、shared_runtime、async_executor、sqlite_connection_lifecycle、op_trace_logs、cache_backend_parity 与 write_outcome_cache。Core 39 项通过（新增 6），覆盖 diagnostic_log、hy_runtime_config、log_file、log_privacy；Core 索引保持 341 个测试文件。没有跳过或 xfail。Core legacy 日志 HTTP 回归仍保留，不将严格模式改成 SDK 默认。

测试环境使用既有 Hub Python 3.12.9；SDK 经独立 21 补丁源码及已缓存 pytest_asyncio 执行，Core 经上游官方隔离 wrapper 运行，避免两个 tests 包冲突。完整命令记录于私有 `.local/diagnostic-content-test-commands.txt`，结果为 `.local/native-content-final.log` 与 `.local/native-content-core-final.log`。没有安装或升级依赖。最终补丁规范化 70 个空上下文行，并裁去一处包含上游空白缩进的多余 hunk 上下文；不修改已测源码。SHA256 为 `3d85f39dd5708e24d064e4b4d23c3dfe05b7f123f1c2db867214fbe15c7c0700`；旧 20 补丁副本正向检查、新副本反向检查均通过。

## 验证范围与后续

固定制品来源为 `c2242fa7490ecd893b69666a4babac503e2d2c93`，镜像 `cloud-memory-hub:dev-c2242fa`、Linux amd64。全新应用 21 个补丁后的 13 个文件与已测副本逐字节一致；镜像内 11 个 native/Core 及 3 个 Hub 生产文件逐项 SHA 验证，均来自 site-packages。

| 制品身份 | 值 |
| --- | --- |
| Image config digest | `sha256:cee4b302ced75cca05a06e04419c3f6fe1749d9a0de36dd853365a0431b89fb9` |
| 本地 OCI index | `sha256:5fa65a4a1ace02f67cd107d3d3164d517537535e9511543cf5391d76d20662e6` |
| 导出归档 SHA256 | `7574fa3d152d2b7954b6521abb1024f772651eceda1b31ceacf2ac58b13a5b4b` |
| 归档字节数 | `261315072` |
| 21 补丁 manifest SHA256 | `bde8f9c668905e8a9511ef2f41ecb5eb0a1ad0adc593a73e408ab2177e17eb27` |

本地 9 项隔离安装探针全部退出 0，包含 diagnostic_content、diagnostic_quiescence、write_effects、governance_execution、drain_quiescence、entity_inventory、productivity_forget_guard、chroma_delete_replay 与 inflight_governance。实际容器身份、只读、禁网、drop ALL、2 CPU／3 GiB 和临时空间约束均核验。完整本地 Compose smoke 同样通过（71.195 秒），使用固定提交导出的输入与独立随机项目，覆盖真实 CLI 初始化、停服维护、重启及授权/撤销持久性；仅清理本次创建的资源。NAS 使用同一校验归档，9 项探针同样全部退出 0，总计 205.332 秒；NAS 安装身份、14 个生产文件及实际容器约束复核通过。两端均无失败或跳过，未在本轮执行双镜像迁移或 NAS 完整 Compose smoke。

NAS 常驻空实验实例仍为 `cloud-memory-hub:dev-c0467f5`；前后容器 ID、StartedAt、image config、运行与 healthy 状态完全一致，未升级或重启。未修改 SSH、日常 Agent 配置或真实历史，未调用外部模型。完整证据位于私有 `.local/diagnostic-content-install-prep/revisions/c2242fa7490ecd893b69666a4babac503e2d2c93/isolated-verification.json`，含源文件核验、各阶段/探针退出码及逐项日志索引。

新增探针包含 SDK/root/第三方/Core 文件输出、损坏输出流、异常 hooks、debug 往返、直接原生 SQLite/JSONL、真实 Uvicorn HTTP 500。原 HTTP 等待探针按精确 request_id 阻塞实际 I/O，并额外核验归档原文与 SDK 提炼记忆仍可完整读取。提炼/embedding 仍是合成替身，未调用外部模型。

独立边界探针覆盖四个原生 sink 及四次级别/格式/环境切换，未发现合成正文输出或投影调用未知对象 str/repr。旧 tracer 在受控 sink 前仍可能预渲染任意对象，其原 span/返回异常语义保留；strict 旧文件出口拒绝，不将这项有限保证扩称为全部上游预渲染均被消除。

独立性能窄探针确认一项可选 Core sink 差异：同键 500 次事件，legacy 采样落 1 行 311 字节，strict 落 500 行 161500 字节；strict 不输出旧窗口聚合。当前 Hub 未默认配置该结构化 sink，因此没有据此认定默认服务回归，也不宣称日志量相同。两模式的真实拥塞实验都保留队列/丢弃报告，缩小限额的真实文件实验确认轮转、总量与过期清理继续有效。后续正式启用该 sink 前需补封闭键采样；本微探针不代替 NAS 长期性能验收。

已有物理日志、独立读取/跨项目副本、Core 旧多行日志与 Docker 历史 stdout 仍未清理。下一项需要精确复用签名完整写请求、定向保留无关字节、处理器重开、受签名保护的临时文件状态及仅日志恢复重放。新的输出策略不能替代这些工作；完整副作用证书、六客户端和 G1–G5 其他退出条件继续保持未完成。
