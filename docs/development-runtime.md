# 实验容器运行指南

状态：用于开发验证，尚未达到完整产品或 NAS 正式部署的交付条件。管理核心和网页复用 MemHub；网络边界见 [ADR-0002](adr/0002-nas-runtime-boundary.md)，未验证项见[运行记录](reviews/2026-10-22-runtime.md)。

## 构建与首次运行

需要 Docker 与 Compose、可访问 GitHub 和锁定依赖的网络环境。目标为 Linux amd64；其他主机架构运行该镜像需要 Docker 的模拟支持。镜像包含第三方许可证和上游来源清单，源码检出及依赖安装不会访问本机 Agent 历史。

在仓库根目录执行：

```sh
docker build --platform linux/amd64 --tag cloud-memory-hub:development .
docker compose run --rm setup
docker compose up -d --no-build hub
docker compose ps
```

Apple Silicon 开发主机可为 Compose 命令设置 `DOCKER_DEFAULT_PLATFORM=linux/amd64`。首次初始化创建管理员凭证与加密密钥；重复初始化会拒绝覆盖，不作为重启步骤。

打开 `http://localhost:8800`，输入初始化产生的管理员凭证。可在自己的终端查看：

```sh
docker compose exec hub cat /config/admin-token
```

不要把该输出粘贴到公开日志、Issue 或聊天。网页通过受限会话访问；容器重启后重新登录。模型配置界面来自上游，真实模型的测试／激活会访问相应提供商，当前验证使用未激活的合成配置。

## 数据与停止

- `hub_data` 保存 Core 数据及索引；`hub_config` 保存管理员凭证、加密密钥及加密的模型凭证数据库。
- 普通停止使用 `docker compose down`；不加 `--volumes`。保留卷后再次启动会复用配置和数据。
- 配置密钥丢失会导致加密凭证无法读取；当前还没有密钥轮换、灾难恢复或完整备份功能，不应依赖此实验版本承载唯一数据。
- 初始化中断后不要直接删除既有凭证重试；先检查配置卷并保留副本，避免覆盖已经使用的密钥。

## NAS 目标与当前边界

部署前应核对宿主可用资源及其他容器负载。当前容器限制为 3 GiB 内存、2 CPU，提炼任务并发为 1；这只是初始保护配置，不能替代真实模型、历史导入和索引负载测试。

运行数据与备份应使用独立的持久化位置，并在部署前核对存储冗余方案。计划将运行数据和历史备份版本放在不同卷，实际宿主路径、权限、容量告警和恢复流程需在部署阶段验证。磁盘阵列不能代替版本备份。

当前 Compose 只发布本机回环端口。远程入口必须配置明确的 HTTPS Origin（`CMH_PUBLIC_ORIGIN`）并经过认证边界；还未提供或验证生产反向代理配置。不同远程接入方式均待实际验证。容器地址不能作为 NAS 外部可达地址使用。

## 可复现验证

```sh
make check
make check-runtime
make check-container
```

容器检查创建随机命名的独立 Compose 项目和临时卷，验证初始化、身份／跨源拒绝、网页登录会话、模型配置密文存储、重启后的配置和密钥可读性，随后仅清理该次测试资源。它不访问用户模型、真实 Agent、NAS 或生产卷。

认证测试不代表浏览器全流程、六入口、跨设备同步或外部模型已经通过；以验证记录的证据范围为准。
