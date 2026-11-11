# 在 NAS 安装 Cloud Memory Hub

**服务装在 NAS 的 Docker 中；电脑只负责连接，不需要再装一个 App。**

## 一条命令完成首装和接入

在 Mac 或 Linux 电脑的目标项目目录运行以下命令。把 `USER@NAS` 换成 NAS 的 SSH 用户名与地址，或现有的 SSH 别名。

```sh
(cmh_installer=$(mktemp) && trap 'rm -f "$cmh_installer"' 0 && curl -q -fsSL --proto '=https' --proto-redir '=https' https://raw.githubusercontent.com/Tsou92/Cloud-Memory-Hub/v0.1.5/scripts/bootstrap-install.sh -o "$cmh_installer" && sh "$cmh_installer" --nas USER@NAS --agent codex)
```

也可以打开想共享的项目，把这句话交给能执行终端命令的 Agent：

> 请用 Cloud Memory Hub 的官方安装命令，在我的 NAS `USER@NAS` 完成首次部署并接入当前 Codex 项目。需要本人登录或确认授权时，让我在原界面处理。

安装器通过 SSH 检查 NAS、校验发行制品、初始化 Docker 服务和私网入口，再自动创建当前项目并配对这台电脑。默认使用当前目录，在主目录运行会建立一个 `Cloud Memory Hub` 文件夹。`--workspace` 可选择已有项目。已有的项目和设备可以继续通过 Hub 的「连接 Agent」页面加入。

