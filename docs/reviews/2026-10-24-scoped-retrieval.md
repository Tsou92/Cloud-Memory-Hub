# 按项目读取、检索与来源追溯验证

日期：2026-10-24。运行源码 `45f15c1`，分支 `codex/scoped-retrieval`。关联 FR-01／02／03／05／07／09、ENG-01；设计为 [ADR-0008](../adr/0008-scoped-retrieval.md)，公开合同为[授权读取](../remote-read.md)。这只是通用读取链路，不能代替真实 Agent 自动注入或完整产品验收。

## 本地实测

`make check-runtime`：143 项通过，较前基线新增 25 项读取场景／参数化用例。测试使用完整 Core 生命周期和临时设备权限库，其中真实 SDK 测试直接向独立 SDK 存储导入合成记忆／原始记录，使用实际本地 embedding 检索；没有把提炼或真实模型质量计为通过。

- 同项目两个 Agent 的记录均可读；其他项目、global、local 命名空间不混入默认列表与搜索。global 显式请求后单独返回。越界 ID 与不存在均为 404。
- 真实 SDK 的 active／archived／非最新记录得到不同处理；只返回当前有效记忆。搜索候选再次读权威记录，错误范围／归档成员与旧格式化摘要不能绕过校验。
- source_raw_ids 可指向并读取真实 SDK 原始内容，保留实际来源和会话；memory:read 不提供 history:read。重建 Hub／重开存储后读到同一合成记录。
- 在 SDK 工作期间撤销、轮换、移除权限，响应没有内容；移除后再恢复同样权限仍因 revision 改变拒绝本次结果。两个读取占用工作槽时，第三个得到固定繁忙错误，槽位释放后可继续读取。
- 服务端拒绝仍含合成凭证的查询且未调用 SDK。文本先整体过滤再分块，凭证跨分块边界也不会切出未过滤片段；SDK 异常不回显内部错误内容。
- 安装后的 CLI 通过真实回环 TCP Hub 检索；本地已知凭证过滤生效，权限移除后无查询正文发送，不创建无关投递库。该 TCP 用例的 SDK 搜索使用受控替身；实际 SDK 检索由上述独立用例覆盖，容器测试再验证组合。
- 传输拒绝错误实例／设备／scope／revision／操作、缺少 capability／grant、错误信任标记和非整数协议版本。CLI 不接收查询命令行参数，查询经 stdin 进入。

固定上游 `tests/test_hy_memory_bridge.py` 原测试 49 项通过。没有新 SDK 补丁；继续复用上游 bridge、客户端生命周期、ONNX encoder 和 HTTP 工厂。

`make check` 的 19 项工程门禁及格式、静态、链接、路径、凭证扫描和 Git 空白检查通过。日志位于忽略目录 `.local/scoped-read-runtime.log`、`.local/scoped-read-upstream.log`、`.local/scoped-read-check.log`。

## 失败归因与开发者自审

真实 SDK 用例首次发现列表包含非最新 active 记录。源码确认 `_memory_node_to_list_item` 未序列化 is_latest，而 Core bridge 默认 True；读取封装改为对列表候选也按 ID 核对当前值，保留同一失败用例。另一次过滤检查误将已脱敏但被分块截断的标记再次视作秘密，导致安全内容返回 503；调整为正文整体过滤后分块，额外元数据独立复核，测试通过。

自审覆盖授权与 query 范围、前后复核、来源与内部 backend 字段区分、原始正文独立权限、固定错误、请求／响应限制、取消时持有 SDK 工作槽，以及旧版本无 capability 时拒绝读取。此为开发者自审，不是独立同行审查。

## 容器与 NAS

`cloud-memory-hub:dev-45f15c1` 的 Linux amd64 镜像已通过本地容器与 NAS 独立临时容器探针：禁网真实 embedding、认证／加密配置、投递与重放、真实 SDK 检索、来源引用、已安装 CLI、重启后的读取和撤销权限后再次重启仍拒绝。

探针先建立合成设备授权，停止临时 Hub，再由独立进程写入合成 SDK 夹具，避免两个活进程写同一嵌入式存储；随后启动 Hub，经已安装 CLI 和实际 HTTP 读取。没有把夹具导入当作远程写入提炼成功。NAS 测试中的 CLI 和 HTTP 均运行在 NAS 容器内，不能当作 Mac 到 NAS 正式 HTTPS 网络验证。

首次本地容器检查在设备探针失败：测试仍断言旧的四项 capability，未更新新 scoped_reads。更新为完整五项列表，并给 smoke 的固定错误加入探针阶段名称；没有放宽权限或跳过断言。完整重跑本地／NAS 均通过。原失败日志 `.local/scoped-read-container.log` 保留，成功日志为 `.local/scoped-read-container-retest.log`、`.local/scoped-read-nas.log`。

| 制品 | 摘要／大小 |
| --- | --- |
| 镜像归档 SHA-256 | `8e32c0284eb286b9b171d48ecb89959f5f24b07a6bc32eeddc121b20eec5941c` |
| 归档字节数 | 261060096 |
| 本地 manifest-list | `sha256:d8e8bc61fb8caa0cd70830bc7d679617fd492891737e54f4617a80f0a3bfc0b8` |
| NAS 加载后的镜像配置摘要 | `sha256:3536b206347baf34e8cd465df625b507173c37d620288f6f856fb51e6c673a03` |

传输完成后在 NAS 校验归档摘要。独立项目 `cmh-nas-validation` 在确认原镜像和空接纳状态后停止，保存并校验 0600 的 config／data 私有归档（74240／543744 字节），完成 `9bbb834 → 45f15c1 → 9bbb834 → 45f15c1`。各阶段健康，管理员凭证与加密密钥摘要、实例 ID、SDK 权威状态、空记忆库和未配置模型状态保持一致，没有自动签发设备或授权来源。

最终实测镜像为 `cloud-memory-hub:dev-45f15c1`，配置摘要与上表一致；继续限制 3 GiB／2 CPU、只读根目录、127.0.0.1:8800。已保存最终镜像设置，旧镜像和备份保留。升级日志为 `.local/scoped-read-nas-upgrade.log`；未修改 SSH、日常 Agent 或真实模型配置。

## 仍未完成

NAS 正式 HTTPS 入口、配对与完整管理界面、实际六种 Agent 采集／注入、离线召回缓存、删除与恢复防复活、自动备份、真实模型质量和一分钟两设备验证仍未完成。SDK 历史列表的全 scope 载入及大库性能仍需优化／验收；分页不是同步快照。旧镜像回滚仅能证明当前实验实例兼容，不能替代带内容／删除规则的恢复演练。
