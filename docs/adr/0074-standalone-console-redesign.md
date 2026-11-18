# ADR-0074 独立自有管理后台与交互架构重构

状态：实现中。日期：2026-11-18。关联 FR-01、FR-03、FR-07、FR-09、FR-10、FR-12、FR-13、FR-14、ENG-01。

## 背景与上游复用边界证据

此前管理网页通过 20 余个递增补丁（`0040`、`0043`～`0059`、`0061`）叠加在固定上游桌面应用 `@one-memory/web`（`.local/upstream/hy-memory/memhub/apps/web`）之上，通过 `VITE_CMH_CONSOLE=true` 分支切换挂载。随着多设备接入、记忆纠错、原文归档检索、治理预览／删除、受控备份与模型费用估算逐步上线，原有耦合方式暴露出明确的结构性缺陷：

1. **桌面单机组件与多设备 NAS 控制台的交互模型冲突**：上游 `SdkMemoryLibrary`、`SdkRawHistory`、`R6Settings` 面向单机本地工作台设计。在 Hub 控制台中，点击引用记忆、打开来源原文、执行「忘记／彻底删除」均只能以追加 `<section>` 的方式堆叠在页面底部，造成严重的视口跳转与套娃滚动。
2. **信息架构与导航失衡**：总览页沿用早期大屏装饰布局，服务连接状态在顶栏、侧栏底卡与右下状态卡重复出现三次；核心工作空间入口（项目切换与新建项目）弱化在右上角角落；日常高频的「已连接设备与来源授权管理」被折叠进 `<details>高级：JSON 配对与设备授权管理</details>`，与低频 8 阶段手动 JSON 配对混排；各子页面常驻大段教学与免责声明文字墙，掩盖业务主操作。
3. **缺少与安全边界兼容的客户端路由状态**：原 `HubConsole` 仅用组件内存状态保存当前视图，刷新浏览器即退回首页且不支持前进／后退。
4. **多层补丁维护脆弱**：每次调整控制台界面均需针对上游桌面源码生成跨文件补丁并维护 `manifest.json` 校验和，同时被迫携带上游桌面端专用的 Three.js、Electron IPC 桥接与本地导入轮询代码。

## 决定

1. **在自有仓库建立独立管理前端包 `apps/console`**：
   - 构建产物输出为标准静态资源（`dist/index.html` 与 `dist/assets/*`），在 `Dockerfile` 中直接复制到 `ONE_MEMORY_WEB_DIR=/opt/cmh/web`，由现有 Core 静态文件服务与 `CloudBoundary` 鉴权边界原样托管。
   - 不修改固定上游源码树与已有补丁集合，不影响 `scripts/prepare_upstream.py` 与上游 Core／SDK 回归。
   - 仅保留实际使用的运行时依赖（`react`、`react-dom`、`lucide-react`），移除上游桌面端冗余的 `three`、`axios`、`@tanstack/react-query`，使用精确锁定的 `pnpm-lock.yaml` 保证可复现构建。

2. **100% 复用现有后端协议、安全边界与客户端状态机合同**：
   - 后端零协议破坏：继续使用 `/hub/v1/console/*`（`bootstrap`、`summary`、`dashboard`、`read/*`、`correct`、`ask-stream`、`usage`）、`/hub/v1/projects`、`/hub/v1/devices`、`/hub/v1/sources`、`/hub/v1/pairings`、`/hub/v1/governance/*`、`/hub/v1/backups/*`、`/hub/v1/admin/credential`、`/api/v1/ui/session/logout`，以及 Core 模型管理接口 `/api/v1/model-profiles`、`/api/v1/model-catalog`、`/api/v1/model-probe-operations/*`、`/api/v1/sedimentation`。
   - 客户端存储键与幂等恢复完全兼容：保留 `cmh_hub_scope:<instance>`（项目选择记忆）、`cmh-project-create:<instance>`（项目创建未知结果核对）、`cmh-invitation:<instance>:<scope>`（一次性邀请轮询与恢复）、`cmh-pairing:<instance>:<scope>`（高级 JSON 配对阶段恢复）、`cmh-governance:<action>:<instance>:<scope>` 与旧 `cmh-forget:*`（治理操作回执恢复）、`one-memory.pending-model-probe.v1`（模型探测未决恢复）。
   - 诚实状态与隐私不变量不变：不把网络失败伪装为空数据，不把「已配对／已接纳」混同为「在线／已提炼」，权限与删除类错误不被掩盖，服务端错误响应正文永不直接回显。

