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

首个容器镜像的直接 CLI 验证实际失败：虚拟环境从构建目录复制到 `/opt/cmh/venv` 后，脚本 shebang 仍为 `/workspace/apps/hub/.venv/bin/python`，容器返回 no such file or directory。修复为在构建阶段就安装到最终 `/opt/cmh/venv` 路径，并增加构建时实际 CLI help 检查；最终镜像还要运行完整发送探针，不以构建时检查代替。原失败输出保存于 `.local/connector-transport-cli-image.log`，有问题的 `dev-f209249` 未传输或部署到 NAS。

`make check` 的 19 项工程门禁及格式、静态、文档链接、路径、源码凭证扫描、空白检查通过。日志为忽略目录 `.local/connector-transport-tests.log`、`.local/connector-transport-upstream.log`、`.local/connector-transport-check.log`。

## Linux amd64 容器证据

修复后的运行源码 `9bbb834` 构建为 `cloud-memory-hub:dev-9bbb834`。完整容器探针通过：禁网模型摘要／实际编码、初始化与认证、模型凭证密文、设备身份，以及真实安装的 CLI send 在重启后重放此前丢失回执的事件。确认本地 accepted 后上游仍只有一个任务；来源撤销后新增待传记录，CLI 返回固定权限错误并保留 queued，服务端队列不增加。再次重启后记录和撤销均保留。

探针实际调用 `/opt/cmh/venv/bin/cloud-memory-connector`，覆盖了最终镜像中的脚本入口与依赖路径，不只是在 Python 中导入发送函数。结果为 `.local/connector-transport-container.log`。模型未激活，不能把接纳状态算作提炼完成。

镜像归档已在 NAS 校验并加载。使用 `DOCKER_HOST=ssh://nas` 运行同一套探针，所有阶段通过，独立随机项目及卷随后清理，结果为 `.local/connector-transport-nas.log`。这里的 CLI 与 HTTP 服务均在 NAS 测试容器中运行，不能据此宣称 Mac 到 NAS 的正式 HTTPS 网络或真实 Agent 已验证。制品信息如下：

| 项目 | 值 |
| --- | --- |
| 运行源码／OCI revision | `9bbb834` |
| 归档字节数 | 261035520 |
| 归档 SHA-256 | `3643d3917fc8efc6c08d8d8fb46bcb802da72cb53110adb47350d4b37353eaac` |
| OCI manifest-list | `sha256:2990933b04efdb01053578b702307beee1155bf016daaba430616427d62ec3be` |
| NAS 加载后的镜像配置摘要 | `sha256:47bf5138d92c83ca762f01f7b28398948c5bff603dcd4d62b0a99eee756b58ab` |

## NAS 实验实例更新

独立项目 `cmh-nas-validation` 升级前停止服务，保存并校验 config／data 的 0600 私有归档。完成 `fd7b5ad → 9bbb834 → fd7b5ad → 9bbb834`；每个阶段健康，管理员凭证和加密密钥摘要、实例 ID、SDK 权威状态、空记忆库及未配置模型状态均一致。未创建设备授权或覆盖权限库。该结果只覆盖空实验实例的版本兼容，不能代替真实内容和治理状态的完整恢复演练。

最终镜像固定为 `cloud-memory-hub:dev-9bbb834`，配置摘要与归档一致，3 GiB／2 CPU、只读根目录及回环端口保持不变。日志为 `.local/connector-transport-nas-upgrade.log`，旧镜像及私有备份保留。没有修改 SSH 或现有 Agent 配置。

## 待完成范围

当前为开发者自审，未完成独立同行审查。NAS 正式 HTTPS 入口、自动配对界面、自动来源采集、授权检索与缓存、客户端下一轮注入、删除水位／防复活和完整备份仍需开发与验收。单次接纳、持续发送和该批测试均不代表跨设备记忆完整可用。没有修改现用 Agent、SSH 设置或真实模型配置。
