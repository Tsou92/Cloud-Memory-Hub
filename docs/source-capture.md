# 本地来源采集：隔离验证

当前支持显式绑定的 Claude Code、Codex、DeepSeek Harness（DSH）与 T3 Code（统一多 Provider 线程库）原生记录，复用固定上游解析器和授权文件读取。它将过滤后的记录写入现有 LocalOutbox；发送继续使用 [send／watch](local-connector.md)。设计与兼容性见 [ADR-0009](adr/0009-local-source-capture.md)、[ADR-0077](adr/0077-dsh-history-import.md) 与 [ADR-0079](adr/0079-t3code-unified-source-capture.md)，实际 CLI 证据见 [验证记录](reviews/2026-10-24-claude-native.md)。

以下隔离验证细节记录 2026-10-24 的阶段；当前安装器、历史导入与附件抽取的支持范围以状态说明和发行说明为准。首轮 collect 会从指定目录的历史起点开始，后续按游标增量采集；不会自动猜测 `~/.claude`、`~/.dsh` 或 `~/.t3/userdata` 路径。

状态说明（2026-11-18，2026-11-24 更新）：本页前两段描述 2026-10-24 的隔离验证阶段。v0.1.6 起安装器已支持项目钩子与自动采集，v0.1.11 起历史导入支持 Claude Code 与 Codex，v0.1.13 起加入 DSH `session.v3.jsonl.zstd`，当前分支补充 `t3-code`（统一扫描 `~/.t3/userdata/statev2.sqlite` 及 `-wal`）。当前支持范围以[设备与 Agent 自助接入](client-installation.md)和发行说明为准。

## 显式配置

先按[连接组件说明](local-connector.md)初始化本地队列并准备私有 connection 文件。NAS 管理端登记的来源 adapter ID 应为 `claude-code`、`codex`、`deepseek-harness` 或 `t3-code`，并绑定同一设备与项目；来源 UUID 和项目 UUID 使用服务端返回值。

在源码目录之外创建权限为 0600 的 source 文件，父目录由当前用户控制。字段如下，尖括号内容必须替换：

```json
{
  "schema_version": 1,
  "adapter_id": "claude-code",
  "source_id": "<来源 UUID>",
  "scope_id": "<项目 UUID>",
  "source_root": "/absolute/path/to/synthetic-transcripts",
  "workspace": "/absolute/path/to/synthetic-project",
  "source_version": "<实际 CLI 版本，可省略此字段>",
  "media_text_extraction": false
}
```

`source_root` 与文件布局：

- `claude-code`：`source_root` 指向该项目的 `~/.claude/projects/<编码后的项目路径>`，读取其中的 `.jsonl`。
- `codex`：`source_root` 指向 `~/.codex/sessions`，读取 `YYYY/MM/DD/rollout-*.jsonl`；工作目录取自会话头 `session_meta.payload.cwd`。
- `deepseek-harness`：`source_root` 指向 `~/.dsh/sessions`（或该项目对应的编码目录），读取 `session-*/session.v3.jsonl.zstd`；工作目录取自会话头 `cwd`，会话 ID 取自头 `id`。zstd 流按解压后的记录序号续读，不能按字节偏移续读；含图片等媒体且未启用可抽取文本的会话保持 fail-closed。
- `t3-code`：`source_root` 指向 `~/.t3/userdata`，以只读镜像方式扫描 `statev2.sqlite` 及 `statev2.sqlite-wal`；覆盖 T3 Code 内所有 Provider（`codex`、`claudeAgent`、`antigravity` 等）以及 Local / git worktree 线程，按 `projection_projects.workspace_root` 精确匹配 `workspace`。只采集 `streaming = 0` 的已完成 `user` / `assistant` 消息（跳过宿主通知类 `user` 行），并按 `(updated_at, rowid)` 推进游标；其他项目的截图与异常记录先行跳过，当前项目内未抽取的附件/图片继续保持 fail-closed（开启 `media_text_extraction` 后支持 `<source_root>/attachments/` 中的 `.txt`/`.md`/PDF 文本层）。

`workspace` 必须与原生记录的 cwd 字符串精确一致；不匹配或缺失的记录单独计入 `workspace_mismatch`，不改派到其他项目。当前采集仅接受项目 scope，global 偏好的提炼／共享另行实现。source_root 应尽量限定在该项目的会话目录。路径和原生会话标识不能替代 NAS 身份。

### 附件文本抽取（可选，默认关闭）

`media_text_extraction: true` 时，采集器在本地读取受支持的附件并把抽取结果当作普通文本入库；原始附件字节不上传、不写入 Hub、不发给模型。首期支持：

