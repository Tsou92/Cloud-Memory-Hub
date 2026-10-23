# 连接传输与自动补传验证

日期：2026-10-24。分支 `codex/connector-transport`。关联 FR-02／03／04／06／07、ENG-01；完整需求仍未验收。设计与上游复用证据见 [ADR-0007](../adr/0007-connector-transport.md)，配置见 [连接组件说明](../local-connector.md)。

## 实现和兼容性

直接调用上游 `one_memory.http_client.create_async_client`，没有新增 HTTP 库；将已锁定的 HTTPX 0.28.1 声明为直接依赖，其他锁定版本不变。连接组件新增私有连接配置、单次 send、持续 watch、有界响应读取、匹配回执确认和暂时失败退避。NAS 新增当前来源访问回执和 source_preflight capability，本地投递 schema 与 NAS 数据 schema 均未变。

旧 init／enqueue／status 参数继续有效。连接文件给定身份必须与现有队列一致；旧 NAS 缺少来源预检能力时停止，不退回未经预检的上传。持久事件不会因响应丢失、错误回执、配置失败或中断而被删除。

## 本地证据

- `make check-runtime`：118 项通过，其中新增 38 项传输用例／参数化场景。真实回环 TCP Hub 使用完整 Core 生命周期；没有真实模型或用户记录。
- 通过真实 HTTP 保存事件后主动丢失响应，确认本地仍 queued、服务端只有一个任务；重新打开队列补传，回执匹配后 accepted，仍只有一个任务。截获正文无合成密码或设备凭证。
- 真实 HTTP 暂时返回 503，watch 保留队列并自动重试，恢复后实际接纳；测试替换等待函数以缩短等待，退避数值范围另有断言，不作为真实断网时长或一分钟 SLA 证据。
- 来源撤销、项目不匹配、写权限移除、其他设备来源、设备撤销和实例误配均在出站正文前失败；上游接纳端仍复核权限。已开始的网络请求不能由预检撤回。
- 使用实际安装的 `cloud-memory-connector` 进程执行 init／enqueue／watch／status／send。watch 通过真实 TCP 接纳，Ctrl-C 返回 130，无堆栈或凭证输出；队列与 Core job 数量一致。
- 使用真实 TLS 自签证书服务验证证书不受信任时拒绝，服务器未收到 HTTP 请求。未关闭 TLS 校验。重定向、超大／压缩／慢响应、重复 JSON 键、错误回执、旧能力／异常握手、私有文件类型／权限均覆盖。
- 固定上游 `test_loopback_proxy_bypass.py` 与 `test_runtime_client.py`：9 项通过，原测试未改动。

自审发现私有配置普通 open 可能在 FIFO 上先阻塞、无法走到类型校验。改为非阻塞打开再 fstat，新增 FIFO 拒绝用例通过。源码扫描曾将合成 Basic Auth URL 误报，测试改为运行时生成样例；没有改扫描器或增加豁免。

`make check` 的 19 项工程门禁及格式、静态、文档链接、路径、源码凭证扫描、空白检查通过。日志为忽略目录 `.local/connector-transport-tests.log`、`.local/connector-transport-upstream.log`、`.local/connector-transport-check.log`。容器／NAS 结果在执行后补充。

## 待完成范围

当前为开发者自审，未完成独立同行审查。NAS 正式 HTTPS 入口、自动配对界面、自动来源采集、授权检索与缓存、客户端下一轮注入、删除水位／防复活和完整备份仍需开发与验收。单次接纳、持续发送和该批测试均不代表跨设备记忆完整可用。没有修改现用 Agent、SSH 设置或真实模型配置。
