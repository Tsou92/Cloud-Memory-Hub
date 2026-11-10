# SQLite 连接寿命测试的观测器修复

关联：ENG-01 / 原生 Core 连接关闭回归。范围仅为固定上游 `test_connection_lifetime.py`，运行时代码、轮询路径、`gc.disable()`、泄漏断言和原有超时不变。

## 根因与方案

PR #6 的 [push CI `42816799831`](https://github.com/Tsou92/Cloud-Memory-Hub/actions/runs/42816799831) 在连接寿命测试期间触发 `Objects/tupleobject.c:909`。原计数器的 `gc.get_objects()` 会暂时强引用全堆对象，包括后台 `database_access._settings` 正在扩容的元组。真实 `MainDatabase` 的 Python 3.12.9 隔离复现中，无快照时关闭后 open/holders 均为 0；持有快照时产生相同 SystemError，随后留下 open=1 / holders=1。这是观测器诱发的连锁，不通过修改数据库访问实现规避。

补丁仅通过测试专用 `sqlite3.Connection` 子类增加弱引用能力，复用原生构造、语句缓存与 close。测试在创建应用前封装 `sqlite3.connect` 和 `sqlite3.dbapi2.connect`；计数只读取所登记的弱引用，后台任务结束且 `asyncio.run` 返回后才由 monkeypatch 恢复。固定 Core、SDK 与本地 Chroma 连接入口已静态核对；没有预先导入 connect 别名或自定义 factory。意外 factory 显式拒绝，避免漏计。

新增校准要求关闭的连接在局部 helper 返回后消失，故意未关闭并执行真实 SELECT 的连接在禁用循环回收时仍被计为 1。并发回归用 Events 固定元组第 11 项的扩容窗口；只有旧计数器会取得并持有真实全堆快照，新计数器必须完全不调用快照入口。修前对照在独立测试进程运行，不与正式通过进程混用。

`test_main_database_access.py` 的 `_open_connections_to` 也使用全堆扫描，本轮仅记录影响，不修改它。该函数还按数据库路径查询 PRAGMA，不能未经单独设计直接替换为本计数器。

## 验证

- 平台：macOS arm64，CPython 3.12.9，SQLite 3.47.1。使用独立固定上游副本，未修改原开发副本。
- 真实数据库根因复现：由根任务在隔离子进程调用未改的 `MainDatabase`；其 `database_access.py` SHA256 为 `680bf9a854c783c2b52d3beb5d179624c9b9e6e7a285df3ceebe97559a9f68aa`。结果如上；原始复现脚本与结果仅保存在私有验证目录。
- 修前对照：保留新增竞争回归，仅将计数器恢复成原有 `sum(isinstance(item, sqlite3.Connection) for item in gc.get_objects())`。官方 runner 独立进程实际 **1 failed / 5 deselected**，失败为真实 `Objects/tupleobject.c:909: bad argument to internal function`。未伪造异常，也未与修后套件共享解释器。
- 修后：同一官方 runner、同一 Python 与绑定后的固定 SDK 源码，本文件 **6 passed / 0 skipped**，耗时 4.20 秒。包含原全部 GET 轮询、两种 sqlite 入口的真实关闭／漏关校准、两种意外工厂拒绝，以及并发元组构造回归；新增竞争用例中快照入口调用次数为 0。
- 两次运行的 `generate_core_test_index.py --check` 均通过，仍为 343 个测试文件；分类检查 `unclassified=0 / multiple=0 / performance-live-without-serial=0`。未新增文件或修改分类索引。
- 同一官方隔离进程再运行本文件与未改的 `test_main_database_access.py`：**39 passed / 0 skipped**，耗时 5.36 秒，覆盖原连接池并发、轮询与测试间清理。相邻文件中的全堆扫描保持原样，本轮没有通过删除或跳过它制造通过。
- `prepare_upstream` 校验固定提交、全部 37 项补丁摘要，并在临时 Git index 重放后确认实际源码逐字一致；0039 仅涉及一个上游测试文件。补丁 SHA256：`e6274571d72eebe4b1c4b7076432adfb776b3c288ceac3f34daea37dba170bab`。
- `make check` 通过：113 项中 **110 passed / 3 skipped**，格式、lint、仓库路径／文档链接、离线凭证扫描及 diff 检查均通过。3 项跳过分别是专用 OS workflow 的 POSIX 无 Python 下载、Windows 无 Python 下载与真实 Windows DACL；没有将它们算作本机通过。此次 PATH 包含官方便携 PowerShell 7.6.9，复制命令检查实际执行。
- 独立只读审查已核对弱引用不保活、原生 close 不改、应用与后台生命周期、并发退出清理及真实反例；补丁最终登记复核单独记录于任务交付。

沿用 `make check-upstream` 的官方 runner 和固定 SDK 来源插件。定向调用为：

```sh
uv tool run --from uv==0.12.5 uv run --no-project -- \
  bash scripts/run-tests-isolated.sh core-pr -q -p verify_core_sdk_source \
  tests/test_connection_lifetime.py tests/test_main_database_access.py
```

执行前 `PYTHONPATH` 仅增加指向本工作树固定 SDK 的临时包目录和仓库 `scripts`，由 `verify_core_sdk_source` 实际验证导入来源；隔离 HOME / data / ports 继续由官方 runner 建立。修后单文件验证仅传第一个文件；修前对照同样只传第一个文件，并加 `-k concurrently_growing_tuple`。完整命令、原始日志和 JUnit 保留在私有验证目录。

本轮未重建 Docker、未访问 NAS 或修改日常配置。该定向通过不等于完整 Core 新提交 CI 已通过；完整回归交由后续 PR CI。
