**这是可选参赛分支 `codex/ugreen-contest` 的准备记录，不是主体产品的发布门槛。**

# 绿联参赛准备

核查日期：2026-11-10。主体继续按[首版发布计划](release-plan.md)交付共享记忆、通用 MCP／Skill 和任务交接；比赛规范不改变产品功能、接口或架构。产品成熟后才整理参赛材料，不因平台限制改造成文件摘要工具。本记录不代表报名、条款接受、上传或 Uliya 安装验证。

## 官方规范与核查来源

依据[官方 Skill 指南](https://hackathon.ugnas.com/skill-docs.php)、[赛事交付要求](https://hackathon.ugnas.com/index.php)及[内容上架规范](https://api.ugnas.com/api/system/v1/ua/agreement/page?agreementCode=skillhub-publish-audit-spec)。已下载并只读检查[官方工具包](https://hackathon.ugnas.com/skill-kit-download.php)，未安装或执行其中脚本。

- ZIP：37,575 字节；SHA256：`cf557e5a022e4297892f39768132855154232efdedeb726b2845450ccd70e1b3`。
- 包内 `data/capability-catalog.json`：版本 `2.0.0`，生成日期 `2026-09-01`；`scripts/check-lib.mjs` 为以下结构和大小限制的依据。重新打包前应复核官方版本。

| 项目 | 当前查明的要求 |
| --- | --- |
| 可安装 Skill | 检查器只接受根目录 `SKILL.md` 和 `references/` 内普通 Markdown；不接受脚本、二进制、manifest、sidecar 或符号链接。网页建议 README，但检查器拒绝根级 README，因此使用说明放项目交付包外层。 |
| 大小与名称 | 单文件 ≤1 MiB，总文件大小 ≤10 MiB，文件数 ≤100；名称 2–64 字符，仅小写字母、数字及连字符；description ≤1024 字符；正文 500 行是建议。 |
| Uliya 编排 | 父 Skill 只通过 `delegate` 调用目录指定的注册专家，并处理 `completed`、`failed`、`cancelled`、`waiting_user`、`waiting_approval`；真实结果才能传递给后续步骤。 |
| 数据与依赖 | 披露外部服务、数据目的地、费用／账号及系统修改；保留第三方来源与许可。Skill 不得擅自安装常驻服务或动态下载执行代码。 |
| 交付材料 | 初赛准备规划填报、可运行演示视频、预测试安装包、README 和原创声明；决赛材料另需真实 NAS 调试与操作证据，不能以模拟演示代替。 |

## 现有适配缺口

官方目录有 9 个 CLI、78 个动作、6 个专家，涵盖文件、文档、知识、相册、影音和监控；没有公开的通用 HTTP、MCP、shell、Docker、记忆或 handoff 动作。[现有通用 Skill](../packages/agent-skill/cloud-memory-hub/SKILL.md)依赖 Hub MCP／本机连接组件，不能直接认定可在 Uliya 运行。

内容上架规范并非全面禁止外部 API，但现行工具包没有证明如何接入自建 Hub。需在可选分支中核实官方支持的连接方式并实测；不得虚构专家、生成伪可安装包或把现有 Mac／MCP 测试称作 Uliya 验收。此缺口不阻塞主体产品发布。

## 产品成熟后的分层打包清单

1. **产品交付包**：对应同一发行版本的源码、Docker 部署文件、依赖／第三方许可、安装运维说明、功能测试材料与校验清单；不夹带真实凭证、个人配置或用户数据。
2. **可安装 Skill 包**：只有实际接入路径和官方安装验证通过后，才生成符合上述结构的独立包；独立 NAS 服务作为明确说明的配套依赖安装，不由 Skill 静默部署。
3. **参赛材料**：README、原创及复用声明、规划填报内容、真实 NAS 演示视频，清楚区分主体产品能力与已验证的 Uliya 能力。
4. **最终核对**：复核当时官方目录和提交要求，验证真实写入、跨设备读取、任务交接及失败处理；静态检查、打包成功和已有合成测试均不替代安装与实机证据。

本分支目前仅保存调研与准备范围，没有生成、上传或提交参赛安装包。
