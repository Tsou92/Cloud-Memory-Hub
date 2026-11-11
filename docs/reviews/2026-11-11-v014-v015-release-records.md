# v0.1.4 与 v0.1.5 发行记录（补记）

2026-11-11 补记。这两版在当时已完成发行与部分验收，但未留下独立验收记录；v0.1.6 发布时发现文档缺口，故按可复核的仓库与 Release 事实补记。**补记不是重新验收**：文中区分「现已可核验的事实」与「当时记录、本轮未复跑」两类，未执行项不写成通过。

## v0.1.4

[v0.1.4 Release](https://github.com/Tsou92/Cloud-Memory-Hub/releases/tag/v0.1.4) 于 `2026-11-11T04:30:45Z` 公开，Release ID `441233843`，标签指向 `79bbe924c40add933da81496e989c9474430fbc7`。

**发行 CI 的实际情况需要看清：[运行 42955474834](https://github.com/Tsou92/Cloud-Memory-Hub/actions/runs/42955474834) 整体结论是 failure，但失败的只有最后的 `publish` 任务。** preflight、两个 quality、三系统 Agent setup、runtime、linux、mac、assemble 共十个任务均成功。`publish` 失败原因是 `release_draft_not_found`（exit 2）——该手动工作流假定草稿已存在，而当时并未创建。

因此 v0.1.4 的制品的可信依据是：发行工作流前十个任务成功产出的 `verified-release`，加上本地发布脚本创建草稿、上传、**完整回下载七个附件并校验**后再解除草稿。不能因为 publish 任务失败就否定制品，也不能把它写成「发行 CI 全绿」。

现已复核：本地 `.local/release-v014-publish/verified-release` 的七个附件经 `SHA256SUMS` 全量校验通过，且大小与 SHA-256 与公开 Release API 逐项一致；Release 已 `draft=false`，两个匿名下载入口实测 200。当时是否做过真实 NAS 安装与 MCP 回归，本轮未找到记录，故不声称。

## v0.1.5

[v0.1.5 Release](https://github.com/Tsou92/Cloud-Memory-Hub/releases/tag/v0.1.5) 于 `2026-11-11T08:45:51Z` 公开，Release ID `441414251`，标签指向 `25f8d636dbf9225522c5321a9a8c788a7eba9b39`。

[发行 CI 42984320773](https://github.com/Tsou92/Cloud-Memory-Hub/actions/runs/42984320773) 成功，publish 按设置跳过。该版新增 NAS 一行安装、跨平台引导脚本与首次设密流程。

当时已有验收记录：[一次首装验收](2026-11-11-first-install.md) 使用正式 v0.1.4 制品在实际 Linux amd64 NAS 上空卷启动，NAS 直连下载遇 TLS 中断后改用本机已校验制品经 SSH 传输，完成真实 Docker、回环健康、私网 HTTPS、项目创建、邀请签发与领取、Mac arm64 客户端与 Codex 钩子安装，并在隔离的 Codex app-server 审阅信任后跑通两轮接续。该记录明确当时的模型是本地测试替身，NAS 未配置长期记忆模型，也未重复操作桌面 UI。

现已复核：本地 `.local/release-v015-publish/verified-release` 的七个附件经 `SHA256SUMS` 全量校验通过，大小与 SHA-256 与公开 Release API 逐项一致。日常 NAS 在 v0.1.6 升级前正运行 v0.1.5（`org.opencontainers.image.revision` 为 `25f8d63`），说明该镜像确实被正常部署运行过；其业务与状态在升级后核对中保留完好。

## 与 v0.1.6 的关系

v0.1.6 在已运行的 v0.1.5 实例上完成冷备升级，详见 [v0.1.6 验收记录](2026-11-11-v016-release-acceptance.md)。三版的发行路径共同说明：本项目的制品可信链是「CI assemble 产物 → 本地完整回下载校验 → 草稿上传后再回下载校验 → 公开」，而不是「工作流整体显示成功」。

## 未执行项

- v0.1.4 的真实 NAS 安装与 MCP 回归当时是否有记录未确认，本轮不补做、不声称。
- v0.1.4 与 v0.1.5 未做独立的浏览器后台验收记录。
- 两版均未重新执行完整 CI；上文的 CI 结论来自既有运行记录。
