# 1.3.1 安装与可选离线下载

Release 只提供 **标准版（仅文件中转）安装包**、源码包和校验文件，不捆绑 Aria2 镜像。需要 **可选 Aria2 离线下载功能（文件中转 + Aria2 离线下载）**时，在标准安装包基础上从镜像仓库拉取本项目适配的 Aria2 镜像。

| 项目 | 标准版：仅文件中转 | 按需启用 Aria2 离线下载 |
| --- | --- | --- |
| Release 安装包 | `file-transfer-standard-1.3.1.tar.gz` | 同一个标准安装包，不另发包含 Aria2 的包 |
| 网盘镜像 | 安装包自带 | 安装包自带 |
| Aria2 镜像 | 无需下载 | `ghcr.io/lingmo77/filetransit-aria2:1.3.1` |
| Compose 文件 | `docker-compose.yml`，仅一个网盘服务 | `docker-compose.aria2.yml`，单个完整文件定义网盘和 Aria2 |
| 运行服务 | `app` | `app`、`aria2` |
| 数据卷 | 原 `ft-data` | 原 `ft-data` + `ft-offline`、`ft-aria2-state` |
| 默认端口 | 网盘 8000 TCP | 网盘 8000 TCP + BT 16881 TCP/UDP |
| 后台设置 | 离线下载保持关闭 | 填写站点根地址，再开启离线下载 |

标准安装包和共用源码包均包含两份独立完整 Compose 文件，**不将它们叠加使用**。源码包 `file-transfer-src-1.3.1.tar.gz` 不含任何镜像，其他 CPU 架构可从源码构建。当前发布镜像为 Linux amd64。

## 一、标准版（仅文件中转）

新安装解包并进入目录：

```sh
tar xzf file-transfer-standard-1.3.1.tar.gz -C /opt
cd /opt/file-transfer
cp .env.example .env
```

修改 `.env`：

```dotenv
FT_SECRET_KEY=<独立生成的随机密钥>
FT_ADMIN_USERNAME=admin
FT_ADMIN_PASSWORD=<强口令>
FT_PORT=8000
FT_WORKERS=4
```

可用 `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` 生成密钥。按反向代理情况设置 `FT_COOKIE_SECURE`、`FT_TRUST_PROXY`；不要公开 `.env`。标准版无需填写 `FT_ARIA2_*` 项。

加载网盘镜像并启动：

```sh
docker load -i images/file-transfer-1.0.tar
docker compose config --quiet
docker compose up -d --no-build
docker compose ps
```

源码部署以 `docker compose build app` 替换加载镜像步骤。到这里标准版已部署完成，默认只运行 `app`；后台离线下载保持关闭，导航不显示离线下载入口。

## 二、可选 Aria2 离线下载功能（按需执行）

> **本节为可选操作。只需要文件上传、分享和下载的标准版用户请跳过。**

使用同一个标准安装包，先按第一节创建配置、加载网盘镜像；已有站点保留原 `.env`。无需额外下载安装包，不需要手动填写站点或代理的 IP。

默认从 GitHub Container Registry 拉取本项目适配镜像，**不是任意第三方 Aria2 镜像**。适配镜像含私有 RPC、网络保护、磁盘监测及共享卷控制协议，普通 Aria2 镜像不能直接替换。

```sh
docker pull ghcr.io/lingmo77/filetransit-aria2:1.3.1
docker compose -f docker-compose.aria2.yml config --quiet
docker compose -f docker-compose.aria2.yml up -d --no-build
docker compose -f docker-compose.aria2.yml ps
```

也可将独立 `docker pull` 替换为 `docker compose -f docker-compose.aria2.yml pull aria2`。**单独使用这一份 Compose 文件即可同时管理网盘和 Aria2，不与标准版 Compose 叠加。**源码部署先执行 `docker compose -f docker-compose.aria2.yml build app`，再拉取 Aria2 并启动。

镜像地址可通过 `.env` 的 `FT_ARIA2_IMAGE` 选填覆盖，用于可信镜像代理或自行构建的相同适配镜像；不要填写其他 Aria2 项目的镜像。默认版本固定为 `1.3.1`。需要在其他架构自行构建时，可执行 `docker compose -f docker-compose.aria2.yml build aria2`；固定 Aria2 软件包版本通过 `FT_ARIA2_PACKAGE_VERSION` 设置，默认 `1.37.0+debian-3`。

两个服务健康后，在「管理后台 → 站点设置」填写 **站点根地址**，例如 `https://pan.example.com`，有自定义端口时照常填写，再开启「离线下载」。系统自动解析域名并刷新 IPv4/IPv6 黑名单；未填写或解析失败时暂停离线下载并重试，普通文件中转照常工作。

- 创建任务默认不公开，成功保存后保留 14 天，可在创建时修改。
- 下载完成后继续做种默认关闭，后台可设置做种时长和分享率。
- 全局下载和上传限速默认 0（不限速），单位 MB/s；上传限制也作用于 BT 下载期间的上传。
- 根据容量配置并发、暂存预算和磁盘空间下限。继续做种会额外保留下载源副本。

`FT_ARIA2_BLOCKED_NETWORKS` 只用于额外地址的高级选填限制，普通部署留空即可；CDN 隐藏源站等说明见 `DEPLOY.md` 第 10.1 节。RPC 6800 仅在容器网络使用，不映射到宿主机。RPC 密钥自动生成，无需更换原 `FT_SECRET_KEY`。

## 三、升级与切换

- 保留原 Compose 项目名、数据库、上传目录、`ft-data` 卷、`.env`、密钥和覆盖文件；已有站点不要重新执行 `cp .env.example .env`。
- 标准版升级只加载新网盘镜像并重建 `app`。
- **离线下载用户升级时，既要加载新网盘镜像，也要拉取本次适配的 Aria2 镜像并重建两个服务。**Aria2 软件包本身仍是 1.37.0，容器适配版本按本站版本号区分；不能仅看软件包号而沿用旧镜像。
- 从标准版启用离线下载，使用同目录的完整 `docker-compose.aria2.yml`、拉取 Aria2 镜像，启动后填写后台域名并开启功能。原用户和文件无需迁移到新卷。
- 停用时先在后台关闭并等待任务暂停，再执行 `docker compose -f docker-compose.aria2.yml stop aria2`，随后使用标准版配置更新 `app`。保留暂存和状态卷，避免丢失可恢复任务。
- 本次没有新增数据库迁移。回退应用须兼容迁移历史，不能还原旧数据库覆盖新增用户数据。

按 Release 的 `SHA256SUMS` 核验附件，包内 `images/SHA256SUMS` 核验网盘镜像。发布物不包含生产配置、密钥、服务器连接信息或用户数据。
