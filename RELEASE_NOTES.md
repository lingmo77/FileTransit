# 文件中转站 1.3.1

本次维护更新简化可选 Aria2 离线下载版的配置：复用后台已有的「站点根地址」，自动解析域名并维护下载服务的网络黑名单，无需手工填写站点 IP。同时修复后台状态刷新和小额度设置保存问题。

Release **只提供标准网盘安装包、源码包和校验文件**，不再捆绑 Aria2 镜像。需要 **可选 Aria2 离线下载功能**时，使用同一个安装包，从镜像仓库拉取本项目适配的 Aria2 镜像。标准版用户无需执行这些可选操作，完整配置见 [EDITIONS.md](EDITIONS.md)。

## 调整与修复

- 拒绝直接下载本站域名及其子域名的链接；自动解析站点域名的 IPv4/IPv6，并在 Aria2 容器内阻止访问对应 IP，覆盖直接 IP、跳转和 BT 出站连接。
- 定期刷新 DNS，并保留最近 24 小时解析出的地址。未填写有效站点地址、解析失败或保护失效时暂停离线下载并重试，普通文件中转照常工作。
- 独立网络保护进程仅保留容器内的 `NET_ADMIN`；Aria2 和磁盘监测进程不保留 capabilities。保护进程停止刷新时，内核放行凭证自动过期。
- `FT_ARIA2_BLOCKED_NETWORKS` 改为高级选填配置，兼容已有值。CDN 隐藏源站等未出现在该域名 DNS 中的额外地址仍可补充封锁，详见 `DEPLOY.md` 第 10.1 节。
- 后台连接状态持续刷新，保存设置或断网恢复后自动更新。
- 修复小于 0.5 GB 的已有上传上限阻止整个后台表单保存的问题；保留额度精度和 `-1` 不限额语义，修改站点域名不会改变原有额度。

## 升级与配置

本次没有新增数据库迁移，继续使用 `a6b9e2037c51`。保留原数据库、上传文件、数据卷、`.env`、密钥、Compose 项目名及覆盖配置，后台设置继续沿用。

**标准版：**

加载本次网盘镜像并重建 `app` 即可；默认 Compose 文件只有网盘服务。

**可选离线下载用户：加载新网盘镜像，再拉取 `ghcr.io/lingmo77/filetransit-aria2:1.3.1`，重建两个服务。**

Aria2 软件包版本仍为 1.37.0，但适配容器配置已更新，不能只更新网盘程序。使用单个完整 `docker-compose.aria2.yml`，其中已包含两个服务，不与标准版文件叠加。

在后台填写本站完整地址（例如 `https://pan.example.com`），再开启离线下载。普通部署无需填写 `FT_ARIA2_BLOCKED_NETWORKS`；已有配置可以保留。RPC 6800 不映射到宿主机，默认 BT 端口为 16881 TCP/UDP。

## 验证

- 本地 `.venv` 完整回归 213 项通过。
- 隔离 Docker 验证域名与对应 IP 封锁、DNS 变化、解析失败恢复、Linux capabilities 及保护进程停顿后的内核阻断。
- 真实引擎验证 HTTP、磁力、BT 文件选择、默认私密/14 天、做种及限速配置、暂停释放名额、同资源重试和大种子请求。
- 验证网盘停止时下载及网络保护继续运行，应用/引擎重启后恢复，取消任务不复活。
- 浏览器验证 1280/390/320 px 布局、后台设置保存与断网恢复、持续上传、速度显示、断网重试与刷新重选。

## 附件与启动

| 附件 | 内容 |
| --- | --- |
| `file-transfer-src-1.3.1.tar.gz` | 通用源码、迁移、两种 Compose、Aria2 Dockerfile/运行配置和文档 |
| `file-transfer-standard-1.3.1.tar.gz` | 标准版：通用源码与配置、Linux amd64 网盘镜像 |
| `SHA256SUMS` | 标准安装包和源码包的 SHA-256 |

标准版：

```sh
docker load -i images/file-transfer-1.0.tar
docker compose config --quiet
docker compose up -d --no-build
```

**仅选择可选 Aria2 离线下载版时：**

```sh
docker load -i images/file-transfer-1.0.tar
docker pull ghcr.io/lingmo77/filetransit-aria2:1.3.1
docker compose -f docker-compose.aria2.yml config --quiet
docker compose -f docker-compose.aria2.yml up -d --no-build
```

新安装先按 `EDITIONS.md` 创建 `.env`；已有站点保留原文件。包内 `images/SHA256SUMS` 可核验网盘镜像，其他 CPU 架构可使用源码构建。发布物不包含运行数据、生产配置、密钥或服务器连接信息。
