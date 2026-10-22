# 1.3.0 两种安装方式

Aria2 是可选组件。两种安装方式使用相同网盘程序和数据库迁移，区别是镜像附件、启动的服务和新增存储卷。

| 项目 | 标准版：仅文件中转 | 增强版：文件中转 + 离线下载 |
| --- | --- | --- |
| Release 安装包 | `file-transfer-standard-1.3.0.tar.gz` | `file-transfer-with-aria2-1.3.0.tar.gz` |
| 镜像 | 网盘 | 网盘 + Aria2 |
| Compose 文件 | `docker-compose.yml` | 一个完整 `docker-compose.yml`，同时定义网盘和 Aria2 |
| 运行服务 | `app` | `app`、`aria2` |
| 数据卷 | 原 `ft-data` | 原 `ft-data` + `ft-offline`、`ft-aria2-state` |
| 默认宿主机端口 | 网盘 8000 TCP | 网盘 8000 TCP + BT 16881 TCP/UDP |
| 额外配置 | 无 | 必填 `FT_ARIA2_BLOCKED_NETWORKS`，并在后台开启离线下载 |

每份安装包都自带一个可直接使用的 `docker-compose.yml`：标准版只有 `app`，增强版同时包含 `app` 与 `aria2`，无需叠加两个配置文件。两份安装包均含通用源码与配置；标准版不会加载或启动 Aria2。`file-transfer-src-1.3.0.tar.gz` 是两种方式共用的源码包，不含镜像。其他 CPU 架构请从源码构建。Docker/Compose 版本要求见 `DEPLOY.md`。

## 一、标准版

新安装解包并进入目录：

```sh
tar xzf file-transfer-standard-1.3.0.tar.gz -C /opt
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

可用 `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` 生成密钥。按反向代理情况设置 `FT_COOKIE_SECURE`、`FT_TRUST_PROXY`；不要公开 `.env`。标准版不需要填写任何 `FT_ARIA2_*` 项。

导入网盘镜像并启动：

```sh
docker load -i images/file-transfer-1.0.tar
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.yml up -d --no-build
docker compose -f docker-compose.yml ps
```

使用源码包时，将导入镜像替换为 `docker compose -f docker-compose.yml build app`，再启动即可。默认只运行 `app`。后台离线下载开关保持关闭，导航不显示离线下载入口，上传、分享、下载和用户管理照常使用。

## 二、含离线下载的增强版

新安装使用 `file-transfer-with-aria2-1.3.0.tar.gz`，解包后同样创建 `.env` 并修改标准版的密钥和管理员口令；已有站点保留原 `.env`，只补充缺失配置。

```dotenv
FT_ARIA2_BT_PORT=16881
FT_ARIA2_PACKAGE_VERSION=1.37.0+debian-3
FT_ARIA2_BLOCKED_NETWORKS=<本站及反向代理自身的实际公网IP/CIDR，逗号分隔>
```

`FT_ARIA2_BLOCKED_NETWORKS` 必须填写实际地址，不能使用文档占位值；为空时 Aria2 拒绝启动。公网 IP 或 DDNS 地址变化后须同步修改并重建 Aria2。非公网、回环、Docker 网络等受保护地址由内置出口规则处理。

导入两份镜像，使用增强版包内的单个默认配置：

```sh
docker load -i images/file-transfer-1.0.tar
docker load -i images/file-transfer-aria2-1.37.0.tar
docker compose config --quiet
docker compose up -d --no-build
docker compose ps
```

共用源码包中，`docker-compose.yml` 是标准版完整配置，`docker-compose.aria2.yml` 是增强版完整配置，两者独立。增强版从源码构建时先执行 `docker compose -f docker-compose.aria2.yml build app aria2`，之后用同一个 `-f docker-compose.aria2.yml` 启动；无需再传标准版文件。BT 使用 16881 TCP/UDP（可改），RPC 6800 只供容器内部访问，不增加宿主机映射。

两个服务健康后，在「管理后台 → 站点设置 → 离线下载」开启功能，刷新页面后导航显示独立任务页。后台设置：

- 任务默认不公开，成功保存后保留 14 天，可在创建时修改。
- 完成后继续做种默认关闭；开启时配置最长时间和分享率，达到任一条件停止。
- 全局下载和上传限速默认 0（不限速），单位 MB/s；上传限制同时作用于 BT 下载期间的上传及完成后做种。
- 根据容量设置并发、暂存预算和磁盘可用空间下限。保存成品和做种源副本会同时占用空间。

RPC 密钥由下载服务自动生成并保存在共享控制卷，不填写到前端，也不需要更换现有 `FT_SECRET_KEY`。详细安全与恢复机制见 `DEPLOY.md` 第 10 节。

## 三、升级及切换

- 已有站点保留原 Compose 项目名、`ft-data` 数据卷、数据库、上传文件、`.env`、密钥及原有覆盖文件。不要对已有站点重新执行 `cp .env.example .env`。
- 标准版与增强版各使用自己包内的 `docker-compose.yml`；增强版额外加载 Aria2 镜像。共用源码部署分别选择标准版或增强版完整配置文件，不能把两个文件叠加。已有自定义覆盖文件仍须保留。
- 标准版切换增强版时，使用增强版包内的完整 `docker-compose.yml`、加载 Aria2 镜像并补充配置，再启动并在后台开启功能。原用户和文件无需迁移到新卷。
- 停用离线下载时，先在后台关闭并等待进行中的任务暂停，再使用增强版配置执行 `docker compose stop aria2`；随后切换标准版完整配置更新 `app`。保留暂存和状态卷，以便将来恢复；不要删除数据库记录或存储卷。
- 应用启动时自动增量迁移。回退需使用兼容迁移历史的应用，不能还原旧数据库覆盖新增数据。

更新前按 `SHA256SUMS` 核验发布包；包内 `images/SHA256SUMS` 核验镜像。发布包不包含生产 `.env`、密钥、数据库、上传目录或服务器连接信息。
