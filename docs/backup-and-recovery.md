# 备份与恢复

状态：发布候选实现与验证中。定期备份入口见下；完整导入和选择历史版本回滚尚未交付，不要直接把三个旧卷覆盖到日常实例。

## 保存位置与范围

容器将完整版本保存到独立的 `/backups` 挂载。默认 Compose 使用 `hub_backups` 数据卷；也可在 `.env` 中设置 `CMH_BACKUP_PATH` 为 NAS 上专用的绝对目录，使备份存放到其他磁盘。目录归容器运行用户 UID/GID `1000:1000` 所有，权限为 `0700`，不能使用公开共享目录。设置后按部署文档重新创建 Hub 容器，现有三个数据卷保持原名称。

备份包括记忆、原文、项目、设备权限、模型配置与密钥以及治理记录。备份含敏感数据，应私有保存；同一 NAS 或同一磁盘上的副本不能替代异机备份。备份期间 Hub 会短暂不可用，客户端已入队的内容保留并由既有补传机制重试。

## 定期备份

使用候选 Compose 和默认 `supervise` 启动方式；显式改成 `serve` 的容器没有调度器。首次启动默认关闭计划。

```sh
docker compose exec hub /opt/cmh/venv/bin/python -m cloud_memory_hub.backup_runtime schedule --every-minutes 1440 --keep 7
docker compose exec hub /opt/cmh/venv/bin/python -m cloud_memory_hub.backup_runtime status
```

这会每 24 小时执行一次，并保留最近七个完整版本。首次执行在配置后一个周期到期时进行；计划与上次尝试时间持久化，容器重启后继续读取。可以再次执行 `schedule` 调整周期和保留数。

立即请求一次备份、查看完整版本列表或关闭计划：

```sh
docker compose exec hub /opt/cmh/venv/bin/python -m cloud_memory_hub.backup_runtime request
docker compose exec hub /opt/cmh/venv/bin/python -m cloud_memory_hub.backup_runtime list
docker compose exec hub /opt/cmh/venv/bin/python -m cloud_memory_hub.backup_runtime disable
```

请求返回 ID 只表示已排队；随后使用 `status` 查看完成状态。`completed` 及对应版本通过清单验证才表示备份生成；`running`、`interrupted` 或 `failed` 不能作为可恢复证明。`last_success_at` 保留最近成功版本的时间，失败不会伪造为成功。状态命令的 `supervisor_running=not_checked` 不等于服务运行验证；部署时仍需检查容器健康状态。

调度器必须先正常停止服务再取得三卷租约。停止失败、空间不足或校验失败时不会发布成功版本。备份失败后恢复服务；若备份盘连状态文件都无法写入，容器日志记录固定错误码 `backup_control_unavailable_retry_delayed`，调度暂停至少 60 秒后再检查，避免反复停服。修复空间或权限后再请求备份，保留已有完整版本与失败现场。

## 完整导入与历史回滚边界

当前仍在实现和演练完整导出导入、当前保护版本及历史恢复命令。清单目录的存在不意味着已经通过恢复演练。不要用旧配置覆盖当前授权或治理卷；这可能让撤销的凭证或已忘记内容重新生效。

已确定的恢复规则见 [ADR-0063](adr/0063-managed-backup-and-recovery.md)：完整空目标导入保留实例身份；同实例历史回滚保留当前安全授权和治理约束。中途失败必须保持服务关闭并保留现场。最终发布前需要完成两个自动版本、独立空环境导入、较新忘记／撤销保留及失败恢复的 NAS 演练。
