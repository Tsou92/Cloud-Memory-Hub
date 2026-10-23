# 本地连接组件的隔离验证

当前实现本地过滤和持久投递队列，还不发送网络请求，也不自动查找、安装或修改任何 Agent。源注册、自动采集、服务器接纳、离线召回缓存和删除协调仍待接通。本阶段请使用合成记录，不导入真实历史。

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
| `content` | 非空 JSON 对象；对话正文、工具结果和所有自由元数据都放在这里 |

顶层未知字段拒绝。成功仅返回事件 ID、`queued`／`accepted` 状态和可用的接纳回执 ID，不打印正文。`queued` 表示本机提交成功；`accepted` 表示匹配的服务端持久接纳回执已保存，仍不等于记忆已处理或另一台设备可检索。本批 CLI 没有网络发送或人工确认成功的命令。

可选 `--known-credentials-file` 指定 0600 私有文件，内容为凭证字符串数组，最大 4 KiB。它只在进程内读取，不复制到事件或队列；不要把其内容粘贴到聊天或日志。该文件不是 NAS 连接认证配置。

## 失败与恢复

- 过滤、合同验证和无法解析的媒体失败只保存固定原因计数，不保存原始失败正文。原 Agent 文件不修改；自动来源重试／隔离管理尚未实现。
- 每次出队重新核对摘要和当前过滤策略。若新的已知凭证规则要求再次过滤，停止投递，等待后续显式修订流程，不修改旧事件身份。
- 队列选取不删除内容，确认丢失可用同一 ID 重放；不匹配或冲突的回执拒绝。并发重复入队也只能保留一条。
- 该队列不是备份，也不代表已实现断网召回或删除传播。不要删除库以消除错误；保留本地状态供诊断。
