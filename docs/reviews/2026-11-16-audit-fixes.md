# 2026-11-16 审计修复记录

日期：2026-11-16。基线 `2f935d7`（v0.1.8 发布提交），分支 `fix/audit-console-correct-history-search`。

## 范围与方法

对全仓库做多视角只读审计（用户上手旅程、核心链路正确性、安全与隐私、测试与门禁、过度设计、发布工程），每条 high／medium 发现再独立复核一次。审计结论 29 条确认、0 条推翻；本轮修复其中影响用户与数据安全的 9 条，其余按下文记录保留。

## 已修复

| 问题 | 位置 | 影响 |
| --- | --- | --- |
| 后台「保存修改」必然 503 | `console_correct.py`、`unified_read.py` | 把 bridge 工厂当实例调用；0.1.8 宣称的记忆纠错实际不可用 |
| 纠错写入只读投影 | `unified_read.py` | 原始原文与 productivity 记录不是可写 SDK 记忆，现在明确拒绝而不是透传 |
| 历史原文搜索满页即报错 | `transport.py` | `HistorySearchRequest` 被当作非列表请求，任何带 `next_offset` 的回复都被判 `remote_read_mismatch` |
| 搜索翻页永不结束 | `remote_read.py` | 超过 50 条搜索窗口后返回空页且 `next_offset` 无限增长 |
| 长会话挤掉其他会话摘要 | `source_history.py` | 排除当前会话放到 SQL，当前会话超过 48 行后其他摘要不再静默消失 |
| 入库不复核管理员凭证 | `remote_ingest.py`、`app.py` | 粘贴进对话的管理员密码／加密密钥会落库并进入外部模型提炼 |
| CHANGELOG 缺 0.1.8 | `CHANGELOG.md` | 已发布内容标成「未发布」，并有一份重复段落 |
| 版本标题无人校验 | `release_artifacts.py` | 新增发布门禁：CHANGELOG 没有对应版本标题就拒绝发布 |
| DSH 插件测试从未执行 | `.github/workflows/quality.yml` | 5 个插件测试加入 CI |
| 真实接线缺 `search_sdk_raw` | `unified_read.py` | 实机测试发现：搜索接口拿到的是 `UnifiedReadBridge`，该桥没有此方法，0.1.8 的原文搜索在真实部署里必定 503 |

## 同时处理的可用性问题

- 安装器完成时打印安装目录；重置密码、备份、升级命令此前都没写清要在哪个目录执行。
- README 与 NAS 安装文档补上 `curl`、`sha256sum`、`tar`、`flock`、`python3` 前置条件。
- 初始化页为本机 Docker 用户补充可执行入口，不再只给 NAS 的 compose 命令。
- Windows 引导脚本补上 `-ServePort`，与 Mac/Linux 的 `--serve-port` 对齐。
- `docs/release-plan.md` 的当前公开版从 v0.1.6 更正为 v0.1.8，并链接发布记录。

## 未修复（保留为后续范围）

- **备份归档未加密**：`export_backup` 打出的 tar 里含管理员 Bearer 与 Fernet 密钥。正确做法是用用户口令加密整个归档，属于协议级改动，不在本轮小修范围。导出前请按文档把归档当凭证对待。
- **弱测试断言**：`last_accepted_at` 的断言已收紧为必须是整数。
- **重复实现**：`reconcile_evidence`／`input_context` 的归档代码、三处权限三元组、五处 `HubConfig` 离线构造属于重构，改动面大于收益。
- **登录失败限速可被用来锁死管理员**：需要重新设计计数维度，涉及认证语义，另开变更。
- **legacy 管理员密码以明文作 Bearer**：这是既有兼容行为，替换需要单独的迁移与兼容说明。

## 验证

- `make check` 通过：ruff format／check 干净，206 项 unittest 通过（23 跳过），仓库路径与凭证扫描通过。
- 每条修复都有回归用例，并在撤掉源码修复后确认对应用例失败：
  - 纠错接线 4 项、只读投影 1 项
  - 搜索分页 1 项
  - 会话摘要窗口 1 项
  - 入库凭证 1 项（撤掉修复后事件返回 202 而非 422）
- `apps/hub` 完整 pytest：先获取内置嵌入模型（`fetch_embedding_model.py`），再运行 **2344 通过、6 跳过、0 失败**。
  未获取模型时该套件有 196 项因 `MissingEmbeddingAssetsError` 失败；改前与改后的失败清单 `diff` 完全一致，证明与本轮改动无关。

## 实机验证（NAS）

在本机 Docker 不可用的情况下，用真实 NAS（自建 `ssh nas`，x86_64）从分支源码构建镜像，另起独立端口 18800 与独立数据卷的实例，不接触生产容器：

- 镜像构建成功，容器健康检查通过。
- 20 项真实 HTTP 断言全部通过，覆盖：项目／设备／来源建立，入库拒绝 hub 自身管理员令牌（422 且不回显），事件接纳，长会话后其他会话摘要仍可见，纠错返回 404 而不是 503，历史原文搜索首页 10 条 / 次页 5 条 / 越过 50 条窗口返回空页且无游标，以及 connector 客户端接受满页。
- 首轮实机测试暴露了 `UnifiedReadBridge` 缺 `search_sdk_raw`（见上表），修复后重建镜像复测通过。
- 测试结束后已删除测试容器、三个测试卷、构建目录和测试镜像；生产 `cloud-memory-hub-hub-1` 仍运行 `cloud-memory-hub:v0.1.8`，状态未变。

## 剩余风险与回滚

- 本轮没有在生产实例上升级验证，也没有执行 `make check-container`／`check-web`；实机证据来自独立测试实例。
- `apps/hub` 需要内置嵌入模型才能跑全量；干净检出首次运行需先执行模型获取脚本。
- 所有改动都在独立分支，单次 `git checkout main` 即可回滚；无数据库、协议或存储变更。
- 已发布的 v0.1.8 GitHub Release 正文由旧 CHANGELOG 生成，仓库修复不会改写它；如需更正要单独编辑 Release。
