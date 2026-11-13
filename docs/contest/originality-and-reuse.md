# 原创及上游复用声明草稿

适用作品：Cloud Memory Hub。产品版本：**v0.1.3，正式七件附件已核验并公开发布**；来源提交为 `25b9f7526bd5509d031dd9511845f6521f3acf92`，见[正式发行](https://github.com/Tsou92/Cloud-Memory-Hub/releases/tag/v0.1.3)及[附件摘要](verification-checklist.md)。本页仍未签署、未提交，不代表绿联认证或已获参赛平台认可。

## 原创贡献与复用边界

Cloud Memory Hub 围绕 NAS Docker 部署、多设备身份与项目授权、远程 MCP／Skill 接入、共享记录及任务交接组织产品流程，并对复用组件进行必要适配。原创贡献以最终发行源码、差异补丁和对应测试为准，不把上游已有记忆引擎、队列或管理界面整体声明为原创。

| 来源 | 复用内容 | 交付时保留 |
| --- | --- | --- |
| [Tencent-Hunyuan/Hy-Memory](https://github.com/Tencent-Hunyuan/Hy-Memory) 及其中 MemHub | 固定提交 `b7d9f0e0f417eae30b192882f848b432484c20a6`；SDK `2.0.5.dev3`、MemHub `1.0.4`。复用记忆数据层、Core、既有处理队列和管理网页基础 | 上游 Apache-2.0 许可证及 NOTICE、固定 manifest、40 项本地补丁及其摘要 |
| Cloud Memory Hub 自有源码与适配 | 设备授权与项目范围、远程 MCP 接入、共享记录／交接及部署发行流程；管理网页的本地主题和仪表盘改动仍作为上游补丁声明 | 自有 Apache-2.0 LICENSE／NOTICE、来源提交、对应源码与测试；不把上游引擎或完整网页声明为自研 |
| 第三方运行依赖与模型资源 | 产品使用的 Python／Web 依赖及模型资源，分别适用自身许可 | 锁文件、上游 `THIRD_PARTY_NOTICES`／SBOM 和模型来源；上游清单中的未确认许可证不改写为已审核 |

核对对象为 v0.1.3 正式 source 与 provenance 附件，不是本地旧参赛工作树。源码 `upstream/manifest.json`、provenance 的 `client/upstream-manifest.json` 与来源提交中的文件逐字一致；40 项补丁在两个附件中逐项字节一致，SHA-256 均符合 manifest。manifest 自身 SHA-256 为 `ac219cf63ac64ad3f6e5306f574823a820ad18806dad43d001ecd5f71350e28d`。

| 已核对内容 | 正式附件中的证据 |
| --- | --- |
| 自有源码 Apache-2.0 许可及上游归属声明 | source 的根级和 `apps/hub/` 下 LICENSE／NOTICE，与 provenance 根级及 `client/LICENSE.cloud-memory-hub`／`client/NOTICE.cloud-memory-hub` 对应副本逐字一致 |
| Hy-Memory／MemHub 许可 | provenance 的 `client/LICENSE.hy-memory` 第 5 行明确 Apache-2.0；`client/LICENSE.one-memory` 为 Apache-2.0，`client/NOTICE.one-memory` 保留 Tencent 归属及第三方声明指引 |
| 固定版本与本地修改 | source 的 `upstream/manifest.json`／`upstream/patches/`，对应 provenance 的 `client/upstream-manifest.json`／`client/patches/`；补丁数量以 manifest 的 40 项为准 |
| 本项目接入与集成实现 | source 的 `apps/hub/src/cloud_memory_hub/remote_mcp.py`、`remote_read.py`、`console_dashboard.py`；分别承接既有授权操作、上游 SDK 读取和按项目范围统计，并非另造记忆引擎 |
| 本地网页改动与发行流程 | `upstream/patches/0040-reference-dashboard.patch`、`Dockerfile`、`compose.yaml`、`scripts/build_client_distribution.py`、`scripts/release_artifacts.py`；具体改动及验证以同版源码、补丁和测试为准 |

provenance 中的 `client/THIRD_PARTY_NOTICES.upstream.md` 第 8–10 行说明逐包许可证尚未全部补齐；`client/SBOM.upstream.spdx.json` 是 2026-10-17 生成的上游快照，139 个包中 136 个标为 `NOASSERTION`，另有 2 个 Apache-2.0 和 1 个 MIT。它证明相应来源资料已保留，不是本次最终镜像全部依赖的完整许可证审核结论。以下登记实际核对的素材来源，不替代许可证文本或提交者确认。

## 视觉素材与内置模型来源

| 对象 | 已核对事实 | 证据边界 |
| --- | --- | --- |
| `apps/hub/src/cloud_memory_hub/web_assets/hub-orb.png` | 与官网采用的光球文件逐字一致；SHA-256 `43beea4405abf9c3421b07b7ec7cad6831e4378cada4151c6b702892a3d98f6e`。PNG 内嵌 C2PA 声明含 OpenAI／ChatGPT、`c2pa.created` 和 `trainedAlgorithmicMedia` | 可登记为含生成式媒体来源声明的视觉素材；本次未验签，未找到独立生成回执或提示词记录，不据嵌入声明推定完整授权 |
| `docs/assets/readme-overview.svg` | 项目内随源码维护的 1200×520 可编辑连接示意图；SHA-256 `eb2af28297b203301957b2b248e30b4e4b24471366d4e4daec676f929549bf83`。图形、文字、图标均在文件内，无外部资源、脚本或嵌入截图 | 登记为产品说明图；不把文件结构检查替代为提交者的原创确认 |
| 内置中文检索模型 | 固定上游的 `memhub/apps/core/src/one_memory/embedding_assets.py` 声明采用 `BAAI/bge-small-zh-v1.5` 的 Xenova ONNX 导出，列出五个文件及摘要；该源码与正式 provenance 的 `client/source-files.json` 记录一致，Docker 构建复用其校验清单 | 属于第三方模型复用。源码声明基础模型为 MIT，同时保留导出仓库未声明独立许可的 `OPEN-ITEMS A-32`；本次未找到单独模型许可文本，不声明原创权重或该项已解决 |

未来参赛视频尚未录制，没有把现有截图、脚本或静态示意图当作真实操作视频。

官方 UGOS Skill 创建工具仅用于了解编排和检查合同；本轮没有复制其引擎、安装工具或生成可安装 Skill。未来如使用其中模板或其他素材，应按实际来源补充声明。不得把第三方商标或兼容性陈述写成官方背书。

## 数据、外部服务与权限说明

- 共享内容发送到用户明确配置的 Hub；项目、设备和来源权限限制读取范围。
- 配置外部模型后，相关处理可能将所选内容发送给该服务并产生费用；参赛说明须列出实际使用的服务、数据用途及账号要求，不能只写“全部本地”。
- 部署与更新由用户授权的安装流程完成。未来 Uliya Skill 不内嵌动态下载执行、提权或常驻服务安装步骤。
- 交付材料不包含真实记忆、对话、数据库、备份、配对文件、密钥、个人设备地址或私人联系信息。演示内容和公开证据分别审阅。

## 最终确认项

以下区分已核对事实与仍待确认事项，不是已签署声明；未完成前不得复制为“原创确认已通过”。

| 项目 | 状态 |
| --- | --- |
| 正式源码提交与发行附件摘要 | 已登记：v0.1.3、`25b9f7526bd5509d031dd9511845f6521f3acf92`；七件正式附件见[核验清单](verification-checklist.md) |
| 上游固定提交、补丁清单与自有／上游许可文本 | 已核对：上述固定版本、40 项补丁摘要及 Apache-2.0／NOTICE 副本；不包含全部第三方依赖许可结论 |
| 上游第三方许可资料及未知部分 | 已登记：保留上游 notices／SBOM，`NOASSERTION` 条目不作推定；后续仅按最终参赛实际纳入的文件核对适用许可义务 |
| 实际纳入的图片、视频、模型及其他素材授权 | 光球、SVG 和内置模型来源已按上表登记；未核对的授权边界保留，未来新增素材另核对。参赛视频尚未录制 |
| 提交者对原创贡献和复用声明的确认 | 待确认；报名身份仅在官方受控表单填写 |

返回[材料说明](README.md)；最终检查见[核验清单](verification-checklist.md)。
