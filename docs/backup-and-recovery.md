# 备份与恢复

状态：发布候选实现与验证中。完整导入、历史回滚、损坏归档拒绝及一次真实进程中断后的保护版本恢复，已通过 NAS 隔离合成数据演练；范围见文末记录。使用以下受控命令，不要直接把三个旧卷覆盖到日常实例。

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

设计与兼容边界见 [ADR-0063](adr/0063-managed-backup-and-recovery.md)。本地测试包括真实 Core／SDK 文件与 HTTP 启动恢复，模型输出使用合成样例；本地回归与下述 NAS 实测分别记录。

## NAS 恢复演练记录

2026-11-10，在 NAS Linux amd64 上使用固定镜像 `cloud-memory-hub:preview-ae9cb2e`（源码 `ae9cb2e`）完成两组独立演练。运行前核对镜像来源、配置及完整文件系统层摘要，检查可用空间；每组使用全新数据、配置、治理和备份卷，容器限制为 3 GiB／2 CPU、网络禁用。业务、凭证和文件均为合成数据，没有调用外部模型或改动日常实例。

- **正常恢复：11 项阶段检查通过。** 实际生成备份、导出并完整检查归档，停止原实例后导入空三卷；启动后通过 HTTP 读回两条记忆、两条 SDK 原文及一条已接纳事件，核对身份、来源权限、模型配置和加密凭证。实际执行普通忘记、凭证轮换、设备撤销及配置修改后，恢复历史版本并重新启动：已忘记记忆仍不可读，无关记忆保留，旧凭证与已撤销设备被拒绝，较新治理见证保留。损坏归档返回 `backup_archive_invalid`，目标三卷的 21 个文件、共 1,109,646 字节的校验摘要在拒绝前后相同，再次启动和读回通过。
- **待处理事件的保护边界单独核对。** 同项目的未处理事件与原生记忆忘记组合返回 `governance_inflight_scope_unproven`；导入前后队列、来源和已登记准入证明一致，符合 [ADR-0038](adr/0038-durable-sdk-admission.md) 的保护。正常恢复演练另用独立项目承载该事件；事件始终为 `accepted`／`pending`，没有清空队列或伪造处理完成。这不表示该事件已完成模型提炼。
- **真实中断：7 项阶段检查通过。** 使用 128 MiB 普通合成文件，在三个恢复标记出现、观察到实际文件已复制 1 MiB 后向维护子进程发送 `SIGKILL`；进程退出 `-9`，文件停在 2 MiB，首次注入即命中。三个标记、暂存现场和通过完整检查的保护版本仍在。普通服务与已启用备份计划的默认调度进程均以 `restore_incomplete` 拒绝启动；真实备份调用返回 `backup_restore_incomplete`，未新增版本。随后将保护版本导出并导入另一组空三卷，实际启动读回业务、身份和配置，并核对完整 128 MiB 当前文件摘要，确认恢复后才清理原现场。

两组演练各使用两容器、八独立卷；验收及取证后按所有权标签清理，额外查询确认无残留。详细探针、日志和归档校验摘要留在私有验证工作区；合成运行卷及其中的归档已清理，不提交运行数据或凭证。此前 `preview-c60b48c` 的 NAS 演练已验证两个不同自动版本和容器重启后计划保留；它与本次恢复镜像的证据分开记录。

这些结果证明限定的导入、历史回滚、损坏归档拒绝和单次进程中断恢复，不代表 NAS 断电、硬盘故障、所有中断位置或自动修复均已验证。备份管理网页的真实点击、最终发行制品的全新安装与升级流程仍须按[发布计划](release-plan.md)验收。
