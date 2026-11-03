# 基础画像旁路证据验证

日期：2026-11-03。关联 FR-08、FR-09、FR-10、FR-11、FR-12；设计见 [ADR-0028](../adr/0028-basic-profile-evidence.md)。

## 实现与评审

补丁 0015 复用原生 L0 基础画像更新，在唯一一次旧状态读取后、写入前冻结实际规则输入。通过原 SDK 结果和幂等回执持久保存输入、尝试/确认新建、尝试/确认旧头更新。部分旧头更新失败仍保持上游原成功返回，但证据明确 unavailable；取消或写入后异常也保留已尝试 ID。超限不会把不完整副作用列表解释为完整结果。

新画像以独立摘要域绑定生产请求和结构化 KV，保留原 user-level 范围、只存差异、迁移及 meta 行为。Chroma 会把空 agent_id/session_id 解码为 null；真实 SDK 探针发现此格式差异，摘要仅对画像的这两个空身份值作等价处理，其他来源域和后端原行为保持。

架构、代码、测试、性能与权限已顺序自审，无独立同行审查。新增捕获不增加数据库读取或模型调用，原 BasicProfileUpsertResult.to_dict 不携带大快照，避免在过程日志重复留存。新增 profile 字段不混入普通 memory_ids；Hub 还需单独处理画像的用户级产出及来源关系。

## 当前已执行

- make check-runtime：589 项通过、5 项可选后端测试跳过、269 条既有上游弃用警告；跳过项另由 make check-memory-origins 加载固定 FAISS/Qdrant 依赖运行，三后端共 28 项通过。新增画像测试共 19 项，包含取消、部分失败、预算、非有限值、并发和实际 Chroma 格式。
- make probe-profile-evidence 的本地开发环境调用链通过：仅返回 basic_info 的合成提炼，跨三个 Agent/会话执行新增、差异更新、无变化；add_extracted 明确 not_used；独立进程两次重开，禁用 writer 后仍能重放原持久证据。存储、SDK 与 Core 实际运行，外部模型被合成响应替代。
- 既有 make probe-memory-origins 通过，普通记忆来源与跨进程重放保持；make check 的格式、静态检查、19 项工程门禁、链接和凭证扫描通过。
- 上游画像 meta、画像 API、MemoryNode、payload-only、写入结果及缓存回归 59 项通过。首次命令缺少 pytest-asyncio，出现测试执行器错误；补充已缓存的固定 pytest-asyncio 1.4.1 后重跑通过，未将首次失败计为通过。
- 补丁清单与固定上游组合校验通过；运行环境使用锁定依赖缓存。首次 uv 工具解析遇到 TLS 失败，切换 UV_OFFLINE=1 使用现有缓存，未改变依赖或降低 TLS 校验。

安装/NAS 验证结果在完成后补充。NAS 现有 d3577f9 实例本轮只读检查为 running/healthy。本阶段尚未接通 Hub 画像来源图；后台派生任务、删除执行、恢复防复活及六入口两机验收仍待完成，不能据此宣称完整产品可用。
