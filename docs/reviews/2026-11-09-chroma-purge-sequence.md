# 删除后继续写入与向量删除重放

日期：2026-11-09。关联 FR-10、FR-11、ENG-01；设计见 [ADR-0037](../adr/0037-chroma-purge-sequence.md)。本轮承接 [治理恢复切片](2026-11-09-governance-retry-and-entities.md) 安装前暴露的组合故障，不能用各段单独通过替代完整路径。

## 问题与修复

Purger 原来按目标 ID 清空 Chroma 写前日志，包括最高序号和向量段尚未消费的 DELETE。实测治理后 queue max=9、已应用元数据水位=11；下一次 SDK 写入复用 10/11，返回成功的部分 ID 不可读，Core 观察器保留 unknown。组合源码探针因此失败。

修复继续调用 SDK 删除，并对所有确认的原生 ID 经 Chroma collection.delete 提交真实目标墓碑。清理严格限定当前集合 topic 与映射后的 point ID，只豁免 operation=3、无 vector/encoding、metadata 为 NULL 或 '{}' 的无正文删除状态；目标正文/向量日志仍清除。history 与结果缓存继续使用原生身份，不混用 point ID。墓碑交由 Chroma 原生消费和回收，不能用随机锚点代替真实删除依据。

清理前后检查日志序号，事务内后验失败会回滚清理。已存在的旧序号倒退在 SDK 初始化前检查；初始化后再次核对实际配置路径，首次空库仍可初始化。completed purge 即使目标已缺失也经过启动前置检查，不通过继续追加空写入来伪造历史修复。返回 `governance_chroma_sequence_invalid` 时保留现场，不开放就绪。

不更改 Chroma schema、SDK/队列协议或上游源码；复用 ID 映射、topic 命名、原生 delete 和现有线程池。已接受的 HNSW 预分配残留限制不变，也不宣称删除日志中的技术 ID 已全部消失。

## 定向与独立验证

- `test_chroma_purge_sequence.py`：同进程与重开后真实 Core Drain→SDK 新写入可读且 observer 完成，忘记/删除内容不进入新提炼历史，无关原文保留；旧倒退启动拒绝，原始业务数据与签名状态保留。三项均先复现失败。
- `test_chroma_delete_replay.py`：1000 个真实节点及向量段水位滞后，4 个独立进程检查严格空 DELETE 保留、目标不重现和同 ID 新正文/新向量一致。相同 fixture 下，加载 9f9c851 的旧清理函数后新向量查询错误，新实现与独立复跑正确。
- `test_chroma_governance.py`：12 项，覆盖实际 Rust topic、非 UUID 映射、集合隔离、空墓碑严格形状、NULL/异常状态、只读拒绝和旧倒退。三个新文件共 16 项通过。
- 独立运行完整 `probe_governance_execution.py`：forget/purge、重启、只恢复结果缓存、完整恢复 data/config、确认请求重试/冲突、来源/内容重导入以及恢复后真实新写入均通过；模型与 supervisor 为合成替身。
- 独立代码审查核对了映射、NULL 三值逻辑、事务回滚、前置顺序及锁边界，未发现需阻塞的新缺陷。

早期高阈值诊断使用大量重复向量，控制组本身退化且脚本未 assert；该输出已撤回。正式测试改用不重复向量，增加 seed/query 强断言，红绿两侧使用同一 fixture；本记录只将新测试的实际内容/距离断言作为证据。

序号前置检查验证 INTEGER PRIMARY KEY 的 rowid 合同，通过独立 MIN/MAX 端点读取；段水位最多 10,000 行，超限拒绝，不无界 SUM 扫描全部日志。本机 1 行/100,000 行的 VM 步骤为 475/465，EXPLAIN 均 SEARCH，仅作有界查询的定向证据。

## 安装状态与剩余边界

`make check-runtime` 全量 838 通过、5 项可选后端跳过、6 个已知队列缺口预期失败、289 条上游弃用警告；工程门禁通过。上游源码及补丁没有改变，固定来源校验通过；本轮直接运行真实 SDK/Chroma 集成，没有把未重跑的全部上游 PR 测试记作新证据。

代码提交 `cde427c9c41a7387916b50407823deb75be5595f` 已构建 Linux amd64 镜像 `cloud-memory-hub:dev-cde427c`。构建复用校验过的固定上游与模型缓存，未改上游补丁；运行环境核对不含构建代理。

| 制品证据 | 实际值 |
| --- | --- |
| 镜像 config digest | `sha256:c81dab123fc15c59152dce812d764308fe55fc38bd282ec36bc6a3bb5b6ee870` |
| 导出包大小 | 261299712 字节 |
| 导出包 SHA256 | `3e919b902f65e87bb0d9ce5cc1e1611b4ead67ba6b0501c33c5087f85aab4bc3` |
| 高阈值独立探针 SHA256 | `53159480ce6611f4919796d3852222bd68dba67c8e75f47bd3500a1e733d09e9` |

五项已安装探针在本地与 NAS 均通过：`probe_governance_execution.py`、`probe_drain_quiescence.py`、`probe_entity_inventory.py`、`probe_productivity_forget_guard.py`、`probe_chroma_delete_replay.py`。容器禁网、根目录只读、移除 capabilities，限制 3 GiB/2 CPU。高阈值探针是独立挂载的可哈希脚本，无 tests/pytest 依赖；它在镜像代码冻结后补充，不代表镜像额外改了产品代码。生产力探针使用受控 writer 产出，其他模型/embedding 替身边界沿各脚本说明，不当作真实模型质量验收。

完整容器检查在本地和 NAS 也通过，涵盖真实本地向量生成、首启/认证、设备授权/撤销、过滤上传、来源采集、安装 CLI/召回及重启持久性。镜像和探针传输到 NAS 后再次核对架构、源码 revision、镜像摘要和逐文件 hash。

常驻实验实例先核对仍为空库、无配置模型，停服后保存 data/config/governance 三卷与部署配置，验证 tar 可读及哈希，再从 60ad0ce 升级到 cde427c。启动及再次重启后健康，身份/凭证/空库状态一致；实际镜像、命名卷、3 GiB/2 CPU、只读根目录、cap-drop ALL 与回环监听不变。没有导入真实历史、激活模型或改变 SSH。未执行旧镜像回退演练，也不以 schema 未变化推断旧镜像可安全回退。

本轮不解决 ADR-0034 的 6 个已接纳任务/旧队列恢复缺口，不完成实体或生产力忘记/删除、离线传播、全量备份或六客户端闭环。只读序号检查也不证明任意混搭备份整体一致；旧版本已经遗漏的写入不会被自动补造。G1 退出条件保持不变。
