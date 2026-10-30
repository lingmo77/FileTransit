# 安装与升级

## 1. 下载、解压、启动

[Release](https://github.com/lingmo77/FileTransit/releases/latest) 只提供两个安装包和一个 `SHA256SUMS` 校验文件。GitHub 自动生成的 Source code 附件是仓库源码，不是另一个安装版本。

- **完整版** `file-transfer-with-aria2-1.4.0.tar.gz`：Linux amd64，内置网盘、Aria2 镜像、Docker Engine 29.9.2 和 Compose 5.6.0。已有 Docker 时直接复用，不升级、不重启已有 Docker。
- **原生版** `file-transfer-native-1.4.0.tar.gz`：Ubuntu 24.04+ / Debian 13+，安装器联网安装 Python 3.12+、虚拟环境及依赖，以专用用户运行 systemd 服务。默认不启用离线下载。

完整版需要 systemd、Linux 容器所需内核支持、`iptables`、`mount`、`tar`、`sha256sum`；普通完整安装的 Ubuntu 服务器通常已具备。精简的 Ubuntu/Debian 缺少 iptables 时，安装器会联网补装；完全断网前请先准备好这些系统工具。包内静态 Docker 不会自动更新，后续由管理员维护。

建议至少 2 核、2 GB 内存，为镜像解压和用户文件预留空间。完整版使用 8000 TCP 和 16881 TCP/UDP；原生版只使用 8000 TCP。防火墙按实际访问需求放行端口。

```bash
# 在保存下载附件的目录核验
sha256sum -c SHA256SUMS --ignore-missing
# 以下选择一个包；不要把两种安装方式安装在同一目录
tar xzf file-transfer-with-aria2-1.4.0.tar.gz -C /opt
cd /opt/file-transfer
sudo bash install.sh
sudo bash ftctl password
```

原生版替换包名即可。首次安装生成的 `.env` 包含随机管理员初始密码及签名密钥。初始密码不写入应用日志；在网页修改后，`ftctl password` 仍只显示安装时的初始值。请妥善保存 `.env`。

安装器以网卡地址生成站点地址，完整版本首次建库自动开启离线下载。公网 NAT、域名、CDN 或反向代理场景，在后台把“站点根地址”改为实际地址。站点解析或网络保护暂不可用时，离线下载暂停并自动重试。不要关闭这些保护，也不要开放 RPC 6800 到公网。

### 可选安装参数

仅在**首次安装**前设置，无需编辑配置模板：

```bash
sudo FT_INSTALL_PORT=9000 FT_INSTALL_SITE_URL=https://pan.example.com bash install.sh
```

已有 `.env` 时沿用原配置。端口有冲突会启动失败，请检查 `ftctl logs`，修改 `.env` 的 `FT_PORT` 后重启。完整版本容器内始终监听 8000；变量只控制宿主机端口。原生版变更端口后同样重启。

完整版也可用 `FT_INSTALL_BT_PORT` 选择首次安装的 BT 端口，默认 16881（TCP/UDP）。

`FT_SERVICE_NAME` 可用于同一服务器上原生服务的独立实例，默认 `file-transfer`，安装器会记录服务名供后续管理。完整版本使用目录对应的 Compose 项目名，首次安装可用标准 `COMPOSE_PROJECT_NAME` 设置，安装器会写入 `.env`；升级时保留相同项目名。

## 2. 常用管理

在安装目录运行：

```bash
sudo bash ftctl status
sudo bash ftctl start
sudo bash ftctl stop
sudo bash ftctl restart
sudo bash ftctl logs
sudo bash ftctl password
```

停止不会删除数据。完整版数据位于本 Compose 项目的 `ft-data`、`ft-offline`、`ft-aria2-state` 命名卷；原生版数据库、文件和离线共享目录位于安装目录的 `data/`。请勿执行删除卷、清库、覆盖数据库或重新生成密钥的操作。

## 3. 原生版以后添加离线下载

仅需要该功能时安装 Docker 和 Compose，使用本项目适配镜像；普通第三方 Aria2 镜像没有本项目的共享控制与网络保护协议，无法直接替换。

```bash
cd /opt/file-transfer
sudo mkdir -p data/offline data/aria2-state
sudo chown 10001:10001 data/offline data/aria2-state
sudo docker compose --project-directory "$PWD" -f deploy/aria2-native.yml up -d
```

在原 `.env` 中添加以下设置，保留原密钥和管理员信息：

```dotenv
FT_OFFLINE_DIR=/opt/file-transfer/data/offline
FT_ARIA2_RPC_URL=http://127.0.0.1:6800/jsonrpc
FT_ARIA2_RPC_SECRET_FILE=/opt/file-transfer/data/offline/control/rpc-secret
```

实际安装路径不同则对应调整。执行 `sudo bash ftctl restart`，后台确认站点根地址后开启离线下载。该容器仅向宿主机回环地址开放 RPC，文件通过共享目录入库，下载路径在容器中仍为 `/downloads`。专用用户 UID/GID 为 10001，与下载引擎保持一致。

## 4. 升级与已有 1.3.x 安装

先停止正在传输的任务；保留原安装目录、Compose 项目名、数据库、所有卷、上传文件、`.env` 和密钥。不要把新安装的 `.env` 覆盖到旧站点。

- **原生版**：覆盖程序文件时排除 `.env`、`data/`、`.venv/`；在原目录执行 `sudo bash install.sh` 更新依赖并重启。安装器不重置已有后台设置和管理员密码。
- **完整版本升级**：在原目录解压完整版后执行 `sudo bash install.sh`；已有项目有自定义 Compose 覆盖文件时，先按下一条合并自定义内容。
- **旧版 Docker 安装**：不要直接覆盖现有 Compose/rollback 覆盖文件。先在临时目录解包并校验、加载两个镜像，然后在原完整 Compose 中把 app 镜像改为 `file-transfer:1.4.0`、aria2 改为 `file-transfer-aria2:1.4.0`。保留原挂载、网络、端口、项目名和 `.env`，使用原配置组合重建服务。
- 标准版旧用户可继续只运行 app；需要离线下载时，合并根目录 `docker-compose.aria2.yml` 的共享卷和引擎设置，再在后台开启。已有数据库不会因 `FT_INITIAL_*` 环境变量被改写。

本版本无新增数据库迁移，迁移头仍为 `a6b9e2037c51`。回退程序必须兼容现有迁移版本，不能还原旧数据库覆盖新增数据。安装器不会自动创建生产备份，也不会删除用户数据。
