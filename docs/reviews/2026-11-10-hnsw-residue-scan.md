# HNSW 预分配空间的扫描边界修正

关联 FR-10、ADR-0032；范围仅为回归测试的残留分类，不修改产品删除语义或数据文件。

## 失败与根因

提交 `e4d3fb0` 的 [GitHub Actions 42778272106](https://github.com/Tsou92/Cloud-Memory-Hub/actions/runs/42778272106) 在 Hub 集成阶段得到 2,163 passed、6 skipped、1 failed。失败为 `test_purge_removes_cached_input_copies_but_keeps_later_originals_and_raws`：选定原文已从业务记录删除，但全文件扫描在 Chroma 段的 `length.bin` 检出合成原文字节。CI 日志没有保留该文件，不能宣称已经核对 CI 原文件的字节偏移。

[Chroma 1.5.9 的 Cargo.lock](https://github.com/chroma-core/chroma/blob/1.5.9/Cargo.lock) 固定 hnswlib `6868102bde454dc761136e1994490133a6a026bb`。[该版本写入器](https://github.com/chroma-core/hnswlib/blob/6868102bde454dc761136e1994490133a6a026bb/hnswlib/hnswalg.h) 对 `data_level0` 和 `length` 都分配未初始化的内存，并在首次持久化时写入完整容量。有效元素范围由头部 `cur_element_count` 确定；长度槽位为 4 字节 float。原测试只按文件名排除了整个 `data_level0.bin`，既漏掉了 `length.bin` 的相同预分配行为，也可能遮住 level0 已用区的真实残留。

## 复现与改动

- 本机 macOS 使用原用例的 16 个独立合成实例，16 passed；在 level0 未用区观察到原文字节，未在 length 文件复现。
- 固定 `e4d3fb0` Linux amd64 镜像运行原用例的 12 个合成实例，得到 1 failed、11 passed。失败实例的 `length.bin` 长 400 字节，100 字节 v1 头声明 capacity=100、count=0；原文命中偏移 30，处于未用区。容器禁网，产品代码来自固定镜像，仅挂载测试和必要的纯 Python 测试依赖；未连接日常卷。
- 失败文件保留。对同一文件重新执行新旧扫描，旧扫描报告残留，新扫描将其归入已证明的未用区；前后文件 SHA-256 相同。此步骤没有擦除字节。
- 新 `hnsw_unused_start` 校验完整 64 位 v1 头、计数／容量、槽位布局及两个数据文件长度，再返回未用区起点。扫描仅排除该起点之后的匹配；已用区、跨边界、未知版本、缺失／损坏头、文件长度不符和无关同名文件均保留为失败。
- 确定性反例证明旧扫描会遗漏 level0 已用区，并误报 length 未用区；新扫描分别正确拒绝和分类。没有添加整个 `length.bin` 的豁免。

## 验证与限制

本机 `test_hnsw_scan_boundary.py`、`test_purge_recovery.py`、`test_governance_execution.py` 共 89 passed。Linux amd64 固定镜像使用新检测规则，44 项边界测试和原失败用例合计 45 passed。相关文件格式、lint 和差异检查通过。全仓 `make check` 已尝试，因并行接入工作中的 `onboarding/connect.py`、`test_onboarding_client.py` 尚未格式化而在第一步停止；不改写其他人的进行中实现，也不将这次全仓检查记为通过。最终工程检查及完整远程 CI 由发布主线统一重新验证。

私有复现文件、源码取证、原始失败日志和新旧分类结果存于开发环境的 `ci-hnsw-residue` 验证目录，不进入发布源码。

这仍是 [ADR-0032 已接受的 HNSW 预分配残留限制](../adr/0032-governance-execution.md#已接受的已知限制hnsw-预分配空间)，不是磁盘物理擦除的完成证明。现已确认该机制也覆盖 `length.bin` 的未用区；备份仍可能包含这些字节，不能宣称磁盘或导出包无任何残留。未来格式不匹配时测试应失败并重新取证，不扩大忽略范围。