3. **采用查询参数路由（`?view=...&tab=...`）适配 `CloudBoundary`**：
   - `CloudBoundary` 对未认证的 `/hub/*` 子路径返回 JSON `401`，而对 `/` 与 `/hub` 返回 `303 RedirectResponse("/hub/login")` 且 `/` 属于免受治理运行时围栏阻塞的 `STATIC_PATHS`。
   - 因此客户端视图状态统一通过查询参数（`?view=overview|memories|history|ask|devices|settings` 及 `?tab=model|usage|backups|security`）与 `history.pushState` / `popstate` 同步，既支持刷新保持当前模块与前进／后退，又保证会话过期刷新时平滑重定向至登录页。

4. **重构工作台交互与登录／初始化页体验**：
   - **全局导航壳层**：侧栏顶部内置项目空间切换器与「新建项目」模态框入口；合并冗余状态卡片；实验版本与数据隐私说明收敛为顶栏常驻徽章与可展开详情浮层。
   - **主从分栏与抽屉／模态框体系**：记忆库采用左列表右详情（窄屏自动切换为右侧抽屉）支持内联纠错；来源原文追溯、问答引用详情、近期动态详情统一通过右侧滑出抽屉（Drawer）展示；「忘记／彻底删除」治理、新建项目、连接新 Agent 向导、导入本机历史指引统一采用模态对话框（Modal），彻底消除页面底部套娃堆叠。
   - **问记忆实时流式呈现**：在保留 `schema_version=2` 与 `final` 帧严格校验的前提下，实时展示 `recall` 召回进度与 `delta` 增量生成文本。
   - **设备与设置降维提效**：设备页主视图直接展示已授权设备卡片、最近接纳时间三态与来源／设备撤销操作，将一次性链接接入做成三平台选项卡向导，手动 JSON 配对折叠至底部高级区；备份页支持点击历史版本自动将 `BACKUP_ID` 填入停服导出／恢复命令。
   - **认证页统一**：同步更新 `boundary.py` 与 `admin_setup_page.py` 中 `/hub/login` 与 `/hub/setup` 的深蓝紫暗色样式与表单排版，严格保持 `default-src 'none'` CSP、`Referrer-Policy: same-origin`、无长度属性限制及初始化脚本 SHA-256 哈希绑定。

## 未采纳方案

- **继续在上游 `memhub/apps/web` 追加大体积补丁**：无法消除桌面组件结构性耦合，且补丁栈会进一步膨胀，阻碍后续迭代与审查。
- **使用 `/hub/<subpath>` History 子路径路由**：与 `CloudBoundary` 对 `/hub/` 前缀返回 JSON `401` 的既有安全边界冲突，未登录刷新页面会直接暴露原始 JSON 错误而非跳转登录页。

## 验证

- `apps/console` 类型检查（`tsc`）、生产构建（`vite build`）与 Vitest 交互及契约测试集（覆盖路由同步、项目切换与创建核对、总览状态与活动降噪、记忆搜索／内联纠错／来源抽屉、治理忘记与彻底删除恢复、原文解析与分页、SSE 流式问答与引用抽屉、一次性链接配对与设备撤销、模型配置／探测／用量／备份命令绑定／管理员改密）。
- `apps/hub/tests/test_boundary.py` 与 `test_initial_admin_web.py` 验证登录页、初始化页 CSP 哈希与安全响应头。
- `Dockerfile` 的 `web` 与 `web-tests` 阶段及仓库门禁检查。
