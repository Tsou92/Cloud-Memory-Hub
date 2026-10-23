# 远程来源与持久接纳验证

日期：2026-10-23。分支 `codex/remote-ingest`。关联 FR-02／03／04／06／07／08、ENG-01；本批实现上传端，不代表对应需求整体通过。设计见 [ADR-0006](../adr/0006-remote-ingest.md)，接口见 [远程接纳合同](../remote-ingest.md)。

## 复用和变更

直接复用固定上游的 `EvidenceInput`、`materialize_write_input`、`WriteRequest` 和 `DurableWriteQueue`。补丁 0005 在既有来源判定函数中增加可选回调，Hub 只处理 `cmh:` 来源，未接管来源保留原授权。未增加记忆表或模型执行队列。

新增来源注册／撤销、HTTP 事件接纳和状态回执。权限库增加独立版本的来源／回执表，兼容设备身份库 v1；内容经过本地过滤，服务端复核后进入已有 Core 队列。项目 UUID 映射到独立 SDK user namespace，操作与对话保留来源身份及自由元数据。未知或超限内容拒绝，不截断。

## 已取得的本地证据

- `make check-runtime`：80 项通过，包括本批 9 个新增集成／合同测试。使用真实 Hub／Core 生命周期与 SQLite 队列；涵盖本地过滤入队到 HTTP 接纳、重复／冲突、跨设备／项目拒绝、重启持久性、并发撤销、扩展库未知版本、严格正文合同和认证优先。
- 注入 Core 已提交而 Hub 回执事务失败的窗口；相同事件重放取得原 job，不新增任务。另注入队列终态并调用实际正文回收，验证回执及摘要仍支持重放；该终态注入不算真实模型处理证据。
- 上游 `test_memory_server_durable_queue.py`、`test_memory_server_unified_entry.py`、`test_unified_write_entry_wiring.py`、`test_write_queue_lease_renewal.py`：212 项通过，原测试未修改。涵盖队列接纳、claim、lease、未知结果处理、正文回收和既有写入入口。
- 上游固定检出与补丁校验通过。原始输出保存在忽略目录 `.local/remote-ingest-tests.log` 和 `.local/remote-ingest-upstream.log`。
- `make check`：19 项工程门禁及格式、静态、文档链接、路径、源码凭证扫描和空白检查通过，输出为 `.local/remote-ingest-check.log`。

本地 Linux amd64 镜像 `cloud-memory-hub:dev-fd7b5ad` 已通过完整容器检查：禁网编码／分词、基础认证、模型配置密文、设备身份、已安装本地连接库过滤后通过真实 HTTP 接纳、丢失回执后的重启重放、原任务计数、确认本地队列、来源撤销和再次重启。测试没有激活模型，任务保持 pending。结果为 `.local/remote-ingest-container.log`。

首次容器探针失败在只读任务计数查询：探针使用了不存在的 `source_id` 列，上游实际列为 `source_key`。已按固定源码表结构修复，重新运行整套容器检查通过；上传实现和镜像未改动，未放宽断言。初始失败保留在 `.local/remote-ingest-container-before.log`，诊断只在忽略的私有文件中保存。

## NAS 独立容器证据

同一镜像归档通过 SSH 传输并在 NAS 校验 SHA-256 后加载。已用 `DOCKER_HOST=ssh://nas` 运行同一组探针，禁网编码、认证／密文配置、设备身份、过滤后 HTTP 接纳、重启补传和来源撤销的全部阶段通过；独立随机项目及卷随后清理。结果为 `.local/remote-ingest-nas.log`。

这是连接库与 NAS 容器 HTTP／Core 队列的真实组合，HTTP 请求在 NAS 的测试容器内执行；不宣称已验证 Mac 到 NAS 的正式访问入口、后台自动发送或两个真实 Agent 的接续。模型没有激活，接纳任务处于 pending。

| 制品 | 核对值 |
| --- | --- |
| 运行源码／OCI revision | `fd7b5ad` |
| 镜像 | `cloud-memory-hub:dev-fd7b5ad`，Linux amd64 |
| 归档字节数 | 259788288 |
| 归档 SHA-256 | `ef9f3847c2ef8e054c93117929c3c413aaadc747462165106dbd95c1d944ca55` |
| OCI manifest-list | `sha256:6311e085112c52a509ff4dfce964bdd82025004f23d8cce6e1e99504e9b4f1f8` |
| NAS 加载后的镜像配置摘要 | `sha256:a3151b617d9dc9c1156822d18344564bf8b5406567aa0d3b93440e258bb662ba` |

探针列名修复位于后续提交 `27306f8`，探针通过 stdin 送入容器，不在发行镜像中；应用源码仍准确对应 `fd7b5ad`。

## NAS 实验实例升级与回滚

对独立项目 `cmh-nas-validation` 执行 `4ce3724 → fd7b5ad → 4ce3724 → fd7b5ad`。升级前停止该实例，保存 config／data 的私有 0600 归档并验证可读取；原镜像和数据卷保留。回滚验证只切换镜像，不用旧备份覆盖当前授权库。

各阶段均确认健康；管理员凭证、加密密钥摘要、SDK authority、空记忆库和未配置模型状态一致。原 instance ID 保持一致，升级不自动授权设备。附加接纳 schema 为 v1，旧镜像仍可打开原设备权限库，再升级后来源／回执表继续存在。此处是空实例结构兼容性验证，不是有真实内容时的完整业务恢复演练。

最终 `.env` 固定 `cloud-memory-hub:dev-fd7b5ad`，容器镜像配置摘要与制品核对一致，状态 healthy；3 GiB／2 CPU、只读根目录及原回环端口不变。日志为 `.local/remote-ingest-nas-upgrade.log`，私有备份保留在 NAS 本批制品目录。没有修改 SSH、激活模型或接入真实 Agent。

## 审查与剩余范围

开发者自审检查了来源归属、零权限默认、接纳事务与撤销串行、处理前当前授权、两个数据库中断恢复、清理后的幂等回执、固定错误不回显正文，以及上游补丁默认兼容。无新增第三方依赖，未进行独立同行审查。

真实模型提炼、SDK 原文最终保存／检索、两个设备及实际 Agent 自动采集／注入、60 秒可用性、离线读取、删除传播、备份防复活均未验收。服务端接纳不代替本地过滤，也不代表独立网络发送器已经完成。NAS SSH 设置和真实 Agent 配置不在本批变更范围内。
