# 可追溯的发行制品

关联 [发布计划](release-plan.md) R3-04、R3-05、R4-05。发行流程复用现有
`quality.yml`、Dockerfile、`scripts/smoke_container.py`、
`scripts/build_client_distribution.py` 和客户端安装器；新增脚本仅负责版本绑定、
制品汇总与草稿校验，不另做镜像或客户端构建实现。

## 触发条件

`.github/workflows/release.yml` 只允许手动 `workflow_dispatch`，不会因推送代码或标签自动发布。
`publish` 默认关闭：流程执行完整检查和构建，保留 `verified-release` 七附件，暂不创建
GitHub Release。首版 NAS 验收使用此模式，先验证最终将公开的同一批制品。
先完成候选版本真实验收、更新产品版本、提交并准备已有标签；本流程不会创建、移动标签。
运行时选择该标签作为 workflow ref，并在 `tag` 输入同一标签。
GitHub 要求手动工作流已存在于默认分支，标签指向的提交也必须包含本工作流。
先将本次工作流变更合入默认分支，再从已验收提交准备标签；当目标相对默认分支
包含新的 workflow 变更时，GitHub 的额外工作流写权限限制可能使创建 release 失败。
本流程不增加 PAT 或额外权限绕过该限制。

仅接受 `vX.Y.Z` 或 `vX.Y.ZrcN`，各数字不能有多余前导零，N 从 1 开始。
去掉一个前导 `v` 后，必须与 `apps/hub/pyproject.toml` 的 `project.version` 逐字相等。
例如 `v1.2.3rc1` 对应 `1.2.3rc1`，不能用别名或开发版绕过。
产品仍为开发版时会拒绝发行。标签必须解析到当前干净 checkout 的同一个提交；
各平台任务固定 checkout 此提交，发布前再次核验远端标签，包括 annotated tag。
已有标签的真实提交以远端标签解析结果为准，不用 release 的 `target_commitish` 展示字段代替。

`rcN` 始终发布为 prerelease，且不更新正式 latest。稳定标签发布为普通 release，
并设置为 latest，供官方主页和用户进入最新稳定版本。再次运行遇到已有同名 release（包括草稿）会停止，
不会覆盖已有附件或复用无法证明来源的草稿。

## 构建与附件

首先对该标签提交调用完整仓库 quality 工作流。随后：

- Ubuntu 24.04 构建 Linux amd64 镜像，显式设置 `CMH_REVISION` 为源码提交；
  调用既有已安装镜像 smoke 和重启检查。只有成功后才 `docker image save | gzip -n`，
  并核验导出镜像的配置摘要、平台、镜像标签及 revision 与实测镜像一致。
- macOS 15 arm64 使用原生 Python 3.12 和现有锁定构建器生成客户端包；
  对最终 tar 安全解压、核对清单、commit、平台和版本，再从最终包安装到全新私有目录。
  安装器执行依赖校验及 CLI 启动检查，不安装 launchd，也不修改日常 Agent 配置。
- 汇总任务从固定提交执行 `git archive`，只收集上述两个已验证二进制包和公开许可证／来源信息。

每次 release 恰好包含以下七个附件，`<tag>` 包含前导 `v`：

| 附件 | 内容 |
| --- | --- |
| `cloud-memory-hub-<tag>-source.tar.gz` | 固定提交的源码快照 |
| `cloud-memory-hub-<tag>-linux-amd64.tar.gz` | 已通过 smoke 的 docker-save 镜像 |
| `cloud-memory-hub-client-<tag>-macos-arm64.tar.gz` | 已对最终包执行全新安装的客户端 |
| `cloud-memory-hub-<tag>-provenance.tar.gz` | 产品／上游许可证、固定上游清单、补丁、锁文件及构建来源 |
| `release.json` | tag、产品版本、源码 commit、平台、镜像 ID 与二进制摘要 |
| `RELEASE_NOTES.md` | 安装说明、已声明平台与 CHANGELOG |
| `SHA256SUMS` | 上述六个附件的 SHA-256 |

每个附件必须为普通文件且严格小于 2 GiB。上游 SBOM 仅覆盖复用来源，不能称为完整产品 SBOM。
源码和 provenance 的 gzip 头不包含本机文件名或当前时间；不承诺第三方依赖和整套镜像逐字可重复构建。
不包含用户配置、配对文件、运行数据、模型密钥或开发机私有路径。

镜像回执中的 `image_id` 保留 smoke 实测镜像的 Docker 原生 ID；新增
`config_digest` 记录归档配置原始字节的 SHA-256。经典 image store 的 ID
可以直接等于配置摘要；containerd image store 的 ID 也可能指向 OCI image
index 或 image manifest，因此不同存储后端导入同一归档后的原生 ID 不保证相同。
存在 OCI Descriptor 时，回执另存其 `mediaType`、`digest` 和 `size`。

