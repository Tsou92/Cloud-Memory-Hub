# 0.1.2 最终制品与运行验收

关联 FR-01、FR-02、FR-03、FR-04、FR-06、FR-07、FR-11、FR-12、FR-13、ENG-01；对应 R1-02、R2-01 至 R2-03、R3-03 至 R3-07、R4-01 至 R4-06 的本次实测部分。各门槛的完整条件仍见[发布计划](../release-plan.md)，本记录不把局部检查合并为全面成熟声明。

## 固定源码与原七附件

- 标签 `v0.1.2` 对应源码 `c59db5b741c0c7694c3540f88299402115026c28`。
- `main` 合并提交 `93561e932cb9da23be756e94de833a14a4a24b38` 与该源码树完全一致，树摘要为 `01e7ad25a31b51781a400c2c06b02d7b5936c603`。该提交的[合并后 CI 42811231931](https://github.com/Tsou92/Cloud-Memory-Hub/actions/runs/42811231931)六个任务全部成功；这与下列独立发行运行分别记录。
- [发行运行 42807799186](https://github.com/Tsou92/Cloud-Memory-Hub/actions/runs/42807799186)成功：preflight、两个 quality、三系统 Agent setup、runtime、Linux／Mac 构建和 assemble 均通过；本次 `publish=false`，publish 任务实际跳过。
- 下载的是该运行生成的 `verified-release`，artifact ID `13761239692`。原七附件经现有 `scripts.release_artifacts verify` 校验通过，未以本机重建包替代。

七附件为源码、Linux amd64 镜像、Mac arm64 客户端、来源归档，以及 `release.json`、`RELEASE_NOTES.md`、`SHA256SUMS`。具体命名与公开校验步骤见[发行制品说明](../release-artifacts.md)。本次关键绑定如下：

| 对象 | SHA-256 |
| --- | --- |
| Linux 镜像归档 | `425fc980677e92f8ff6a95be4bf93581e3f1903a49b975e135f927c6d72874b7` |
| NAS 已载入镜像配置／镜像 ID | `15d91c655d496a23ce85f28df8cc602031950ddd75b0b5541789ab1ecfa74423` |
| Mac arm64 客户端归档 | `01983423fbb32d92318096767de54759d38e79d9fb2952e898ebfb7b2bb8bd6f` |
| `release.json` | `bede26922320ef626dc286ce45fdc2785b4588bb8f6617924e6b170dcf304eab` |
| 客户端包内 manifest | `a3756d95911c7dd6fb86a941327e6f085182cd2742da7461e4cc5393a915840c` |

## 本次实际完成的检查

| 场景 | 观察到的结果 | 证据边界 |
| --- | --- | --- |
| NAS 原镜像空卷安装 | 在真实 Linux x86_64 NAS 导入上述原归档，复用 `scripts.smoke_container`，exit 0。初始化、认证、重启持久性、设备／来源撤销、过滤投递、SDK 读取和控制台检查通过 | 使用合成数据及烟测替身；不是新的外部模型质量评测 |
| 隔离 NAS 9443 同卷升级 | 使用同一镜像保留既有卷及设备／授权元数据，冷备和原现场保留；外部 HTTPS 健康及公开连接脚本均 200，TLS 校验开启 | 升级后的原生 Agent 调用另见下面两行，不把健康检查当作功能验收 |
| 当前 Codex 桌面 | 实际桌面对话通过远程 HTTP MCP 执行七个工具、共八次调用；新记忆和交接均接纳，并逐字读回 | 沿用独立合成身份；搜索空结果只证明 API 行为，未证明语义质量或自动逐轮采集 |
| 同 Mac Claude CLI 读回 | 真实 Claude Code CLI 2.1.321 沿用隔离配置，单次执行 status、recent、handoff 三个读取工具；原文与五个交接字段逐字相同 | Claude 工具选择由本地合成响应驱动器完成；是同 Mac 跨 Agent，不是新双机或 60 秒时效验收 |
| 真实 Chrome | 登录合成实例，选择项目、看到新原文和完整五字段交接，核对无需独立 App 的引导及模型自动发现控件；390 像素视口无横向溢出，浏览器错误日志为空 | 仅登录和导航；本次未提交模型发现请求、修改模型配置或重复完整首次配置流程 |
| MacBook 高级客户端原包安装 | 在新的私有目录，使用本机 arm64 Python 3.12.9 执行包内校验及安装，均 exit 0；四个 CLI `--help` 通过，447 个已安装生产文件与包内来源摘要一致；原七附件未变 | 未配对、未注册 LaunchAgent、未修改日常 Agent；不能代替高级路径的配对和后台运行验收 |
| 日常 NAS 8443 升级 | 停服冷备校验后启动同一最终镜像，保留原业务卷和新增备份卷；容器 healthy、命令为 supervise，外部 HTTPS 两项检查通过 | 备份及升级现场保留，没有自动回滚；不等于本轮重新执行所有故障恢复演练 |
| 日常原 MCP 连通与读写 | 原连接升级后读取既有记录、检查状态、保存一条项目进度记录，再按新接纳回执读回，共四次调用无工具错误 | 未导出管理员凭证；未将 processing_state 未检查表述为提炼完成或所有 Agent 自动同步 |

生产冷备与私有凭证仍保存在 NAS 受控位置，不随此文档入库。升级 helper 的凭证文件属性核对不等于凭证内容或签名证明；原 MCP 实际读写提供独立的升级后功能证据。

## 历史结果与本次未执行项

`346938a` 的真实两台 Mac、独立身份、双向原文／完整交接及单端撤销证据仍有效于其原版本，见[一次性接入记录](2026-11-10-one-link-onboarding.md)。它不能冒充本次 `0.1.2` 原制品的双物理机复验。

本次 Mac mini SSH 再次超时，最终同版双物理机复验未执行。用户随后明确由其在次日自行测试，并授权现有本机验收完成后直接上线；据此本次发布不再等待 Mini，仍不把未执行项记为通过，也不继续轮询设备。

本轮没有重做外部模型提炼／问答质量、所有首次网页配置步骤、完整历史导入、六宿主自动采集、离线读取缓存、后台登录重启或所有备份恢复故障点。已有对应证据保留其版本和环境边界。后台视觉风格继续等待用户后续指定，本轮没有风格重构。

## 发布与收尾状态

[v0.1.2 正式 Release](https://github.com/Tsou92/Cloud-Memory-Hub/releases/tag/v0.1.2) 已于 `2026-11-10T12:26:22Z` 公开，Release ID `440321239`。既有发布脚本 exit 0；七附件上传后重新下载校验，与本次验收的原七附件一致。另以不带身份的 GET 请求下载公开 `SHA256SUMS`，得到 HTTP 200、599 字节，SHA-256 为 `08c3fdd76c12a1e1971c7d64a071e98e030f57dad8c66e5064c75d180c6d10c7`；此匿名检查仅针对校验文件。

[官网](https://memoryhub.tsounas.cn) 已正式提升至 Vercel 部署 `dpl_CHpERiJn46KnhFppCdzfJxqfd6tC`：九个公开文件匿名 GET 均为 200，摘要与发布源码一致；五个私有路径 HEAD 均为 404。真实 Chrome 完成首页至指南、`guide#agents` 定位、390 像素菜单操作及无横向溢出检查，控制台错误为 0；下载按钮实际到达 `v0.1.2`、Latest 和 `c59db5b` 对应页面。GitHub 外部附件列表持续显示 Loading，重载未改善，本次未将浏览器附件列表展开记为通过；前述七附件下载校验及匿名校验文件直链结果独立保留。

测试资源只做了归属和生产依赖盘点，尚未执行最终清理；正式原附件、生产冷备、恢复资料、日常环境和 Git 历史继续保护。

参赛分支仍只保留在本地，不推送 GitHub，不改变主体产品或本次公开能力承诺。

## 私有证据索引

以下仅登记可核验的报告名称及摘要，不提交真实数据、日志正文、运行配置、私人地址或凭证。

| 报告 | SHA-256 |
| --- | --- |
| `anonymous-download-acceptance.json` | `4925e50d9d3c4cec8b99a3f96730207d66d6da903f200695d3e34c215e393692` |
| `ci-42811231931-main-merge.json` | `a61845e222242daa10e2faa6b354857f3afa0ca808343ae7321105f5ff2e6731` |
| `nas-smoke-result.json` | `5bd97d93b852999ec6be135dee0f8b281c339fe78dcfd8ea192b50e0f6d12f90` |
| `fixture-browser-acceptance.json` | `1b9d6f21088b9d085781dd067a83ae24695f73ab188d51442fe3c8c5b9afa54e` |
| Codex `native-desktop-results.json` | `9f6156333c97e7cd39fe7ea6fdf6b0ca026b5ecdfc7f3747cdd35fdbd6adf52b` |
| Claude `strict-readback.json` | `e894c7f131bc017460b442d24d24779433aab53750d3a5a3ef8705f5475b3f64` |
| MacBook 原包 `final-report.json` | `7088bf5eb5a003049587e6d9a9b32ec02f791e59b7b8fbc34f92047a6ff41ae9` |
| `production-upgrade-execution.json` | `66398080e1005ed5fdecf586462e2b1ec4ff370a7dd3f88a04ae95cfc3dc4faa` |
| `production-upgrade-complete.json` | `fe3363982e5b524df6a743863e313a22ddbe94695f10b106a916c2ea7322d64d` |
| `production-external-acceptance.json` | `f165bf2a5d321aa0f13452826e947666e890fe19b0e327546a4a4a82333e222e` |
| `production-mcp-acceptance.json` | `ca25878c4e4292099fa408fceaa59644abaab5a095fa6df75f68a07d4a998ca5` |
