# ADR-0004：远程设备身份与项目授权

日期：2026-10-23。状态：分步实现中；关联 FR-02、FR-03、FR-06、ENG-01。

## 复用证据与缺口

固定上游 `b7d9f0e` 的 `host-bridge/src/host-grants.ts` 是可信本机安装器的 TypeScript 授权库：身份由 `isHostId` 限制，scope 是本机 workspace/source，注释明确排除 HTTP 认证与跨重启旧库恢复。Core 的 `imports/grants.py` 授权文件根及 inode 身份；`auth.py` 是单一回环管理员凭证。`memory_server/daemon_contract.py` 的 GrantState 管理进程内开关，不保存远程身份。

将 NAS 设备伪装成这些本地主机／文件根会扩大既有授权语义。新增远程身份库，仅补足稳定设备 ID、项目 ACL 和 HTTP 凭证；不复制本机授权库，不改上游数据表，不另建记忆队列。Mac 端仍复用上游安装器和来源授权。管理员认证、会话、Origin／Host 保护继续使用现有 Hub 边界。

## 协议与信任边界

```text
HTTPS + 精确 Host/Origin
  ├─ 管理员凭证或有效网页会话 -> /hub/v1/projects、/hub/v1/devices
  │                                -> 创建、授予、轮换、撤销
  └─ 独立设备 Bearer -> /hub/v1/session -> 当前身份与明确授予的 scope
                       后续数据接口 -> 每次重新核对权限 -> 复用 Core/SDK
设备身份绝不转换成 Core 管理员身份；未知 /hub/v1 路径不回退到 Core。
```

- 单用户 NAS 中心，不新增多租户产品需求。服务器生成 instance、device、project UUID，设备名称和本地目录不作为身份依据。
- 项目 scope 为服务器创建的项目 UUID；`global` 专用于跨项目偏好，必须单独授予。默认设备无内容权限，不支持通配符或凭客户端自报项目获得授权。
- 一个设备代表一个连接组件安装；来源与 Agent 标识随后独立注册，不枚举或限制 Agent 品牌。历史源 ID、修订及来源设备继续保留。
- 每个 scope 明确列出 `events:write`、`memory:read`、`history:read`、`sync:read`。这些是授权声明，不代表对应数据接口已经实现；握手单独公布实际能力。
- 设备凭证含设备 UUID 和 256 位随机秘密，仅签发／轮换响应返回一次。数据库仅保存完整凭证的 SHA-256 摘要；高熵随机凭证不使用人类密码的口令算法。响应禁止缓存，日志和异常不返回凭证或请求正文。
- 每次请求读取当前权限；轮换立即使旧凭证失效。撤销不可反向激活，重新接入需要新身份。权限变更、轮换及撤销携带 `expected_revision`；SQLite 事务保证并发修改只有一个能提交，过期版本返回 409。
- 已开始的内容操作在发布／返回结果前还须重新检查权限版本；本批身份接口不能代替后续内容级授权与撤销测试。

## 本批实现的 v1 接口

| 路径 | 方法与权限 | 行为 |
| --- | --- | --- |
| `/hub/v1/projects` | 管理员 GET／POST | 列出／创建项目；创建体 `{display_name}` |
| `/hub/v1/devices` | 管理员 GET／POST | 列出／签发零权限设备；创建体 `{display_name}` |
| `/hub/v1/devices/{device_id}/grants` | 管理员 PUT | `{expected_revision, grants:[{scope_id, permissions}]}` 完整替换该设备授权；空数组撤掉全部内容权限 |
| `/hub/v1/devices/{device_id}/rotate` | 管理员 POST | `{expected_revision}` 轮换凭证，响应只返回一次新凭证 |
| `/hub/v1/devices/{device_id}/revoke` | 管理员 POST | `{expected_revision}` 撤销设备 |
| `/hub/v1/session` | 设备 GET | 返回 protocol_version、instance_id、设备 ID／revision、当前 grants 与实际 features |

错误使用固定 code：401 无效设备身份、403 管理员身份缺失／权限不足／跨源、404 未知资源、409 版本或状态冲突、413 请求超过 64 KiB、422 输入不合法、503 身份存储不可用。输入严格拒绝未知字段、重复 scope／permission、未知项目和非整数 revision；不在错误里回显输入。每次授权替换最多 256 个 scope。客户端不得缓存授权结果作为后续请求的依据。

## 存储与兼容性

新增配置卷内的 `remote-access.sqlite3`，包含 schema version、instance ID、项目、设备及授权；不修改 Core 的数据库或 SDK 文件。文件为 0600，父目录受控，拒绝符号链接、硬链接和过宽权限。事务采用 SQLite 内置锁和条件版本核验；复用标准库，不新增服务或依赖。

老实验实例升级时创建空身份库，不授权任何旧客户端；旧管理接口及凭证继续有效。降级保留此文件但旧镜像不读取；不得删库以“修复”错误。未知 schema version、损坏文件或运行时丢失数据库均拒绝身份请求，不偷偷重建。新实例的 instance ID 不同，客户端必须重新配对，不能盲目重放旧缓存。

记忆恢复不得覆盖当前授权／撤销库。完整配置灾难恢复还需要独立的最新授权与删除状态核对；在相应恢复设计实现前不提供自动恢复入口，不宣称已解决旧备份导致撤销或删除复活。此限制保留 FR-11／FR-13 的完整要求。

## 验证与分步交付

先完成身份库、HTTP 管理与握手、真实 Core 边界集成。覆盖持久化、无明文、项目／全局隔离、未知项目整笔拒绝、旧 revision、并发更新、凭证轮换、撤销及重启、文件／schema 失效、CSRF、设备不能访问 Core、错误不泄密和超大请求。同步存储操作放入线程池，避免阻塞 ASGI 事件循环；普通请求不写 last-seen，避免轮询制造持续写锁。

随后接入源身份、本地过滤和离线队列，再复用 Core 任务／SDK 实现写入与检索，并补客户端、时效及治理验收。首次签发响应丢失时管理员重新轮换／签发，不保存可恢复的明文；重复管理操作按 revision 处理。

架构自审：单一 ASGI 边界、无新增守护进程，身份与内容权限分离；质量自审：独立 schema，不修改上游授权意义；测试：上述正常／失败／并发路径及真实 Core；性能：索引主键查询、短事务与线程池。此为开发者自审，不是独立同行审查，也不把身份功能当作跨设备完整验收。
