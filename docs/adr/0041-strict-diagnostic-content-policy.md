# ADR-0041：Hub 诊断只输出封闭元数据

日期：2026-11-10。状态：本地实现、独立审查及本地/NAS 隔离安装验收完成，见[验证记录](../reviews/2026-11-10-diagnostic-content.md)。关联 FR-10、FR-11、ENG-01；承接 [ADR-0040](0040-diagnostic-write-quiescence.md)。

## 问题、复用与边界

固定上游的非 debug 投影允许任意字典键、reason/error/source/owner、标识后缀及伪造 redacted 占位符带出正文；直接 JSONL 调用、text add 的 INFO 和异常格式化还有其他出口。已有 0017 不能构成“关闭 debug 后无正文”的保证。

继续复用原 pipeline wrapper、SQLite/JSONL writer、logging handlers 与 Core 日志级别控制，增加共用的内容策略，不建立第二套日志存储。SDK MemoryInspector 的默认正文接口保持兼容；Hub/Core 现有 HTTP 没有暴露该原始接口，桌面流水线面板只依赖步骤、时间、计数和请求标识。业务原文、实际模型输入、记忆产物及治理证据不属于诊断投影，仍按原合同保存。

## 决定与数据流

为 Hub 显式启用版本化的严格元数据策略；普通 SDK 默认仍为 legacy。策略在创建日志资源前绑定，不能由 debug、环境变量的后续修改或重复 setup_logging 降级。严格模式遇到不受支持的策略/资源组合应在业务开始前明确拒绝，不能悄悄回退。

原生接口为 enable_strict_diagnostics()、strict_diagnostics_enabled()、StrictDiagnosticFormatter 与共用 pipeline 投影。激活是进程级单调操作；活跃 legacy cache/writer/Core 诊断队列由轻量弱引用登记，检查与新建登记共用锁。存在活跃旧资源时，激活明确拒绝，不做部分策略升级；资源成功关闭后解除登记。已初始化的纯 logging handler 可直接加固。Hub 仅使用 SQLite；未审计的其他诊断后端在 strict 模式拒绝创建。

handler 创建/挂载与策略启用共用锁，避免严格检查之后才挂上旧 formatter。升级既有 handler 前，必须一次性非阻塞取得其锁；任一 handler 正在 emit 或本线程已持有其锁时，返回固定 logging-active 错误，不改变原 formatter 或策略。旧输出结束后可重试。不得持策略锁阻塞等待旧 formatter 的任意回调，以免锁反转；全部加固后才公布 strict。已开始的旧输出不是新策略可以撤销的字节。

```text
Hub 启动 / 创建服务
  -> 安装受控输出处理器，启用原生 strict-v1 策略
  -> 原 SDK / Core 初始化
      -> pipeline wrapper: 封闭投影 -> 已有后台任务
          -> JSONL writer: 再次同一投影 -> 文件
          -> SQLite cache: 再次同一投影 -> 诊断表
      -> SDK / 第三方 / Uvicorn logging
          -> 受控 formatter: 固定类别、代码位置、时间、级别 -> 文件/标准输出
      -> Core DiagnosticLog（仅显式启用时）
          -> 同一封闭内容策略 -> 原有队列/文件
  -> debug 切换只改变级别及安全细节的数量，不开放正文
```

投影保留原外层结构，prompt/response 为空，明确包含策略版本和 content_omitted 标志。parsed 仅保留代码列明的键、闭集枚举、有界有限数值和布尔值；未知键名与字符串值一起舍弃。请求标识仅接受 Hub 生成的规范 UUID 组合或 SDK 内部 UUID；来源路径、项目名称、agent/session 等自由字符串不能凭后缀或长度通过。必要的关联标识采用明确的受限编码或不含原文的稳定投影，不将语法有效等同于治理授权。未知步骤归为固定 unknown，不能原样输出自由文本。

不使用字符串前后缀识别“已脱敏”，不以 blacklist 或递归任意键保留替代封闭 schema。投影须幂等、有输入/嵌套/集合预算，不调用不受信对象的 str/repr。非法类型、非有限数字、超限输入输出固定缺省元数据，不把失败输入写入异常日志。

严格 formatter 不调用 getMessage、formatException、str(error)，不复制 exc_text、stack_info 或任意 extra。保留固定日志来源类别、数值级别/时间、受控代码定位及安全异常类别；原始消息、请求 URL、headers 和第三方响应正文一律不参与渲染。formatter 失败、stream.write/flush 失败时也不得通过 logging.handleError 输出原始 record；使用受控处理器及独立进程的 logging.raiseExceptions=False，并验证坏输出流。Core 异常报告严格分支在构造 payload 前即省略原异常消息，不能先执行任意 __str__ 再丢弃结果。

Hub 的服务入口明确使用 Uvicorn log_config=None，避免服务器重新安装裸输出；只给 root 加 filter 或关闭 access_log 不足。SDK 三个自有 handler 同样受策略保护。Hub 现有 create_hub 不经过 Core CLI，不自动创建 core.log/diagnostic-core.jsonl；但显式启用 Core DiagnosticLog 或 start_logging_to_file 时，也必须遵循严格策略。

