# 本地连接组件的隔离验证

当前实现本地过滤、持久投递队列、send／watch 网络发送、[授权读取 CLI](remote-read.md) 和[显式授权的来源采集](source-capture.md)，CLI 不自动查找、安装或修改任何 Agent。服务端已提供[来源注册和持久接纳接口](remote-ingest.md)；日常客户端安装、离线召回缓存、客户端注入和删除协调仍待接通。本阶段请使用合成记录，不导入真实历史。

协议与限制见 [ADR-0005](adr/0005-local-filtered-delivery.md)。组件复用项目中固定版本的上游文本脱敏器，并补充 Cookie、中文凭证标签、嵌套结构和 URL 规则。已知凭证可通过私有 JSON 文件在本机内存中加入过滤；不能据此宣称任意格式／多模态秘密均已识别。

## 初始化与状态

先按 [运行指南](development-runtime.md) 准备固定上游和 Python 运行环境。以下 UUID 仅供隔离演示，不表示已经配对 NAS；正式配对必须使用服务端签发的身份，当前未提供配对命令。

```sh
CMH_DEMO_INSTANCE=11111111-1111-4111-8111-111111111111
CMH_DEMO_DEVICE=22222222-2222-4222-8222-222222222222
uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub \
  cloud-memory-connector init \
  --state-dir ../../.local/connector-demo \
  --instance-id "$CMH_DEMO_INSTANCE" --device-id "$CMH_DEMO_DEVICE"
```

`--state-dir` 相对于运行进程工作目录；上例 uv 将目录切换到 `apps/hub`，因此状态位于仓库忽略目录 `.local/connector-demo`。也可明确传入自己选择的绝对路径，但不要将个人路径提交 Git。

重复 `init` 会拒绝覆盖。查看状态时使用同样参数，将 `init` 改为 `status`。库丢失、权限异常、版本未知或 instance/device 不匹配会失败，不自动重建或换绑。

## 入队

将单个事件 JSON 通过标准输入传给 `enqueue`，参数与初始化一致，不把正文写在命令行参数中。事件包含：

| 字段 | 内容 |
| --- | --- |
| `protocol_version` | 整数 `1`，可省略 |
| `event_id` | 本次不可变事件 UUID；重复投递使用同一个 ID |
| `instance_id`、`device_id` | 与队列绑定一致 |
| `source_id`、`scope_id` | 来源与项目 UUID；全局范围需明确 `global`，不代表获得服务端权限 |
| `conversation_id`、`record_id` | 稳定的不透明 UUID；原生会话／消息 ID 的本地映射随后由适配器维护 |
| `revision` | 正整数；记录修改使用新事件 ID 和新的 revision |
| `kind` | `conversation_turn` 或 `operation` |
| `occurred_at` | 可选带时区时间 |
| `content` | 对话为 `{messages:[{role,text,metadata?}],metadata?}`，操作为 `{text,metadata?}`；正文、工具结果和所有自由元数据都放在这里，服务端严格验证对应结构 |

顶层未知字段拒绝。成功仅返回事件 ID、`queued`／`accepted` 状态和可用的接纳回执 ID，不打印正文。`queued` 表示本机提交成功；`accepted` 表示匹配的服务端持久接纳回执已保存，仍不等于记忆已处理或另一台设备可检索。CLI 不提供人工把事件标记成功的命令。

可选 `--known-credentials-file` 指定 0600 私有文件，内容为凭证字符串数组，最大 4 KiB。它只在进程内读取，不复制到事件或队列；不要把其内容粘贴到聊天或日志。该文件不是 NAS 连接认证配置。

## 配置与持续发送

传输设计及复用证据见 [ADR-0007](adr/0007-connector-transport.md)。管理员先创建项目、签发设备并授予 events:write，再为设备注册来源。当前尚无自动配对／安装界面，身份和来源通过上述管理 API 准备；不把管理员凭证放到连接组件。

使用自己控制的私有文件保存连接信息，例如 `.local/connector-demo/connection.json`：

```json
{
  "schema_version": 1,
  "origin": "https://memory.example.test",
  "instance_id": "<服务器实例 UUID>",
  "device_id": "<服务器签发的设备 UUID>",
  "credential": "<该设备的独立凭证>"
}
```

示例中的值必须替换为隔离验证实例的实际签发信息；文件为当前用户所有、权限 0600，父目录使用 0700，不允许符号链接／硬链接／FIFO。不要把凭证放到命令参数、聊天或 Git。远程连接要求 HTTPS 并校验证书；HTTP 只允许回环地址。origin 不带路径或尾部斜杠，也不带用户名、查询参数或 fragment。配置版本、实例或设备不匹配时停止，不自动换绑已有队列。

提供 `--connection-file` 后，init／enqueue／status 可以从中取得 instance/device，不必再传两个 ID；若同时传入必须一致。入队时也会把当前设备凭证加入已知秘密集合。新队列先执行一次 init；已有队列不能重新初始化。

```sh
uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub \
  cloud-memory-connector send \
  --state-dir ../../.local/connector-demo \
  --connection-file ../../.local/connector-demo/connection.json
```

`send` 最多处理 32 条待传事件。改为 `watch` 会持续检查队列并自动补传：正常间隔 2 秒，满批继续发送；连接失败、超时、408／425／429 或 5xx 使用带抖动的指数退避，最长 30 秒。按 Ctrl-C 结束，尚未确认的事件仍保留。watch 每轮重读连接及已知凭证文件，支持显式更换私有凭证文件后的下一轮验证；不会安装后台服务或修改任何 Agent。

每批先握手，每条发送前核对当前来源／项目授权；不跟随重定向。一次网络请求完整期限 20 秒、响应上限 128 KiB。实例变更、来源撤销、401／403、冲突、无效响应和正文拒绝会停止传输并保留队列。检查固定错误码后处理权限／配置／事件问题，再启动；不通过删库或改事件 ID 消除冲突。

成功输出 accepted／queued 计数；持续模式只输出状态变化或本批接纳数。退出码 0 为单次完成、2 为需要处理的错误、3 为可重试网络错误，Ctrl-C 为 130。accepted 只代表 NAS 持久接纳；watch 不是完整同步或离线召回服务，删除抑制状态协调仍待加入正式启用流程。

## 失败与恢复

- 过滤、合同验证和无法解析的媒体失败只保存固定原因计数，不保存原始失败正文。原 Agent 文件不修改；自动来源重试／隔离管理尚未实现。
- 每次出队重新核对摘要和当前过滤策略。若新的已知凭证规则要求再次过滤，停止投递，等待后续显式修订流程，不修改旧事件身份。
- 队列选取不删除内容，确认丢失可用同一 ID 重放；不匹配或冲突的回执拒绝。并发重复入队也只能保留一条。
- 该队列不是备份，也不代表已实现断网召回或删除传播。不要删除库以消除错误；保留本地状态供诊断。