NAS 需具备 Linux amd64、已启用的 Docker Compose、SSH 和已登录的 Tailscale。需要参数时，加在命令末尾的右括号前。已有实例使用[升级指南](development-runtime.md#已有实例升级保持配置和身份)。

| 场景 | 参数 |
| --- | --- |
| NAS 的 Tailscale 在 Docker 中运行 | `--tailscale-container tailscale`，将 `tailscale` 换成实际容器名 |
| 已有 HTTPS 反代指向 NAS 回环端口 | `--origin https://hub.example.com`，换成自己的地址 |
| SSH 使用非默认端口 | `--ssh-port 2222`，换成实际端口 |
| Claude Code CLI、OpenCode 或 Hermes | 将 `--agent codex` 换为 `--agent claude`、`--agent opencode` 或 `--agent hermes` |

支持 Apple Silicon Mac 上的 Claude Code CLI 和 Codex 桌面自动收发。安装器写好项目钩子，Codex 首次打开项目时按 **Review hooks** 提示信任。等待信任会显示为待完成。完成后普通聊天即可；模型配置只在需要长期记忆提炼时使用。OpenCode、Hermes 和通用 MCP 的范围见[设备接入指南](client-installation.md)。

首装完成后，安装器会打开受保护的初始化页。输入 `admin`，然后设置新密码。保存成功后进入后台。Mac 上要重新打开管理员入口，可把命令中的 `--agent codex` 换成 `--open-admin`。若已设密码，安装器会打开普通登录页并把现有凭证放到剪贴板。

## 把其他设备接入已部署的 Hub

在后台选择要共享的项目，进入「连接 Agent」，为目标设备生成一次性链接。把页面上的一句话发给 Agent，或在目标设备运行页面生成的命令。

在 Apple Silicon Mac 上选择 Codex 或 Claude Code CLI 时，默认会在 `Cloud Memory Hub Projects` 中为本次接入准备专用目录。安装后在 Agent 中打开结果显示的共享目录，再开始新的聊天。要沿用已有的本地项目，可以先在 Agent 中打开该项目，再让 Agent 用 `--workspace` 指定目录。Codex 首次打开共享目录时，需要在界面审查并信任已安装的钩子。

Linux、Windows 和其他 Agent 可使用 MCP。按安装结果重新加载连接，并验证能从 Hub 读写同一个项目。

下面保留已有发行版的逐步部署和手动安装说明。

推荐让能执行终端命令的电脑端 Agent 帮你部署。你负责开启 NAS 连接、完成登录；下载、校验和启动服务交给 Agent。

## 1. 在 NAS 开启连接

打开 NAS 的管理网页，完成两件事：

| 在哪里找 | 要做什么 |
| --- | --- |
| 应用中心、套件中心或容器管理 | 安装并启动 Docker／Container Manager。当前预编译镜像支持 Intel／AMD 的 Linux amd64 NAS；ARM NAS 暂不按已支持处理。 |
| 控制面板中的终端、SSH 或远程终端 | 开启 SSH，记下连接地址、登录用户名和端口。不同 NAS 的菜单名称可能不同。 |

电脑需要能连接 NAS：可以在同一局域网，也可以通过已有的 Tailscale 私有网络。这里填写的是 **NAS 地址**，不是 Docker 容器的内部地址；不用把 SSH 开放到公网。

SSH 是让电脑远程操作 NAS 的入口，**不要求 NAS 管理网页自带命令行**。安装前不必自己配置 Compose、导入镜像或准备模型密钥。

## 2. 把这段话发给电脑上的 Agent

打开有终端执行能力的 Claude Code、Codex 或其他 Agent，替换方括号内的信息，然后发送：

```text
请帮我把 Cloud Memory Hub 部署到 NAS。
NAS 地址：[填写地址]；SSH 用户名：[填写用户名]；SSH 端口：[填写端口，通常为 22]。
请先阅读 https://github.com/Tsou92/Cloud-Memory-Hub/blob/main/docs/nas-installation.md ，
按官方指南使用最新已发布版本，完成下载校验、Docker 部署、可访问的 HTTPS 入口和健康检查。
已有实例请先备份，保留原数据和配置。需要我登录或授权时，请指导我在本机完成，不要让我把密码或密钥发到聊天。
完成后给我管理页面地址，并指导我安全登录和连接 Agent。
```

第一次 SSH 登录、NAS 管理员授权或网络服务登录，可能需要你在自己的终端／浏览器中操作。Agent 应告诉你在哪里完成；无需把密码或密钥发给它。只有普通聊天功能、不能执行终端的助手无法替你实际部署。

## 3. 打开自己的管理页面

部署完成后，Agent 应给你一个从电脑能够打开的 **HTTPS 地址**，并指导你在本机安全取得登录凭证。登录后：

1. 在「设置 → 模型」填写自己的模型服务和 API Key，并完成连接测试。
2. 创建或选择项目，在「连接 Agent」生成这台电脑的一次性连接链接。
3. 把页面给出的安装话术交给 Agent；在另一台设备重复连接同一个项目，测试保存和读回一条记忆。

NAS 服务安装和电脑的 Agent 接入是两步：前者只需部署一次，后者在每台设备分别完成。详见[设备与 Agent 接入](client-installation.md)。

## 能直接在 Docker 镜像仓库里搜索吗？

正式发行通过 [GitHub Releases](https://github.com/Tsou92/Cloud-Memory-Hub/releases/latest) 提供预编译镜像文件，尚未提供已验证可匿名拉取的官方 Docker Hub／GHCR 镜像地址。若发行页暂时没有公开版本，请等待新版，不使用旧归档或测试制品代替。不要把仓库源码地址当成镜像名称，也不要使用来源不明的同名镜像。

支持「从文件导入镜像」的 NAS 可以导入发行镜像；但镜像导入后仍需初始化、持久存储和网络配置。**目前最省事的完整路径是上面的 Agent 代部署。** 将来提供镜像仓库入口时，也会同时给出完整的启动配置。

<details>
<summary>给执行部署的 Agent：按现有发行流程操作</summary>

以下复用已有发行包、Compose、初始化和备份能力，不新增安装服务。

1. **先确认目标。** 读取 NAS 架构、Docker／Compose 状态、可用存储及已有同名实例。只操作用户指定的 NAS；若是已有实例，使用[升级流程](development-runtime.md#已有实例升级保持配置和身份)，先备份，保留项目名、卷、凭证和连接。不要重新初始化已有数据。
2. **选择真实发行版。** 从官方 [Releases](https://github.com/Tsou92/Cloud-Memory-Hub/releases/latest) 获取一个已发布稳定版本的 NAS 镜像、源码包和 `SHA256SUMS`。三个文件属于同一版本；校验镜像和源码两个文件都实际存在且摘要匹配，不能只凭 `--ignore-missing` 返回成功认定文件齐全。不要在 NAS 从主分支重新构建来代替发行包。
3. **复用启动配置。** 将文件放在新的目录，导入镜像、解压源码。首次部署使用该版本的 `compose.yaml`，设置固定 `CMH_IMAGE` 与可访问的 `CMH_PUBLIC_ORIGIN`。已有实例沿用原 Compose 与 `.env`，仅按升级指南调整；保留原 data/config/governance/backups 挂载及权限、端口和资源限制，不用发行配置覆盖自定义配置。
4. **配置电脑可访问的入口。** 默认端口只绑定 NAS 回环地址。优先沿用已配置的 HTTPS 反向代理；没有入口时，已验证路径是 NAS 与电脑连接同一 Tailscale 网络，使用 Tailscale HTTPS Serve 转发到 Hub。需要账户登录或管理员启用 Serve 时，指导用户在自己的浏览器完成。不能把 NAS 上的 `localhost` 当作电脑能打开的地址，也不要直接改为公网裸 HTTP、关闭证书校验或改 SSH 配置来绕过限制。
5. **初始化并验收。** 仅全新实例运行一次 `setup`，再启动 `hub`。按[首次运行](development-runtime.md#发行版首次运行)核对服务健康及网页可达，确认重启后配置和持久卷仍保留。若未完成网络或权限步骤，明确指出尚未完成，不报告部署成功。
6. **交付使用入口。** 返回管理页面地址、部署版本、私有部署目录与停止／重启方法；指导用户在自己的交互终端查看管理员凭证并粘贴到登录页，不将凭证输出到 Agent 工具日志或聊天。模型 API Key 由用户在网页填写。接着引导连接当前 Agent，并实际保存、读回一条合成测试记忆。不要自动导入私人历史。

</details>

## 手动安装

熟悉 NAS 终端和 Docker 的用户可按下面的流程自行操作。命令应在 **通过 SSH 登录后的 NAS 终端**中执行；这是全新实例流程，已有实例直接看[升级说明](development-runtime.md#已有实例升级保持配置和身份)。

从[发行页](https://github.com/Tsou92/Cloud-Memory-Hub/releases/latest)下载同一版本的 NAS 镜像、源码包和 `SHA256SUMS`，放入 NAS 的一个新目录。下面的版本号仅演示文件命名；执行前必须将第一行改为发行页实际提供且已经下载的版本号：

```sh
CMH_VERSION=v0.1.3
test -f "cloud-memory-hub-${CMH_VERSION}-linux-amd64.tar.gz" &&
test -f "cloud-memory-hub-${CMH_VERSION}-source.tar.gz" &&
sha256sum --ignore-missing -c SHA256SUMS &&
docker load -i "cloud-memory-hub-${CMH_VERSION}-linux-amd64.tar.gz" &&
tar -xzf "cloud-memory-hub-${CMH_VERSION}-source.tar.gz" &&
cd "cloud-memory-hub-${CMH_VERSION}" &&
printf 'CMH_IMAGE=cloud-memory-hub:%s\n' "$CMH_VERSION"
```

确认镜像和源码都通过校验。把最后输出的 `CMH_IMAGE` 配置行写入新解压目录的 `.env`，再把 `CMH_PUBLIC_ORIGIN` 设为自己的 HTTPS 地址，继续[发行版首次运行](development-runtime.md#发行版首次运行)。镜像获取、导入和源码解压并不等于服务已经启动。
