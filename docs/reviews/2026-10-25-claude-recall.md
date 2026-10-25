# Claude 每轮在线召回验证

日期：2026-10-25。关联 FR-01、FR-02、FR-03、FR-05、FR-06、FR-07、FR-11、ENG-01。实现设计与上游不能直接复用部分的证据见 [ADR-0010](../adr/0010-agent-recall-hook.md)。

## 实际客户端证据

本机实际 Claude Code CLI 2.1.308，在专用临时配置与合成项目运行，同一进程连续四轮输入相同问题。产品 claude-hook 经实际 HTTP 访问 Hub，复用实际 Hy-Memory SDK 与本地 embedding。第一轮读取项目 V1 和 global 偏好；轮间由 SDK 更新同一记忆，第二轮读到 V2，最新注入不含 V1；第三轮撤销全部授权，最新注入为拒绝；第四轮模拟服务不可用，最新注入为不可用。其他项目记忆从未进入模型请求。

并行运行产品 collect-watch 与 watch。前两轮四条原生记录经实际 HTTP 获得接纳回执；后两轮四条仍保留在本地。原生记录与出站事件正文只包含合成用户／助手消息，未重复采集 hook 注入的记忆。SDK 实际队列有四条接纳任务；这不是四条提炼完成的记忆。观察 hook 只记录事件，不注入测试上下文。

探针：`make check-claude-recall`。日志 `.local/claude-recall/native-recall.log` 及其报告指向的私有临时证据不进入 Git。生成模型为本机 HTTP 测试替身，SDK 记忆为直接写入的合成夹具。没有调用外部生成模型，没有修改用户日常 Agent 配置或导入真实历史，不能据此宣称模型采纳质量、自动提炼、一分钟目标或两设备网络达标。

## 自动化检查与自审

定向 hook／读取测试 24 项通过。覆盖查询先过滤再截取、默认项目与显式 global、重复问题重新读取、两范围授权 revision 不一致、部分拒绝时整批丢弃、超时取消、所有输出字段的边界转义和 UTF-8 大小限制，以及实际 HTTP 进程中的撤销／离线错误。

完整 `make check-runtime` 175 项通过；原有 `make check-claude` 两轮采集探针继续通过；固定上游 `tests/test_hy_memory_bridge.py` 的 49 项回归通过；`make check` 通过。对应日志分别为 `.local/claude-recall/runtime.log`、`native-capture-regression.log`、`upstream.log`、`check.log`。上游完整 core-pr 集本轮未重复运行，本轮没有新增上游补丁；生成模型、两机网络和正式客户端安装不在这些检查内。

首次测试中四项构造样例缺少必填 content_offset，补齐后 24 项通过，没有放宽产品合同。工程检查发现探针中一行超过长度限制，已调整换行。开发者自审检查了上下文来源、预算、授权、出站过滤、失败不阻断和配置边界；不构成独立同行审查。

## 容器制品与 NAS 验证

运行源码 `234244bb7f3c2b2b517d74871f01c60522c60160` 的 Linux amd64 镜像为 `cloud-memory-hub:dev-234244b`。本地和 NAS 的独立临时容器均通过完整探针：禁网 embedding、认证／配置、设备与来源授权、过滤接纳、原生格式采集投递、实际 SDK 检索与已安装 recall hook。重启后能读取；撤销权限再重启后，hook 返回 denied，且输出没有记忆正文。

容器内测试通过已安装命令输入合成 hook JSON，并非在 NAS 安装 Claude CLI；Mac 实际 CLI 的证据单独列在上文。NAS HTTP 测试在容器内部进行，不能替代 Mac 到 NAS 的正式 HTTPS 入口验证。日志为 `.local/claude-recall/container.log`、`nas.log` 和 `transfer.log`。

| 制品 | 摘要／大小 |
| --- | --- |
| 归档 SHA-256 | `62f54c8c0a781734cb6ef4e48ee39f55d378681484ac7a208e671e3b93431da0` |
| 归档字节数 | 261231616 |
| 本地 manifest-list | `sha256:59d1df2858e5574417f52b170a780605759702532de3f22a08f99946a51cf789` |
| NAS 镜像配置摘要 | `sha256:998f3e86b3347e9fe8f09e9d0b8dc235c12aac54d01dd047d30d60341b7b82c5` |

NAS 加载前核对归档 SHA-256，加载后核对配置摘要、完整提交号及 linux/amd64；没有将 manifest-list 与配置摘要混用。源码和镜像均不包含运行卷、真实历史或实际凭证。

常驻实验项目 `cmh-nas-validation` 经确认无设备／来源接入、无接纳记录、空记忆库且未激活模型后，保存并核对 config／data 私有归档（0600，74240／563712 字节），完成 `93fc63d → 234244b → 93fc63d → 234244b`。各阶段健康，实例 ID、管理员凭证与加密密钥摘要、SDK 权威状态和空库保持一致。镜像设置已持久保存，旧镜像与备份保留；日志为 `.local/claude-recall/upgrade.log`。

最终实测为 `cloud-memory-hub:dev-234244b`，配置摘要与上表一致，3 GiB／2 CPU、只读根目录、cap_drop=ALL、127.0.0.1:8800。无需 sudo，未调整 SSH 设置；NAS 浏览器入口仍未验收。此回滚只验证当前空实验实例的兼容性，不替代带真实内容与删除规则的恢复演练。

## 剩余验收

离线召回缓存、治理删除／恢复防复活、正式安装与卸载、其他五种客户端、真实提炼模型和两设备一分钟目标仍未完成。权限撤销仅防止新读取，不能清除已经返回给原 Agent 的历史上下文。服务没有新增数据库迁移或修改服务器读协议，撤销此功能可移除专用 hook，既有采集和读取仍按原合同工作。
