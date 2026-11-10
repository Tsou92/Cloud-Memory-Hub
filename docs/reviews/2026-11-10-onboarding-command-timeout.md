# 复制命令测试宿主等待上限验证

关联 [ADR-0065](../adr/0065-one-link-remote-agent-onboarding.md) 的一条命令接入验收。发行运行
[42803231636 的 Windows 安装测试](https://github.com/Tsou92/Cloud-Memory-Hub/actions/runs/42803231636/job/129785864785)
在 `not_found` 子场景启动 Node 生成复制命令时超过 10 秒，尚未执行 PowerShell。
日志证明这一子进程超时，未证明更底层的调度或启动原因。

仅将 `tests/test_onboarding_commands.py` 中 Node 命令生成子进程的等待上限从
10 秒改为 30 秒。它读取合成页面输入并输出命令，不进行下载或配对，也不代表产品的响应时限。
随后真实 shell 执行仍限定 15 秒；所有下载完整性、退出码、坏包不执行及旧脚本不执行断言保持原样。

2026-11-10 在 macOS 使用真实 Node 22.14.0、便携 PowerShell 7.6.9 和 Python 3.13.3，
对修改后的文件执行：

```sh
python3 -m unittest discover -s tests -p test_onboarding_commands.py -v
```

结果为 2 passed、0 skipped，耗时 6.383 秒。POSIX 与 PowerShell 各实际运行
404、部分下载、完整下载、安装器失败四个子场景，共八个；测试只使用环回 HTTP 和合成脚本。
便携 PowerShell 所在目录仅加入本次测试进程的 `PATH`。

同一环境的 `make check` 通过：113 项中 110 passed、3 skipped；跳过的是专用系统 CI
负责的两个缺 Python 引导场景和真实 Windows DACL 场景。仓库路径、文档链接、源代码秘密扫描及
差异空白检查通过。此结果对应本次测试期限改动，后续合并其他改动仍需检查最终提交。

这项结果是 macOS 上的真实 Node/PowerShell 测试，不是 Windows 真机复验。
下一候选仍需通过 Windows CI；此前发行运行的失败记录保留，不因其他普通 CI 成功而消除。