发行校验要求 Descriptor 的 digest 与实测 ID 一致，并逐层核对归档中相应
blob 的大小和 SHA-256；index 必须唯一选出 `linux/amd64` image manifest，
再证明其配置与 docker-save `manifest.json` 指向同一份配置。直接 OCI image
manifest 使用相同的配置绑定检查。平台、源码 revision 和发行标签检查继续保留；
未知 Descriptor 类型、多个匹配平台、配置绑定链上缺失或损坏的 blob 均拒绝。此兼容仅服务于
现有 Linux amd64 制品，不新增其他架构支持，也不能仅凭相同 revision 标签认定
归档就是已测镜像。跨存储后端验收应核对归档摘要和 `config_digest`，将原生
`image_id` 作为相应 Docker 环境中的观测记录。
存储后端背景见 [Docker containerd image store 文档](https://docs.docker.com/engine/storage/containerd/)；
具体 ID 表示仍以该次实测 inspect 和归档中的内容摘要为准。

## 同一制品验收后发布

按已有标签触发 `release.yml`，保持 `publish=false`。成功后，从该次运行下载
`verified-release` 到新的空目录，核对 `release.json` 的 tag／commit 与本次运行一致，
再校验 `SHA256SUMS`。NAS 导入其中的 Linux 镜像归档，使用同一镜像完成安装／升级验收；
Mac 验收使用其中的最终客户端包。保留运行 ID、附件摘要、镜像配置摘要及实际验收结果。

验收通过后，在该标签对应的干净源码 checkout 中调用既有发布器，`RELEASE_DIRECTORY`
指向刚才下载并验收的原七附件目录：

```sh
python -m scripts.release_artifacts verify --tag v0.1.1 --commit "$(git rev-parse HEAD)" --directory RELEASE_DIRECTORY
python -m scripts.release_artifacts publish --tag v0.1.1 --commit "$(git rev-parse HEAD)" --directory RELEASE_DIRECTORY --repository Tsou92/Cloud-Memory-Hub
```

发布器仍执行下文的远端标签、草稿、上传、重新下载与摘要核对，不跳过任何校验。
此时不要重新触发构建、重新打包或仅改文件名，否则制品已不再是 NAS 验收的原对象。
工作流显式选择 `publish=true` 会在构建后自动公开，因此不用于上述需要先做 NAS 验收的路径。

下载到独立目录后，可运行 `shasum -a 256 -c SHA256SUMS` 校验六个附件。
镜像使用 `gzip -dc cloud-memory-hub-<tag>-linux-amd64.tar.gz | docker load` 导入，
再将 Compose `.env` 的 `CMH_IMAGE` 设置为 `cloud-memory-hub:<tag>`。
部署入口见源码 README；客户端见 [安装文档](client-installation.md)，
升级前保护和恢复边界见 [备份与恢复](backup-and-recovery.md)。

## 草稿、失败与发布边界

默认任务只有 `contents: read`；只有最后 publish 任务拥有 `contents: write`。
工作流与 publish 任务各自按标签串行，不取消正在进行的同标签任务。
publish 使用可见草稿的完整分页列表确认远端没有同名 release；只有成功遍历完的列表
才能判定不存在，404、网络、鉴权和限流错误均中止。不会用只查已发布版本的 by-tag 接口查询草稿。
创建草稿后固定其 release ID，再上传明确的七个附件。完整分页检查远端附件集合、大小和状态后，
下载到新的空目录，重新验证全部摘要与元数据，最后才将草稿改为公开。
额外、缺失、重名、损坏附件和标签移动均拒绝发布。

发布请求前失败不会主动发布或清理草稿，保留已经创建的资源供核对。
发布请求后必须读回 `draft=false` 才报告成功；请求或读回断线返回
`release_publish_result_unknown`，远端可能已经公开。此时必须检查实际 release 状态、
标签和附件，不得自动删除、重建、覆盖或盲目重试。
普通失败重跑也不会绕过“已有同名 release”检查；由维护者核对并明确处理现有草稿。

## 验证范围

`tests/test_release_artifacts.py` 使用合成 Git 仓库、客户端包、docker-save 结构及
模拟 GitHub 响应，覆盖源码绑定、清单、路径／摘要拒绝、分页、草稿顺序与未知结果。
它不替代真实 GitHub Actions、Linux 镜像构建、原生 Mac 全新安装或公开下载验证。
远端工作流尚未运行时，不能把本地测试通过写成候选制品已经验收或 release 已公开。

官方接口依据：
[复用工作流](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)、
[GitHub 托管 Runner](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)、
[创建草稿](https://cli.github.com/manual/gh_release_create)、
[发布草稿](https://cli.github.com/manual/gh_release_edit)、
[包含草稿的 Release 列表](https://docs.github.com/en/rest/releases/releases#list-releases)、
[创建 Release 的权限约束](https://docs.github.com/en/rest/releases/releases#create-a-release)、
[Release 附件限制](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。