Hub 是拥有进程日志配置的独立服务；create_hub 直接调用同样接管该进程 Python logging，不支持与不同日志策略的应用混用。先运行仅依赖标准库的 bootstrap，再导入 SDK/Core 与业务模块并激活原生策略。分离已有调用者 handler，但保留对象且不关闭文件；保留带明确所有权标记的 SDK/Core handler 并加固，其他 logger 统一转到受控 root。安装安全 lastResort，重复初始化不增加 handler。之后另行安装自定义输出不属于该合同；测试在隔离作用域恢复调用者状态，不能以关闭调用者文件实现通过。

原生激活 API 的原子拒绝以调用该 API 时的策略/formatter 为准。Hub 在此之前已经安装无正文 bootstrap；服务初始化若被拒绝，这层输出保护和调用者 handler 分离仍保留，不自动重新开放原始输出。此时没有接纳 Hub 业务，操作者可结束旧资源后重试服务创建。

独立 CLI 的普通启动异常、warnings、线程及 unraisable 异常统一送受控日志；嵌入式调用不接管宿主的全局异常 hooks。Core asyncio strict 报告不再调用可能直接输出原始 context 的旧自定义 hook。直接 print、第三方原生进程或 C 层输出不由 Python formatter 自动保证，固定调用链须另外检查，不宣称任意可插拔代码全受保护。

## 兼容与失败行为

不改变数据库版本、治理意图、签名、许可、业务队列或产出摘要。新诊断行维持字段类型；原始正文 Inspector 在 SDK legacy 下保持原行为，严格 Hub 返回明确省略的诊断字段。日志级别接口继续工作。旧日志仍可读取，不能凭新策略标记已经清理。

SQLite memory_operations 的 content/reason 同样省略；system_metrics 保留 sys1/sys2 started/completed/failed、vdb/graph 操作计数与 sum_ms，以及原有两类 timing_sums 的闭集。分钟键只允许规范日期时间，合并旧行后再次投影；不修改 write_outcomes、episode_state 等业务恢复内容。可选旧 RequestTracer 直写文件入口在 strict 下明确拒绝，不关闭默认的安全 pipeline 诊断。

旧 tracer 的 span/finish(False) 在进入受控 sink 前仍可能对调用者对象作 str/repr；这沿用原返回/异常行为，不在本补丁改写其业务 trace 结构。严格投影、formatter 与 Core 异常处理自身不执行这些方法；不能把此保证扩称为全部上游调用链均不预渲染任意对象。

Core 结构化 sink 的 strict 入口当前直接投影并入队，未复用旧采样的自由 source/state 组合键。因此显式启用该 sink 后，原三类高频事件会逐条输出，暂不生成旧 300 秒窗口的聚合 count/summed_ms。原队列容量、丢弃报告、轮转、总字节和过期清理仍保留；Hub 默认未配置这个独立 sink。正式启用为日常运行前，应以投影后的封闭键补回采样，并验证计数/耗时聚合；不能宣称严格模式与旧模式日志量相同。

策略激活必须先于加载业务数据；初始化失败保留明确且无正文的错误。缓存资源不能在策略不一致时重复使用。已排入的旧任务不靠重新设置环境变量处理，使用 ADR-0040 的真实任务排空与生命周期边界。

回滚到旧镜像会重新启用旧日志行为；未来正式升级文档必须标明这一能力差异。旧文件、读请求副本、日志恢复重放和 Docker 已保留 stdout 是仍需完成的后续治理工作，本项不授予整目录删除权、不缩减彻底删除要求。

## 验收矩阵与实现分工

| 路径/失败场景 | 必须验证 |
| --- | --- |
| 自由键、后缀值、伪造占位符、混合嵌套、异常对象 | 合成正文不进入严格投影；不执行任意 str/repr |
| 原生 wrapper、直接 JSONL、直接 SQLite | 同一封闭策略，debug 开/关均不输出正文；正常步骤/耗时/计数仍存在 |
| SDK legacy | 原对应日志、Inspector、级别与 pipeline 回归保持兼容 |
| SDK setup 已执行、重复创建、debug 来回切换 | 无意外降级、无重复 handler，关闭与重试仍正常 |
| root、SDK 自有、第三方、Uvicorn 异常 | 实际输出文件/流没有合成正文，仍能定位错误类别与代码 |
| 真实 HTTP 写入/检索/治理 | 原文与记忆业务结果保持；诊断投影生效；ADR-0040 等待真实 I/O 的回归保留 |
| 安装与 NAS 隔离实例 | 固定补丁实际安装，合成数据正反例与错误路径通过；不冒称常驻升级或正式客户端验收 |

原生策略与其回归由独立私有上游副本实现；Hub 启动接线、协议/设计文档由主工作区负责，另一路进行只读设计/差异审查。旧文件清理继续独立准备精确请求归属、原子重写/句柄重开、签名恢复与验收，不与本补丁混称完成。执行对应上游/Hub 测试、make check 和安装探针后补实际结果；未经执行的项目不计通过。
