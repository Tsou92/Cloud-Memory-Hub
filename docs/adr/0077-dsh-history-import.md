# ADR-0077 DeepSeek Harness 历史导入

状态：已实现并随 v0.1.13 发布（2026-11-19）。日期：2026-11-19。关联 FR-08、FR-14、FR-15。

## 背景

DSH 已有插件做每轮自动采集，但历史会话只在 `~/.dsh/sessions` 中，未接入现有 source adapter。DSH 每个会话保存为：

```
~/.dsh/sessions/<编码后的工作目录>/session-<uuid>/session.v3.jsonl.zstd
```

zstd 压缩的 JSONL，事件带 `type`、`seq`、`time` 和 `data`。上游 `JsonlSourceAdapter` 已支持 `.zstd` 文件并按解压后的记录序号续读，因此不需要另写一套读取、游标和授权层。

## 决定

新增 `deepseek-harness` 来源 adapter：

- `source_root` 指向 `~/.dsh/sessions` 或该项目对应的编码目录；只读取 `session-*/session.v3.jsonl.zstd`。
- 会话头 `type=session, version=3` 提供 `cwd`（工作目录）和 `id`（会话 ID）；`workspace` 必须精确匹配 `cwd`，否则计入 `workspace_mismatch`。
- 映射：
  - `user/message`：仅 `source.kind == "user"` 的文本内容；插件消息计为 `not_written_by_person`。
  - `assistant/message`：`text` 内容作为 assistant 行；`tool-call` 块作为同一记录的 tool 行；`reasoning` 不导入。
  - `tool/result`：`tool-result` 内的 text 内容作为 tool 行，保留 `callId`。
  - `system/message`、`turn/start`、`turn/end`、`agent/inbox/spliced`、`tool/call`（与 assistant 记录重复）等不生成行。
- 工具证据沿用上游裁剪与凭证清理，并额外去掉终端控制/格式字节（保留换行和制表符）、把 URL 替换为 `[url]`。真实终端输出常含 ANSI 控制符和查询值像不完整 JSON 的链接，隐私过滤器会分别以 `hidden_control_character`／`invalid_json` 中止整批；工具证据是上下文而非主记忆，因此做上述收敛，用户/助手文本仍保留 URL 走正常过滤。
- 含图片／附件等不支持媒体时保持 fail-closed；启用 `media_text_extraction` 后只对支持的 PDF 文本层/文本附件走 ADR-0075 的本地抽取。

## 兼容与边界

- 适配器 ID 与实时 DSH hook 一致，均为 `deepseek-harness`；历史来源使用独立 source UUID，不覆盖实时来源。
- zstd 流没有可按字节恢复的偏移；游标是解压后的记录序号。文件被重写时由现有 ctime/大小检测触发重读，原生消息 ID 保证幂等。
- 会话头缺失或版本不是 3 时，记录没有 workspace，按现有逻辑跳过，不猜测目录名。
- 只导入文本与工具证据；DSH 的 reasoning、系统消息、命令控制事件和图片不进入 Hub。工具结果裁剪上限沿用上游 1600 字符。

## 验证

- 合成 `session.v3.jsonl.zstd`：会话头、真实用户消息、assistant 文本 + tool-call、tool/result、插件消息、system、turn/start、tool/call 均覆盖；队列 4 行，跳过原因符合预期；二次采集 0 新增。
- workspace 不匹配时全部计入 `workspace_mismatch`。
- 含 document 时默认 `source_media_not_supported`；启用 `media_text_extraction` 后只上传派生文本，不出现 base64。
- 工具结果中的控制字节被移除，不触发隐私过滤中止。
