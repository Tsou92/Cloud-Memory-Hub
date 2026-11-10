# 存储租约测试的高文件描述符等待修复

关联：FR-10 / FR-11 / FR-13 / ENG-01，以及 [ADR-0013](../adr/0013-runtime-storage-exclusion.md)。仅修改 Hub 的 `tests/test_storage_lease.py`，不改变存储租约、数据库、服务启动或其他运行时代码。

## 失败与边界

PR #7 的 [PR CI 42827543163](https://github.com/Tsou92/Cloud-Memory-Hub/actions/runs/42827543163) 在提交 `f466285f2c240de1c184c4c1d427e7d7c78408b3` 的 Hub 回归中实际得到 **1 failed / 2279 passed / 6 skipped**，耗时 661.48 秒。唯一失败为 `test_real_process_holds_all_roots_and_os_releases_after_kill`：`select.select([child.stdout], [], [], 10)` 抛出 `ValueError: filedescriptor out of range in select()`，尚未执行就绪文本及锁状态断言。

根因是测试等待器的 `select` 文件描述符编号限制。日志没有提供描述符高编号的来源，不能据此断言产品泄漏或纯 runner 环境问题。已阅读此前 [SQLite 测试观测器修复](2026-11-10-sqlite-test-counter.md)，未把那次独立的 GC 快照问题当成本次根因，也没有改动其观测器或产品连接生命周期。

## 最小修复与验证

- 用标准库 `selectors.DefaultSelector` 等待同一真实子进程的 stdout pipe，保留 **10 秒**超时。selector 使用上下文管理器关闭。
- 原用例参数化为普通 FD 与高 FD：高 FD 参数只通过 `fcntl.F_DUPFD` 复制一个 pipe 到编号至少 1024，不需要占满此前的编号，不修改进程资源限制。若平台的 `RLIMIT_NOFILE` 明确不允许分配该编号，只跳过高 FD 参数，普通用例继续执行；其余分配或等待错误保持失败。
- 两参数均继续检查原 `ready` 文本、三个根目录被同一子进程持锁时拒绝竞争、无关目录可用，以及 kill 后 OS 释放锁。finally 关闭高 FD 副本、终止并等待剩余子进程，关闭原三条 pipe。
- 本机环境为 macOS arm64 / CPython 3.12.9，实际使用 `KqueueSelector`。先用真实 pipe 的 FD 1024 复现原异常，同一 pipe 的新等待器确认就绪。
- 修前保留新增高 FD 参数、仅保留原 `select.select` 等待器：**1 failed / 1 passed / 7 deselected**，0.21 秒，高 FD 参数抛出与 CI 完全相同的 ValueError。
- 修后完整 `test_storage_lease.py`：**9 passed / 0 skipped**，3.67 秒。包含真实服务的第二进程及迁移拒绝、终止后恢复执行检查。
- `make check` 通过：113 项中 **107 passed / 6 skipped**；格式、lint、仓库路径／文档链接、离线凭证扫描及 diff 检查通过。跳过的是三项本机 PATH 未提供 PowerShell 的测试、两项仅由专用 OS workflow 执行的缺 Python 下载测试，以及真实 Windows DACL 测试，未将这些跳过算成本机通过。

定向验证沿用项目锁定环境：

```sh
uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub \
  pytest -q tests/test_storage_lease.py --tb=short
```

原失败 CI 不重跑、不取消；同提交 push CI 单独保留结果。该修复仍须通过后续新提交的 Linux 完整 CI，不能将本机通过或旧 push 成功替代新提交验收。未访问 NAS、日常凭证或用户历史。
