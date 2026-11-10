# ADR-0066：参考图后台与真实首页投影

日期：2026-11-10。关联 FR-01、FR-03、FR-10、FR-12、ENG-01。状态：实现中。

## 决定

按用户指定截图统一后台和官网，视觉规范见 [DESIGN](../../DESIGN.md)。复用 HubConsole、HubContext、模型／记忆／原文／配对／备份组件及现有管理员会话，不重写业务流程。网络节点由当前项目实际有效来源连接生成，完整分页、按来源 ID 去重；已停用来源、已撤销设备及没有当前项目读写授权的连接不显示。旧来源未记录 Agent 品牌，不根据显示名猜测品牌或在线状态。

新增只读 `POST /hub/v1/console/dashboard`，请求沿用 ScopeRequest，管理员鉴权和同源 POST 规则与 console/summary 相同。响应为追加入口，不更改旧接口、数据库、设备凭证、MCP 或安装协议。

## 数据合同

- 响应包含 schema_version=1、instance_id、scope_id、generated_at，以及 statistics、record_types、daily_activity、activity_timezone、activity_state、missing_timestamps。
- statistics 包含 device_connections、agent_connections、shared_records、handoffs，均为非负整数。设备连接按有效来源的 device_id 去重；Agent 连接按 source_id 计数。有效权限为 events:write、memory:read、history:read 至少一项。
- shared_records 是本项目当前未被忘记／删除屏蔽的接纳事件数，包括各个 revision；不是提炼后的 SDK 记忆条目数。record_types 固定为 conversation、remember、handoff、other 四项，分别提供 kind、label、count。handoff／remember 只识别 operation 中 agent_access_version=1 的对应元数据。
- 继续调用既有已校验 suppressed(scope_id)；在结果发布前重新核对。近期正文继续走原 history-list/get 的完整性和隐私检查，摘要接口不返回正文、凭证、请求身份或另一项目的数据。
- 七日活动按 Core 持久队列的服务端 created_at 统计；同时匹配接纳回执 request_id 与 receipt_id。日期为 UTC，包含当天和此前六日。来源消息时间、模型处理时间和页面加载时间均不替代接纳时间。
- activity_state=ready 时返回连续七个日期桶，missing_timestamps=0。没有记录的项目返回七个零。任意可见回执无法核对时间时返回 unavailable、空日期数组与无法确认的数量，不将未知当零。计数或授权无法确认则整个请求失败，前端展示错误并明确旧数据状态。
- 不新增心跳、Docker 守护进程访问、硬件监控或后台轮询采集库。光点为可关闭的共享链路示意动画，不能冒称实时传输事件。

## 复用与兼容

复用 cmh_event_receipts 的范围索引、已有来源／设备／grant 表，以及 Core main_database 的只读连接池读取队列时间元数据。分页读取必要的回执身份与 SQL 分类，避免向浏览器或 Python 投影全部正文；无新的索引库或数据迁移。所有显示名沿用隐私过滤和 React 文本渲染。

光球为本次生成的静态素材，以固定路径提供；与公共安装页面同样先校验 Host/Origin，只允许 GET/HEAD。登录页和连接引导只换样式，原凭证、签发、一次性消费与未知结果恢复行为保持。

## 验证场景

管理员／设备／未登录／跨源请求；两个项目与 global 隔离；只读连接、停用来源、撤销设备和撤销 grant；超过一页的节点；0／1／多节点；真实接纳分类、忘记与时间缺口；UTC 日期边界；损坏元数据拒绝；错误／空状态和 reduced-motion；桌面、平板与窄屏；既有配对、读取、问答、忘记、模型及备份操作回归。执行结果另行登记，设计不视为通过。
