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

本地／NAS 容器探针已编写，镜像运行和升级回滚尚待本轮执行；不能把探针源码当作通过证据。

## 审查与剩余范围

开发者自审检查了来源归属、零权限默认、接纳事务与撤销串行、处理前当前授权、两个数据库中断恢复、清理后的幂等回执、固定错误不回显正文，以及上游补丁默认兼容。无新增第三方依赖，未进行独立同行审查。

真实模型提炼、SDK 原文最终保存／检索、两个设备及实际 Agent 自动采集／注入、60 秒可用性、离线读取、删除传播、备份防复活均未验收。服务端接纳不代替本地过滤，也不代表独立网络发送器已经完成。NAS SSH 设置和真实 Agent 配置不在本批变更范围内。
