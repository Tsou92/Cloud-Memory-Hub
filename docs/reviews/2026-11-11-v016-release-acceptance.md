# v0.1.6 发行、NAS 升级与官网发布

关联 FR-01、FR-02、FR-11、FR-12、FR-13、ENG-01；对应发布计划 R3-04 至 R3-07 与 R4-05、R4-06 的本次实测部分。本文只记录本轮实际发生的检查，未执行项单列，不记为通过。

## 当前公开版本与制品

[v0.1.6 Release](https://github.com/Tsou92/Cloud-Memory-Hub/releases/tag/v0.1.6) 已于 `2026-11-11T12:18:37Z` 公开，Release ID `441563414`，并已设为 latest。标签 `v0.1.6`（annotated）解析到源码 `f9441f2b4d7681f1ba8fe6196ac07b7f3ffb9b61`，与 `main` 当前提交一致。

[发行 CI 43002946244](https://github.com/Tsou92/Cloud-Memory-Hub/actions/runs/43002946244) 成功：preflight、两个 quality、三系统 Agent setup、runtime、Linux／Mac 构建及 assemble 共十个任务通过。该运行使用 `publish=false`，publish 任务按设计跳过；随后用同一批 `verified-release` 附件创建草稿，完成 NAS 与本机验收后再公开，没有用另一份本机构建替代。

公开前 `publish` 步骤把七个附件从 GitHub 草稿完整回下载并 `verify_bundle` 校验，通过后才解除草稿。公开 Release API 的七个附件大小与 SHA-256 与本地已验证制品逐项一致。

| 附件 | SHA-256 |
| --- | --- |
| `cloud-memory-hub-v0.1.6-linux-amd64.tar.gz` | `4b26781309aafa05209a4ecddf34f6debf18657f371c1492309219019145c402` |
| `cloud-memory-hub-client-v0.1.6-macos-arm64.tar.gz` | `542412cba056b52aca71656043d73a4f294bdeebb8844de418548d7af314e932` |
| `cloud-memory-hub-v0.1.6-source.tar.gz` | `e0d046f9c5d6ef8b08bd075546f320f5515ff4d395824c745378521d6a5c1c39` |
| `cloud-memory-hub-v0.1.6-provenance.tar.gz` | `15768d26b0e87ae63f0a16b849c5d3b38a6f9e9463b666c4c9c10d9f82db7656` |
| `release.json` | `099b9c0e95de4d1e945af9779601724aab5042a7237d85001282d5f9dfd15920` |
| `RELEASE_NOTES.md` | `2c3858ea819fe79ac6ada5968e487a078b523dae84f0a8580adff250e573512c` |
| `SHA256SUMS` | `7cf851a4e6912ca357e05ac4488ded2a7e3ce7d1f71b7c5eddbf0ed14c149df0` |

v0.1.5 与其之前的公开 Release 保留原样，未撤回。

## 实际完成的检查

| 范围 | 结果与边界 |
| --- | --- |
| 公开入口匿名验证 | 未携带身份访问 Release 页、`releases/latest` 重定向与 `v0.1.6` 的 NAS 安装指南，均 GET 200；七个附件分片请求均 206 且远端 digest 与 CI 产物一致。`bootstrap-install.sh`／`.ps1` 下载后与标签源码逐字节相同，隔离 PowerShell 的 `-VerifyOnly` 与 `sh bootstrap-install.sh --nas-only --help` 均通过。 |
| 公开发行二进制 | Linux amd64 与 Mac arm64 归档匿名分片下载返回 206 且首字节为 gzip 魔数，未做完整解包（CI 已在 assemble 前完成，见下）。 |
| 发行源码回归 | 对标签源码执行 `python -m unittest discover -s tests -v`：205 通过、23 跳过、0 失败。跳过项未计入通过数。 |
| 本机 Docker 首装 | 用 v0.1.6 Linux 镜像在隔离 Compose 项目与回环地址完成一次 `--local` 首装，再校验短密码初始设密、重启后用同一密码登录、拒绝去空格变体，并重复执行同一命令（不重复导入镜像）。首装 16.49 秒、重复执行 1.48 秒；随后容器、镜像、卷与网络按项目标签清理并反向核验为空。 |
| 日常 NAS 升级 | 先停服对四个卷做冷备并逐个用 SQLite `integrity_check` 校验（全部 ok），再以 v0.1.6 镜像在停服状态生成并完整检查升级前版本（12 个文件、868,804 字节），随后切换 `CMH_IMAGE` 启动。健康检查 200、产品版本 0.1.6、`org.opencontainers.image.revision` 为 `f9441f2`，`install-id` 与四个持久卷未变，容器 0 重启，日志无错误。 |
| 升级后业务数据 | 逐表比对升级前冷备与运行实例：原有记录全部保留（1 个设备、2 个项目、1 条配对、1 个 instance、各 schema 标记），升级后仅新增本轮验收写入的 1 条合成记忆与其回执。旧镜像 `cloud-memory-hub:v0.1.5` 保留，具备回滚条件。 |
| 原有 MCP 连接 | 沿用升级前已签发的设备凭证与 MCP 地址，完成 `initialize`、`tools/list`（9 个工具）、`memory_status`、`memory_remember` 与 `memory_recent` 读回。写入返回 `accepted` 与 `receipt_id`，随后在 `memory_recent` 中按 `record_id` 读回同一内容。不把容器健康当作读写成功。 |
| 产品主页 | 官网与指南已发布 v0.1.6。生产域名十二个公开文件 HTTP 200，字节数与 SHA-256 与发布源码逐项一致；六个私有路径返回 404；两页各 16 处版本引用均为 v0.1.6，两页出现的 6 个发行／引导链接实测均 200。 |

## 浏览器验收

本轮用本机 Chrome 无头实例经 DevTools 协议在真实渲染环境下检查，未使用 BrowserSkill（该 CLI 未安装）。

| 项目 | 结果 |
| --- | --- |
| 桌面首页／指南 | 1440×900 与 1440×1200 渲染正常，`v0.1.6 已发布` 徽章与指南版本行可见 |
| 窄屏 | 390×844 下 `scrollWidth == innerWidth == 390`，首页与指南均无横向溢出 |
| 手机菜单 | `#menuToggle` 点击后 `aria-expanded` 由 false 变 true 菜单展开；Escape 后回到 false，且焦点回到该按钮 |
| 减少动态效果 | 模拟 `prefers-reduced-motion: reduce` 后媒体查询命中 |
| Swarm 演示 | 跳过按钮可用，跳过后场景节点数为 0、页面无残留隐藏正文（3143 字符正文可见）；重播按钮可用 |
| 无 JavaScript | 禁用脚本执行后正文 3098 字符、17 个导航链接、`body` 可见、无横向溢出 |
| 结构与锚点 | 两页各 1 个 `h1`，无重复 `id`，无悬空页内锚点 |
| 链接目标 | 首屏主按钮指向 `guide.html#server`，发行按钮指向 `releases/tag/v0.1.6`，导航 GitHub 指向公开仓库 |

## 未执行项与收尾边界

1. **NAS 管理员后台的浏览器核对未执行。** 该实例的 `admin-token` 仍为 `cmh_admin_setup_v1_` 初始化令牌，升级前后哈希一致，说明待初始化状态早于本次升级；`/hub` 在完成首次设密前显示“需要先设置管理员密码”。管理员设密、登录后的项目／模型／记忆页面核对必须由用户本人完成，本轮不以 curl 结果替代。首次设密、登录、后台改密的功能本身在隔离 Docker 首装与既有自动化测试中覆盖。
2. **`tools/call` 的 `request_id`／`session_id` 必须为 UUID。** 首次用非 UUID 字符串调用返回 `request_identity_invalid`，属预期校验而非缺陷；改用 UUID 后写入成功。文档与复制话术未受影响，本轮未改动该接口。
3. **Mac arm64 客户端本机安装未重做。** 本版依赖 CI 在新私有目录对最终包执行的验证与安装，以及 v0.1.4 已记录的本机安装证据；不据此宣称本机已重新验收。
4. **未重做升级失败回退。** 本轮只做成功路径升级，未重新注入失败或执行回滚；v0.1.5 镜像与四个冷备归档保留在现场。备份恢复演练的原有边界（单次维护子进程 `SIGKILL`，不代表断电或硬盘故障）继续有效。
5. **未测试其他平台宿主。** 六入口完整自动采集、离线读取缓存与全域恢复仍按[发布计划](../release-plan.md)分别验收。
6. **`docs/release-plan.md` 的阶段表格未随本轮逐条改写。** 本文作为 v0.1.4 之后各版的发行与验收记录，表格中仍标为未执行的双物理机等项保持原结论。

## 清理与留存

本机保留 `verified-release` 七附件、`release-id`、CI 产物回执与 NAS 冷备；NAS 保留 v0.1.5 镜像、v0.1.6 升级前版本（`cmh-backup-20261111T122150958216Z-bf987660a1c24faabb55ea82f95656b2`）与四卷冷备归档。本轮生成的合成验收记忆留在日常实例中，内容仅为“NAS 已从 v0.1.5 升级到 v0.1.6”的标记，不含用户数据。本记录不公开机器地址、私有目录、运行凭证、用户记忆或会话正文。
