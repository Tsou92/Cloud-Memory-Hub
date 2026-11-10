# 本地来源采集：隔离验证

当前支持显式绑定的 Claude Code 原生 JSONL 文字与工具记录，复用固定上游解析器和授权文件读取。它将过滤后的记录写入现有 LocalOutbox；发送继续使用 [send／watch](local-connector.md)。设计与兼容性见 [ADR-0009](adr/0009-local-source-capture.md)，实际 CLI 证据见 [验证记录](reviews/2026-10-24-claude-native.md)。

这是开发阶段的连接能力，不是正式安装器。本阶段使用新建合成目录，不接入日常 Agent 配置或真实历史。首轮 collect 会从指定目录的历史起点开始，后续按游标增量采集；不会自动猜测 `~/.claude` 路径。

## 显式配置

先按[连接组件说明](local-connector.md)初始化本地队列并准备私有 connection 文件。NAS 管理端登记的来源 adapter ID 应为 `claude-code`，并绑定同一设备与项目；来源 UUID 和项目 UUID 使用服务端返回值。

在源码目录之外创建权限为 0600 的 source 文件，父目录由当前用户控制。字段如下，尖括号内容必须替换：

```json
{
  "schema_version": 1,
  "adapter_id": "claude-code",
  "source_id": "<来源 UUID>",
  "scope_id": "<项目 UUID>",
  "source_root": "/absolute/path/to/synthetic-transcripts",
  "workspace": "/absolute/path/to/synthetic-project",
  "source_version": "<实际 CLI 版本，可省略此字段>"
}
```

`workspace` 必须与原生记录的 cwd 字符串精确一致；不匹配或缺失的记录单独计入 `workspace_mismatch`，不改派到其他项目。当前采集仅接受项目 scope，global 偏好的提炼／共享另行实现。source_root 应尽量限定在该项目的会话目录。路径和原生会话标识不能替代 NAS 身份。

```sh
cloud-memory-connector capture-init --state-dir /path/to/private-state \
  --connection-file /path/to/private-connection.json --source-file /path/to/private-source.json
cloud-memory-connector collect --state-dir /path/to/private-state \
  --connection-file /path/to/private-connection.json --source-file /path/to/private-source.json
```

capture-init 只登记来源、创建采集扩展和私有密钥，不扫描。collect 执行一批扫描，输出入队／重复／按原因跳过的数量；不发出网络请求。NAS 离线时仍可采集。也可用明确的 `--instance-id`、`--device-id` 代替 connection 文件进行纯本地验证；这不会授予服务器写入权限。

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
- 未写完的 JSONL 末行留待后续扫描。损坏记录、超限或混合图片／文档块不按成功处理；目前不支持附件解析。
- 本批上限为 1000 条原生记录、每文件读取预算 8MiB、发现目标 10000；上游授权层单文件上限 2GiB。命中预算／规模限制须明确处理，未验证任意规模的全量导入。

## 已验证与待完成

已在真实 Claude Code CLI 的隔离合成会话中验证：两轮四条原生消息自动进入产品队列，采集器重启后不重复。宿主下一轮注入能力另由合成 hook 证明；产品召回 hook 尚未接通，不能把这两项拼成已完成的双向闭环。

六入口、两设备全量导入与去重、离线召回、删除防复活、自动后台安装、真实模型与一分钟目标均保持待验收。现阶段的能力不授权导入真实历史。
