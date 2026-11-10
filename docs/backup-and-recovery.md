# 备份与恢复

状态：发布候选实现与验证中。完整导入和历史回滚已有本地合成数据验证，NAS 演练仍待完成。使用以下受控命令，不要直接把三个旧卷覆盖到日常实例。

## 保存位置与范围

容器将完整版本保存到独立的 `/backups` 挂载。默认 Compose 使用 `hub_backups` 数据卷；也可在 `.env` 中设置 `CMH_BACKUP_PATH` 为 NAS 上专用的绝对目录，使备份存放到其他磁盘。目录归容器运行用户 UID/GID `1000:1000` 所有，权限为 `0700`，不能使用公开共享目录。设置后按部署文档重新创建 Hub 容器，现有三个数据卷保持原名称。

备份包括记忆、原文、项目、设备权限、模型配置与密钥以及治理记录。备份含敏感数据，应私有保存；同一 NAS 或同一磁盘上的副本不能替代异机备份。备份期间 Hub 会短暂不可用，客户端已入队的内容保留并由既有补传机制重试。

## 定期备份

使用候选 Compose 和默认 `supervise` 启动方式；显式改成 `serve` 的容器没有调度器。首次启动默认关闭计划。管理员也可在网页「设置 → 备份与恢复」设置周期和保留数、立即请求备份及查看最近状态。网页版本列表仅读取有界清单摘要，显示“已生成，恢复前完整校验”；导出和恢复时仍执行完整文件校验。以下命令提供相同计划的维护入口。

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

## 手动完整导出与检查

先从 `backup_runtime list` 选择 `state=complete` 的版本 ID，用它替换下列 `BACKUP_ID`。导出包含三卷的完整私有归档；不覆盖已有输出文件。`/backups` 必须留有足够空间，导出后可自行把文件保存到其他设备。

```sh
docker compose exec hub /opt/cmh/venv/bin/python -m cloud_memory_hub.backup_maintenance export --version BACKUP_ID --output /backups/private.cmh.tar
docker compose exec hub /opt/cmh/venv/bin/python -m cloud_memory_hub.backup_maintenance inspect --archive /backups/private.cmh.tar
```

目前只接受本功能生成的未压缩 `.tar`，文件须为 `0600`，父目录须为当前运行用户拥有的私有目录。检查在运行三卷之外的私有临时目录解包，核对格式、文件清单、摘要、数据库和治理身份；拒绝链接、越界路径、缺失或额外文件。默认最多展开 16 GiB，较大版本可用 `--max-bytes` 明确提高，最大 1 TiB。该上限约束解包规模，不代表可用磁盘空间；还须容纳暂存副本及恢复前保护版本。

只导入自己控制来源的完整备份。文件摘要用于核对归档内容，不是生成者的数字签名；归档包含原生数据库，不能把来历不明的数据库或第三方归档作为可信恢复来源。

## 同实例历史回滚

历史回滚只适用于同一实例、同一加密密钥和治理链相容的版本。先停止 Hub，包括负责重启子进程的调度器，再运行独立维护容器；不要在正在运行的 Hub 中执行恢复。

```sh
docker compose stop hub
docker compose run --rm --no-deps --entrypoint /opt/cmh/venv/bin/python hub -m cloud_memory_hub.backup_maintenance restore --version BACKUP_ID --writers-stopped
```

命令先保存当前完整保护版本，再准备恢复。旧记忆、原文、来源、回执和模型设置恢复到选定业务时间点；当前管理员凭证、加密密钥、项目、设备撤销／轮换／授权，以及完整治理卷均保留。旧来源的身份必须仍能核对，其撤销状态保持当前值。快照之后新增的来源可能需要重新连接，结果返回对应数量；程序不会重传真实历史。

当前状态发生变化、来源身份或签名见证无法核实，以及旧未完成任务在当前准入记录中已可能写入时，命令拒绝恢复。无法证明来源的未完成旧队列同样不自动重放；不通过清空队列绕过未知写入。

成功返回 `files_restored_pending_startup` 只表示文件准备完成。记录返回的 `protective_backup_id`，然后启动并检查服务，由原有启动流程应用当前忘记／删除约束：

```sh
docker compose up -d hub
docker compose ps hub
docker compose exec hub /opt/cmh/venv/bin/python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8800/healthz', timeout=5).status)"
```

健康检查通过后，还应确认网页可读取目标项目、已忘记内容仍不可读、当前设备凭证仍符合预期。启动治理恢复失败时，服务保持受保护状态，不能把维护命令成功视为恢复验收完成。

## 导入新的空目标

原实例必须停止，防止同一身份在两个实例同时写入。目标 `/data`、`/config`、`/governance` 必须是新的空卷或空目录，最多允许已有的存储锁文件；不要先执行 `init`，也不要用此命令覆盖现有实例。归档放在独立 `/backups`，不得位于运行三卷中。

在已准备上述空卷的目标 Compose 环境中运行：

```sh
docker compose run --rm --no-deps --entrypoint /opt/cmh/venv/bin/python hub -m cloud_memory_hub.backup_maintenance import --archive /backups/private.cmh.tar --writers-stopped --source-stopped
```

导入保留备份时的实例身份、管理员和设备凭证、模型配置及治理记录，不创建新身份。旧快照无法证明备份之后的忘记／撤销；灾难恢复只恢复归档保存的时间点。成功后仍按上一节启动和验证，不同时重启原实例。

## 中途失败与现场保留

跨卷替换前会写入 `.cmh-restore-incomplete.json`。若中断或校验失败，保留保护版本、`/backups/.restore-<operation_id>/` 暂存和运行卷标记；普通启动与自动备份都拒绝继续，返回 `restore_incomplete` 或 `backup_restore_incomplete`。不要删除标记来强行启动，也不要盲目重复覆盖。

保持原卷停止并保留现场。可以把已验证的保护版本导入另一组空三卷，完成隔离启动和内容核对后再切换；存在未知写入或保护版本也无法验证时，需要先审阅该错误，不能自动绕过。完成恢复后若仅清理暂存失败，结果单独标明 `staging_cleanup_pending=true`，不把它误报为业务恢复失败。

设计与兼容边界见 [ADR-0063](adr/0063-managed-backup-and-recovery.md)。本地测试包括真实 Core／SDK 文件与 HTTP 启动恢复，模型输出使用合成样例。正式发布前仍需完成两个自动版本、独立空环境导入、较新忘记／撤销保留及失败恢复的 NAS 演练。
