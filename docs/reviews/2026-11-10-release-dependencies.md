# 发行候选依赖安全分诊

关联[需求说明第 13 节](../requirements.md#13-工程与发布要求)及
[工程规范第 7、8 节](../engineering.md)。范围为 GitHub 自部署发行候选的 Python
HIGH／CRITICAL 扫描项；本记录区分包版本命中、实际调用路径与已验证修复。
没有改动运行协议、数据库或治理语义，也不以清零扫描计数为目标扩展未启用的服务。

## 扫描基线与证据

统筹使用经官方校验的 **Trivy 0.76.2** 扫描候选镜像归档，源码提交为
`ae9cb2e84be4a756fc68b7bcd2d92d9a2cb9d5df`。审查者读取了本地证据
`.local/release-validation/ae9cb2e-trivy-report.json`、同名前缀的 `trivy.log`
及 `trivy-summary.json`，未自行重跑镜像扫描。报告时间为
**2026-11-10 13:31:01 +08:00**；数据库在该次运行中实际更新。

| 分类 | CRITICAL | HIGH | MEDIUM | LOW | UNKNOWN | 总条目 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Debian 12.15 | 2 | 44 | 129 | 112 | 18 | 305 |
| Python | 3 | 10 | 23 | 4 | 0 | 40 |
| 合计 | 5 | 54 | 152 | 116 | 18 | 345 |

这些是报告中的漏洞命中条目数，不是唯一 CVE 数，也不是已确认可利用的数量。
本轮逐项分诊 Python 的 **13 条 HIGH／CRITICAL**；其余级别未作完整审查。
日志明确提示部分严重性来自其他供应商，不能把扫描等级等同于上游自己的评级。

## 需要修复的实际调用路径

### certifi：必须更新 TLS 信任库

- 基线：`certifi 2026.7.22`，CRITICAL，
  [GHSA-6q48-6c7w-9gw2](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2)。
- 上游已确认 emSign 根证书关联的中间 CA 私钥泄露，具备相应密钥及网络中间人位置的
  攻击者可伪造可信证书。GitHub 审核公告建议 **2026.10.19 或更新版本**：
  2026.10.18 移除全部 emSign 根，2026.10.19 恢复不受影响的根。
  [公告与修复说明](https://github.com/advisories/GHSA-6q48-6c7w-9gw2)。
- 实际路径：`apps/hub/src/cloud_memory_hub/transport.py` 的客户端 Hub 通信，以及
  固定上游 `one_memory/model_catalog.py`、`model_probe.py`、模型传输等均使用 HTTPX。
  `one_memory/http_client.py` 没有替换默认信任库；同版本本地已安装 HTTPX 0.28.1 的
  `_config.py` 在未指定证书环境覆盖时调用 `ssl.create_default_context(cafile=certifi.where())`。
  所以这不是仅安装了未使用的依赖，正常 HTTPS 路径会读取该根证书包。
- 最小处置：锁定已发布的修复版本并重建 Linux 镜像及 Mac 客户端包。当前源码已改为
  `certifi==2026.10.23`；**尚未据新制品复扫确认修复**。

### urllib3：词表首次下载可进入受影响的响应解析器

- 基线：`urllib3 2.7.0`，两条 HIGH；修复版本为 **2.8.0**。
  [chunk-size 内存放大](https://github.com/urllib3/urllib3/security/advisories/GHSA-vxq7-64xx-v4gw)
  对应 CVE-2026-97689；
  [HTTPS 代理 TLS 配置混用](https://github.com/urllib3/urllib3/security/advisories/GHSA-8988-9cw3-xx77)
  对应 CVE-2026-97687。
- 实际路径：固定上游 `hy_memory/client.py` 的写入流程调用 `count_input_tokens`，再经
  `hy_memory/utils/token_count.py` 调用 `tiktoken.get_encoding("cl100k_base")`。
  缓存缺失时，已安装的 `tiktoken/load.py` 用 `requests.get` 下载词表。
  Requests 2.34.2 即使采用默认 `stream=False`，也通过 `r.content → iter_content →
  raw.stream` 读取；urllib3 对 chunked 响应继续调用 `read_chunked`。
  词表摘要校验发生在下载后，不能限制解析 chunk-size 行时的内存分配。
- 这证明受影响客户端代码路径存在，**未证明攻击者能控制固定 HTTPS 词表源**。
  恶意响应还需要服务端、网络信任或等效条件。代理漏洞另外需要特定 HTTPS 代理与
  TLS 配置，本轮未证明该配置在当前产品中启用。
- 最小处置：当前源码已改为 `urllib3==2.8.0`，同时覆盖两项公告；
  **新镜像及客户端制品尚未重建、复扫**。

## 未命中当前启用入口的项

| 包／基线 | 公告与触发条件 | 当前源码证据与处置 |
| --- | --- | --- |
| aiohttp 3.14.3 | CVE-2026-111064：服务端首请求超时；CVE-2026-111066：服务端 `Request.post()` multipart 文件描述符耗尽；CVE-2026-123817：WebSocket 分片重组内存放大。三项扫描均 HIGH，上游各标为 Low／Moderate／Low，修复版本均为 3.14.4。 | Hub 的 HTTP 服务由 Uvicorn／FastAPI 承载，已核对的 Hub、MemHub、HY-Memory 调用代码没有 aiohttp server、`Request.post()` 或 aiohttp WebSocket 入口。不能声称这些入站攻击在 Hub 可达。统筹已安排同系列修复更新至 3.14.5，并配套满足其约束的 yarl 1.25.1；这是发行依赖更新，尚未重建验收。 |
| filelock 3.32.4 | CVE-2026-112940／GHSA-v4x8-3wr5-3wq8，仅影响 SQLite 支撑的 `ReadWriteLock`／`AsyncReadWriteLock`，还要求攻击者能在锁目录中创建或替换文件；修复版本 4.2.1。 | 固定上游 `one_memory/runtime.py`、`file_imports.py` 使用 `FileLock`；已安装 Hugging Face Hub 使用 `FileLock`／`SoftFileLock`。本轮检索未发现产品或这些依赖调用 filelock 的 SQLite 读写锁。Chroma 中同名 `ReadWriteLock` 是自己的线程锁，不是该漏洞类。当前不因这条公告要求升主版本。 |
| oauthlib 3.3.1 | CVE-2026-118669／GHSA-7984-qgvg-3j9c：OAuth 服务端 client_credentials／password grant 身份混淆，4.0.0 修复；CVE-2026-49272／GHSA-3jp2-3mrg-2vxc：服务端 device_code grant 验证缺失，4.0.1 修复。 | Hub 使用 `CloudBoundary` 和 `DeviceAccessStore` 自身的管理员／设备凭证验证，没有启用 OAuthLib provider、validator 或相应 token endpoint。间接安装 requests-oauthlib 不等于启用 OAuth 服务器。当前不因这两条公告要求升主版本。 |

aiohttp 原始公告：
[首请求超时](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-5wr8-gcqf-gqf3)、
[multipart](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-9xw5-w5jj-pvcg)、
[WebSocket](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-2g87-pp6c-29x5)。
filelock 原始公告：
[SQLite 读写锁](https://github.com/tox-dev/filelock/security/advisories/GHSA-v4x8-3wr5-3wq8)。
OAuthLib 原始公告：
[grant 身份混淆](https://github.com/oauthlib/oauthlib/security/advisories/GHSA-7984-qgvg-3j9c)、
[device_code](https://github.com/oauthlib/oauthlib/security/advisories/GHSA-3jp2-3mrg-2vxc)。

## Chroma：嵌入式路径与 HTTP 服务端必须分开判断

基线 `chromadb 1.5.9` 命中四项，扫描未提供修复版本：

- CRITICAL：[CVE-2026-45829／GHSA-f4j7-r4q5-qw2c](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c)，
  Chroma Python HTTP 服务的 collection 创建接受恶意模型仓库及 `trust_remote_code=true`。
- CRITICAL：[CVE-2026-45833／GHSA-36p7-vc44-83pf](https://github.com/advisories/GHSA-36p7-vc44-83pf)，
  具备 UPDATE_COLLECTION 权限的调用者通过集合更新配置触发代码执行。
- HIGH：[CVE-2026-45830／GHSA-2wm9-hf6c-p5cr](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr)，
  服务端跨租户授权检查缺失。
- HIGH：[CVE-2026-45831／GHSA-xph7-9rjv-w5fr](https://github.com/advisories/GHSA-xph7-9rjv-w5fr)，
  SimpleRBAC 没有按 tenant／database／collection 限定权限。

当前部署与调用证据：

1. 固定上游 `one_memory/hy_memory_bridge.py` 明确设置 Chroma 本地存储，并将
   `config.vector_store.host`、`port`、`api_key` 设为 `None`。
   `hy_memory/data/vector_store_chroma.py` 因此走 `chromadb.PersistentClient`，不是
   `HttpClient`；同版本已安装 Chroma 默认后端是 `chromadb.api.rust.RustBindingsAPI`。
2. 产品入口 `apps/hub/src/cloud_memory_hub/__main__.py` 启动自身的 Hub ASGI 应用，
   Docker 默认执行 `supervise`；`compose.yaml` 仅转发 Hub 的 8800 端口。
   没有启动、挂载或代理 Chroma Python FastAPI 的 collection／tenant HTTP 服务，
   也不依赖 Chroma SimpleRBAC 实施 Hub 项目授权。
3. HY-Memory 集合由代码使用固定的 HNSW metadata 创建；数据写入／检索提供已有的
   embeddings。产品事件或模型配置没有向 Chroma 透传用户指定的
   `embedding_function`／`trust_remote_code`。另一条本地检索路径
   `one_memory/retrieval/vector_store.py` 显式使用 `embedding_function=None`，并检查
   已存集合配置不包含 embedding function。
4. 上游 [issue 6717](https://github.com/chroma-core/chroma/issues/6717) 还描述了 Python
   客户端读取恶意远程集合配置的情形，不能仅凭“没有 HTTP server”忽略它；当前不使用
   该远程集合路径，且已核对集合配置输入来源。这里没有对所有外部构造数据库作安全承诺。

据以上源码，四项公告的攻击入口**当前未启用，不列为本次发行的可达阻断**。
这不是漏洞已修复或扫描误报的结论。保留版本命中记录；若将来启用 Chroma HTTP 服务、
远程 Chroma 集合、用户可控 embedding function 或改变数据导入信任边界，必须重新评估。

## OS 项核对与基础镜像处置

已从 Docker 官方 registry 核对并固定新的 Python 3.12 Bookworm 镜像索引
`sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df`，
其 Linux amd64 标注 Python 3.12.16。保持同一 Debian 发行版及 Python 次版本，
构建阶段补执行安全更新；APT 更新、升级和安装均显式使用 `Acquire::Retries=0`，
避免旧 APT 在安装自身修复前走存在问题的重试路径。实际新镜像版本及复扫仍待记录。

| 原报告范围 | 官方条件与当前处置 |
| --- | --- |
| APT CVE-2026-109518、OpenSSL CVE-2026-84782 | Debian 已发布 Bookworm `apt 2.6.2` 与 `openssl 3.0.22-1~deb12u2`；本次镜像重建应安装并核对这两个修复，不能仅沿用原缓存层。依据：[APT](https://security-tracker.debian.org/tracker/CVE-2026-109518)、[OpenSSL](https://security-tracker.debian.org/tracker/CVE-2026-84782)。 |
| SQLite CVE-2025-7458；CVE-2026-11822、11824、119373、119374、119379 | 条件分别涉及任意 SQL、损坏的 FTS5 数据库页、`detail=none` 与负 rowid，或原生 changeset API。Hub 没有任意 SQL／changeset 接口；导入搜索使用固定 FTS5 schema 和带引号的查询，不提供负 rowid／detail 参数；恢复仅针对管理员自有可信备份。外部构造数据库不在该信任边界内。保留版本命中，不能称 SQLite 全部已修复。依据：[任意 SQL](https://security-tracker.debian.org/tracker/CVE-2025-7458)、[损坏 FTS5](https://security-tracker.debian.org/tracker/CVE-2026-11824)、[负 rowid](https://security-tracker.debian.org/tracker/CVE-2026-119373)、[损坏索引删除](https://security-tracker.debian.org/tracker/CVE-2026-119374)、[changeset](https://security-tracker.debian.org/tracker/CVE-2026-119379)。 |
| zlib CVE-2023-45853、CVE-2026-76844、CVE-2026-85091 | Debian 明确说明 Bookworm zlib 没有构建前一项涉及的 contrib/minizip。后两项涉及原生 `gz*` 写入失败后继续写入；当前备份为未压缩 tar，未发现产品调用这些原生 API。原报告把 76844 标题写为 webpack 问题，与 Debian 条件不符，按供应商记录核对而非按标题判定。依据：[MiniZip](https://security-tracker.debian.org/tracker/CVE-2023-45853)、[gz_write](https://security-tracker.debian.org/tracker/CVE-2026-76844)、[gz_vacate](https://security-tracker.debian.org/tracker/CVE-2026-85091)。 |
| util-linux CVE-2026-53613、53615、78410；ACL CVE-2026-54369 | 涉及特权 mount／fstab、磁盘分区探测或特权 ACL 路径调用。产品没有这些入口；标准 Compose 使用非 root、全部 capability 删除、no-new-privileges、只读根文件系统，不开放设备或 Docker socket。依据：[mount](https://security-tracker.debian.org/tracker/CVE-2026-53613)、[分区探测](https://security-tracker.debian.org/tracker/CVE-2026-53615)、[bind mount](https://security-tracker.debian.org/tracker/CVE-2026-78410)、[ACL](https://security-tracker.debian.org/tracker/CVE-2026-54369)。 |
| gzip CVE-2026-41992、ncurses CVE-2025-69720、systemd CVE-2026-16742、Perl CVE-2026-9538 | 需要特制 LZW/LZH 解压、`infocmp`、运行中的 systemd-homed 或 Perl Archive::Tar。产品运行入口不调用这些服务／工具；备份解析为有界 Python tarfile。包的存在不等于已开放该功能，保留扫描命中。依据：[gzip](https://security-tracker.debian.org/tracker/CVE-2026-41992)、[ncurses](https://security-tracker.debian.org/tracker/CVE-2025-69720)、[systemd-homed](https://security-tracker.debian.org/tracker/CVE-2026-16742)、[Archive::Tar](https://security-tracker.debian.org/tracker/CVE-2026-9538)。 |

以上是当前标准部署和可信备份边界内的分诊，不是对特权容器、用户额外启用的服务或
任意第三方数据库的安全承诺。新增不可信数据库导入或放宽容器权限时必须重新评估。

## 处置状态与未测边界

- 本次已核对源码、原始报告、官方上游／GitHub 审核公告，以及本机同版本依赖实现；
  没有运行漏洞利用、生产调用、NAS 命令或日常客户端服务。
- 工作树中 `certifi==2026.10.23`、`aiohttp==3.14.5`、`urllib3==2.8.0`、
  `yarl==1.25.1` 已由统筹更新；锁文件生成与兼容性检查由统筹推进。
  **本记录尚无更新后构建、回归或 Trivy 复扫结果。**
- 必须在新 Linux 镜像和 Mac 客户端制品上确认实际安装版本，并记录对应构建／安装
  回归与镜像复扫结果。不能把源码版本号修改、旧测试或旧镜像报告当作修复验证。
- OS 条件与构建处置已核对；尚未取得新镜像安装版本及复扫证据，暂不作已修复结论。
- 源码审查没有发现的入口不等于覆盖所有可选部署方式。本轮也未全面审查 MEDIUM、LOW
  和 UNKNOWN 项，因此不能宣称“零漏洞”或整个依赖集合已完成安全审计。