- PDF 文本层（≤50 页、≤20 MB）；扫描件／纯图片页因没有文本层会被拒绝。
- `text/plain`、`text/markdown` 文件（≤20 MB；抽取文本 ≤64 KiB）。

图片、扫描件、加密 PDF、Office 文档与 URL 引用仍保持拒绝；任何抽取失败都会停在原记录并返回固定错误码，不越过失败位置。派生文本带文件名、MIME、页数、字节数与 sha256 前缀，便于追溯；原件仍由原 Agent／现有文件同步管理。开启前请确认 PDF／文本中的 PII 与未知凭证可能未被现有过滤器完整覆盖。

```sh
cloud-memory-connector capture-init --state-dir /path/to/private-state \
  --connection-file /path/to/private-connection.json --source-file /path/to/private-source.json
cloud-memory-connector collect --state-dir /path/to/private-state \
  --connection-file /path/to/private-connection.json --source-file /path/to/private-source.json
```

capture-init 只登记来源、创建采集扩展和私有密钥，不扫描。对于 `t3-code`，附加 `--from-now` 可仅以只读方式将水位游标推进到当前最新已提交消息位置，在不读取或导入旧历史正文的前提下直接开启后续增量采集。collect 执行一批扫描，输出入队／重复／按原因跳过的数量；不发出网络请求。NAS 离线时仍可采集。也可用明确的 `--instance-id`、`--device-id` 代替 connection 文件进行纯本地验证；这不会授予服务器写入权限。

持续采集使用相同参数，将 collect 改为 collect-watch。它每批后等待两秒，再重新读取配置和扫描。网络补传 watch 在另一个进程运行，两者共享同一本地队列；本阶段尚未安装系统后台服务。Ctrl-C 结束进程，后续运行恢复已有状态。出现解析／媒体／权限／过滤错误会退出并返回固定错误码，不越过失败位置。已成功提交的前缀保留，不把部分完成报告成全量完成。

## 状态和恢复

- 来源目录身份使用 `root:v3`，包含目录创建时间；Linux 文件系统未提供该字段时拒绝采集。
  旧 `root:v2` 绑定升级后会报告 `capture_binding_mismatch`，`capture-init` 不会覆盖它。
  保留原状态库、队列、游标和 `capture-key`，不要删库重试。现有恢复方式是明确登记
  新来源 UUID，并在原状态目录为新来源执行 `capture-init`；这会建立独立游标，可能
  重新扫描、重复导入，不能作为自动、无损续采迁移。MCP 记忆与交接凭证不受此目录
  身份变化影响。设计与旧 Core 自选目录的重新确认规则见
  [ADR-0064](adr/0064-source-root-generation.md)。
- queued 表示过滤后的事件已在本机事务提交；accepted 表示 NAS 持久接纳。两者都不代表已提炼、可检索或已进入另一个 Agent。
- capture 扩展与队列同库，密钥单独保存在状态目录的 capture-key。密钥仅用于游标密封和本地身份 HMAC，不上传；恢复必须同时保留库和密钥。丢失／篡改时停止，不能删库重建来掩盖问题。
- 原生记录幂等，重复扫描不新建事件；过滤内容变化产生递增修订，已有事件不变。它不等同于用户纠错优先、冲突解决或删除传播。
- 文件长度和 mtime 保持不变的原地改写，通过本地 ctime 检测并重扫该文件；追加与截短继续使用上游锚点校验。完整文件全周期对账、删除检测和大历史争用性能仍需实现和测试。
- 未写完的 JSONL 末行留待后续扫描。损坏记录、超限或混合图片／文档块不按成功处理；默认拒绝附件，启用 `media_text_extraction` 后仅支持 PDF 文本层与文本文件（ADR-0075），图片与扫描件仍拒绝。
- 本批上限为 1000 条原生记录、每文件读取预算 8MiB、发现目标 10000；上游授权层单文件上限 2GiB。命中预算／规模限制须明确处理，未验证任意规模的全量导入。

## 已验证与待完成

已在真实 Claude Code CLI 的隔离合成会话中验证：两轮四条原生消息自动进入产品队列，采集器重启后不重复。宿主下一轮注入能力另由合成 hook 证明；产品召回 hook 已接通，见[自动召回说明](agent-recall.md)。

六入口、两设备全量导入与去重、删除防复活与一分钟目标仍保持待验收；自动后台安装与真实模型已分别有安装和 NAS 证据。历史导入按 `source_root` 显式授权，不自动扫描目录。
