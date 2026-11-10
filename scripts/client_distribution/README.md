# Cloud Memory Hub Mac 客户端安装包

关联 FR-01、FR-02、FR-03、FR-06、FR-07、FR-12、ENG-01，设计见 ADR-0060。

本包提供 `cloud-memory-setup`、`cloud-memory-agent`、`cloud-memory-mcp` 和
`cloud-memory-connector`。它连接您已经部署的 Hub，不启动本地 Core，不安装 Node，
不会修改 Claude、Codex 或其他 Agent 的配置，也不会自动启动后台服务。

## 安装

1. 安装本机架构的 Python **3.12**，并确保可以运行 `python3.12`。
2. 从正式发布页面核对压缩包 SHA-256，解压后进入安装包目录。
3. 执行 `sh install.sh`。若 Python 不在 PATH 中，使用其完整路径执行 `install.py`。
4. 在 Hub 网页的设备接入页面下载配对文件。使用安装器打印目录中的
   `cloud-memory-setup import --pairing-file <配对文件>` 完成接入。

安装器会先校验全部文件，再创建新的私有虚拟环境，通过 PyPI 下载锁定版本的依赖，
核对下载哈希并安装包内 wheel。安装需要联网；本包不是离线安装包。
不要使用 `sudo`。凭证只在后续配对时导入，不包含在发行包中。

默认安装至当前用户的 `Library/Application Support/CloudMemoryHub/client-releases`。
可以通过 `sh install.sh --target <新目录>` 指定其他位置；已有目录会拒绝覆盖。
`sh install.sh --verify-only` 仅验证文件完整性。哈希清单不能替代正式发布来源的真实性验证。
安装目录下的 `skill/cloud-memory-hub/SKILL.md` 是可手动安装到 Agent 的公共 Skill 模板，
不含设备凭证或本机绑定；具体 MCP 配置由配对后的 setup 命令生成。

每个包的 `manifest.json` 声明目标架构。安装器拒绝其他架构和非 macOS 系统；
构建机架构与 Python 架构必须一致。首批实际验证范围在发布说明中逐项列出，
存在 arm64 或 x86_64 构建能力不等于两种架构都已验证。不要将虚拟环境复制到另一台机器
或移动安装目录；应在目标位置重新安装。

## 升级与移除

新版本安装到新目录，通过新目录的命令进行接入和检查；旧版本继续保留。
本安装器不改变 Agent 连接或 LaunchAgent。切换已有后台服务时，先用旧版本的
`cloud-memory-setup service stop --config-file <原配置文件>` 停止服务，再执行
`cloud-memory-setup service uninstall --config-file <原配置文件>` 移除旧服务注册。
随后使用新版本的 setup 命令，对同一配置文件依次执行 `service install` 和 `service start`。
只停止服务仍会保留旧版本路径的 plist，不能代替卸载服务注册。
需要回滚时先停止并卸载新版本服务注册，再用保留的旧版本执行安装和启动。

移除前通过该版本的 setup 命令依次执行 `service stop` 和 `service uninstall`，
确认旧服务注册已移除后，再删除该版本的安装目录。
配对配置与待发送队列位于独立的数据目录；删除客户端版本不删除这些数据。

## 来源与构建

三个业务 wheel 直接由固定的 Hub、MemHub Core 和 HY Memory 源码构建，没有复制或重写
过滤、队列、解析器。当前仍保留上游 Python 依赖；不代表本机启动了记忆数据库或模型服务。
仅有源码发行的依赖在构建时按锁内哈希下载，并由固定构建工具生成 wheel，避免安装端临时编译。

`provenance` 包含上游 SHA、补丁清单与补丁、源码文件哈希、原始依赖锁、构建约束、上游许可证、
NOTICE 和上游 SBOM。上游 SBOM 是复用来源记录，不冒充本安装包的完整 SBOM；
本包实际文件以 `manifest.json` 为准，Python 依赖以两个 `*.lock` 文件及安装后的包元数据为准。

在项目根目录执行以下命令构建；输出目录必须尚不存在：

```sh
python3 -m scripts.build_client_distribution \
  --release 0.1.0-preview.1 \
  --python python3.12 \
  --output .local/releases/cloud-memory-hub-client-preview
```

构建使用固定 `uv==0.12.5`，校验固定上游与补丁，从 Hub 锁文件导出带哈希的精确依赖，
生成目录和同名 `.tar.gz`。只复制业务包源码与构建元数据，不复制开发虚拟环境、用户配置或数据库。
构建记录中的 `hub_worktree_dirty` 为 true 时属于工作树预览，不应冒充干净提交的正式发行。
