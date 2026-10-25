# 文件中转站 · 部署实施说明

适用版本：1.3.1，Release 仅提供标准网盘安装包；需要离线下载时按需从镜像仓库拉取适配的 Aria2 镜像。Aria2 是可选组件；先阅读 [EDITIONS.md](EDITIONS.md) 选择安装包与配置。第 2–8 节的基础命令用于标准版，可选 Aria2 离线下载版按第 10 节使用同时定义网盘与 Aria2 的单个完整配置文件。

本文档面向执行部署的人，从头到尾照着做即可。全流程只需要 Docker，不需要装 Python、Node 或数据库。

---

## 1. 部署前确认

### 1.1 目标机器要求

| 项 | 最低 | 建议 | 说明 |
| --- | --- | --- | --- |
| 操作系统 | Linux x86_64 | Ubuntu 22.04 / 24.04 | 镜像按 **amd64** 构建，ARM 机器需自行重新构建 |
| Docker | 25.0+（离线镜像包） | 使用受维护版本 | `docker --version`；旧版本可尝试源码构建 |
| Docker Compose | v2.24.0+（`docker compose`） | 使用受维护版本 | 当前文件使用 `env_file.required`，不是旧 `docker-compose` |
| 内存 | 1 GB | 2 GB+ | `FT_WORKERS` 每进程约 150–250 MB |
| 磁盘 | 5 GB | 按上传量预留 | 用户上传的文件全部存在数据卷里 |

一条命令自查：

```bash
docker --version && docker compose version && uname -m && free -h && df -h /
```

`uname -m` 必须是 `x86_64`。**如果输出 `aarch64`，离线包里的镜像不能用**，请走第 3 章的源码方式部署。

### 1.2 端口

默认监听 **8000**。要改就改 `.env` 里的 `FT_PORT`（只影响宿主机映射端口，容器内始终是 8000）。

```bash
ss -tlnp | grep 8000     # 确认端口没被占用
```

### 1.3 你会得到两个包

| 包 | 大小 | 用途 |
| --- | --- | --- |
| `file-transfer-src-*.tar.gz` | 以 Release 为准 | 源码、Compose、Aria2 配置和文档，需联网构建 |
| `file-transfer-standard-*.tar.gz` | 以 Release 为准 | 标准版，仅包含网盘镜像 |

镜像安装包使用原始 `docker save` tar 保存，校验值见 `images/SHA256SUMS`；发布包整体使用 gzip 压缩。请为镜像加载和用户数据预留空间。

离线包不依赖部署时联网下载基础镜像和依赖。源码包便于在其他架构构建。

当前 Compose 使用的 `env_file.required` 需要 Compose 2.24.0 及以上，见 [Docker 官方说明](https://docs.docker.com/reference/compose-file/services/#required)。

---

## 2. 方式一：离线包部署（推荐）

### 2.1 上传并解包

把 `file-transfer-standard-*.tar.gz` 传到目标机器（`scp`、U 盘、内网共享均可），然后：

```bash
tar xzf file-transfer-standard-1.3.1.tar.gz -C /opt
cd /opt/file-transfer
```

不需要 `--strip-components`：包里已经是一层 `file-transfer/` 目录。

### 2.2 导入镜像

```bash
docker load -i images/file-transfer-1.0.tar
```

标准版预期加载 `file-transfer:1.0`；可选 Aria2 离线下载版另加载 Aria2，见第 10 节。

```bash
docker images file-transfer
```

> **关于镜像格式**：离线附件可能使用 OCI layout（`index.json` + `blobs/sha256/`）。使用 Docker 25.0 及以上；旧版本无法识别时使用源码包现场构建。
>
> 在 **Docker 25.0 及以上**（或启用了 containerd 镜像存储的版本）上 `docker load` 可直接识别。若目标机器 Docker 较老、报出无法识别的格式，请改用第 3 章的**源码包**方式现场构建，效果完全等价。
>
> 这个 tar 的完整性可以校验：
> ```bash
> sha256sum images/file-transfer-1.0.tar
> # 见包内 images/SHA256SUMS
> ```

### 2.3 生成配置

```bash
cp .env.example .env
```

**必须修改密钥和管理员口令**；离线下载还需填写第 10 节的出口封锁地址：

```bash
# 1) 生成密钥并填进 FT_SECRET_KEY（下面这条命令直接输出可用的值）
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

# 2) 改掉默认管理员口令 FT_ADMIN_PASSWORD（默认是 admin，等于没有防护）
```

用 `sed` 一步到位（把 `<粘贴>` 换成上一步的输出）：

```bash
sed -i "s|^FT_SECRET_KEY=.*|FT_SECRET_KEY=<粘贴>|" .env
sed -i "s|^FT_ADMIN_PASSWORD=.*|FT_ADMIN_PASSWORD=<你的强口令>|" .env
```

> `FT_SECRET_KEY` 用于给会话签名、加密 SMTP 密码。**部署后不要再改**：改了会导致所有登录会话失效、数据库里已加密的 SMTP 密码无法解密。

**内存小于 2 GB 的机器**把并发降下来，否则容易被 OOM Killer 干掉：

```bash
sed -i "s|^FT_WORKERS=.*|FT_WORKERS=2|" .env
```

### 2.4 启动

```bash
docker compose up -d
```

标准版镜像安装包使用 `docker compose up -d --no-build`，后台离线下载保持关闭，无需配置 Aria2。可选 Aria2 离线下载版步骤见第 10 节。等几秒后看状态：

```bash
docker compose ps
```

等到 `STATUS` 显示 **`Up ... (healthy)`** 才算成功。首次启动应用会自动建库、跑数据库迁移，约 5–20 秒。

### 2.5 验证

```bash
curl -s -o /dev/null -w "健康检查 HTTP %{http_code}\n" http://127.0.0.1:8000/healthz
```

出现 `HTTP 200` 即部署成功。然后用浏览器访问 `http://<服务器IP>:8000/`。

---

## 3. 方式二：源码包部署

目标机器能联网（能拉 `python:3.12-slim`、能 `pip install`）时可用。

```bash
tar xzf file-transfer-src-1.3.1.tar.gz -C /opt
cd /opt/file-transfer
cp .env.example .env
# 修改密钥、管理员口令和 Aria2 出口封锁地址，见 2.3、10.1

docker compose up -d --build     # 注意这里有 --build
```

构建需要下载基础镜像和 Python 依赖，视网络情况约 3–10 分钟。

> **构建时若报错拉不到镜像**，多半是目标机器给 dockerd 配了失效的代理。检查：
> ```bash
> systemctl cat docker | grep -i proxy
> ```
> 指向的代理不通就先停用它，再 `systemctl daemon-reload && systemctl restart docker`。

### 3.1 在没有 Docker 的机器上预先构建镜像

如果目标机器完全没有外网，但你有另一台能联网的 x86_64 机器：

```bash
# 在有网的机器上
tar xzf file-transfer-src-*.tar.gz && cd file-transfer
docker compose build
docker save file-transfer:1.0 -o file-transfer-1.0.tar
# 把 tar 拷到目标机器
docker load -i file-transfer-1.0.tar
```

---

## 4. 首次登录后要做的三件事

用 `FT_ADMIN_USERNAME` / `FT_ADMIN_PASSWORD` 登录（默认 `admin` / `admin`），然后：

### 4.1 立刻改管理员密码

后台 → **用户管理** → 找到 admin → 改密码。若 `.env` 里还留着弱口令，登录后第一件事就是改掉。

### 4.2 设置单文件上限

后台 → **站点设置** → **单文件大小上限**。

> ⚠️ `.env` 里的 `FT_MAX_UPLOAD_MB` **只是首次建库时的播种默认值**，不构成硬上限。建库之后再改它没有任何效果，真正生效的是后台这个设置项。改完**不需要重启**，最长 15 秒内全量生效（多 worker 的进程内缓存刷新周期）。

默认 20480 MB（20 GB）。

### 4.3 配置邮件服务（可选，但会影响注册）

不配 SMTP 时：注册流程发不出验证邮件，新用户无法自助完成注册。

后台 → **邮件服务**：填 SMTP 地址、端口、账号、**授权码**（注意不是登录密码）。

常见配置：

| 服务商 | 地址 | 端口 | 加密 | 密码填什么 |
| --- | --- | --- | --- | --- |
| 163 邮箱 | `smtp.163.com` | 465 | SSL | 授权码 |
| QQ 邮箱 | `smtp.qq.com` | 465 | SSL | 授权码 |
| Gmail | `smtp.gmail.com` | 465 | SSL | 应用专用密码 |

填完点 **发送测试邮件** 实时验证。

**暂时不想配邮件**：把后台 → 站点设置 → **需要邮箱验证** 关掉，用户注册后可直接登录。

---

## 5. 日常运维

### 5.1 常用命令

```bash
cd /opt/file-transfer

docker compose ps                  # 查看状态
docker compose logs -f --tail=100  # 跟踪日志
docker compose restart             # 重启（不丢数据）
docker compose down                # 停止并删除容器（数据卷保留）
docker compose up -d               # 再次启动
```

**不要用 `docker compose down -v`** —— `-v` 会连同数据卷一起删除，用户上传的文件和数据库全部丢失。

### 5.2 数据在哪

原有网盘数据保留在 Docker 命名卷 `<目录名>_ft-data`，目录名叫 `file-transfer` 时即 `file-transfer_ft-data`。里面是：

```
/app/data/app.db      # SQLite 数据库（用户、文件记录、日志、站点设置）
/app/data/storage/    # 用户上传的文件本体
```

查卷的实际路径：

```bash
docker volume inspect file-transfer_ft-data --format '{{.Mountpoint}}'
```

### 5.3 数据保留与回退

按项目维护约定，更新时不自动创建生产备份。直接更新必要代码，保留现有数据库、用户文件、命名卷、`.env` 和密钥。不得清空卷后还原旧数据库，这会丢失升级后的新增数据。回退须使用能识别当前迁移版本的镜像，或先关闭离线入口和协调器，不执行数据库降级。

### 5.4 日志与过期清理

- **操作日志**在后台「操作日志」页：记录登录时间、IP、浏览器、上传文件等，支持按关键词/类型/日期筛选、导出 CSV、按时间范围清空（大幅省磁盘）。
- **过期文件**由内置维护任务自动删除（默认每 10 分钟一轮），到期即删，公开与否都会删——私有文件同样会被清理。文件最长保留 30 天（后台可调），管理员可将个别文件设为「长期有效」。
- 清理周期由 `FT_CLEANUP_INTERVAL_MINUTES` 控制。

---

## 6. 反向代理与 HTTPS

生产环境建议放在 Nginx / Caddy 后面并启用 HTTPS。

**关键：`client_max_body_size` 必须设得足够大**，否则大文件上传会被 Nginx 用 413 拦掉，且应用根本收不到请求。

```nginx
server {
    listen 80;
    server_name files.example.com;

    # 必须 ≥ 后台设置的「单文件大小上限」，否则大文件上传直接被拒
    client_max_body_size 20G;
    # 大文件上传/下载耗时长，超时要放宽
    client_body_timeout   600s;
    proxy_read_timeout    600s;
    proxy_send_timeout    600s;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 多线程下载（IDM / 迅雷）依赖 Range，不要关掉
        proxy_http_version 1.1;
        proxy_buffering off;
    }
}
```

启用后，`.env` 里同步改两项再 `docker compose up -d`：

```bash
FT_TRUST_PROXY=true      # 否则日志里记录的是 Nginx 的内网 IP，不是用户真实 IP
FT_COOKIE_SECURE=true    # 走 HTTPS 时开启，Cookie 只经加密连接发送
```

---

## 7. 常见问题

**容器起来后一会儿就退出／`docker compose ps` 不是 healthy**

```bash
docker compose logs --tail=200
```

多为 `.env` 语法错误（值不要加引号）、端口被占用、或内存不足被 OOM 杀掉（`dmesg | grep -i oom`）。内存紧张就把 `FT_WORKERS` 降到 1–2。

**页面能打开但登录后立刻掉线**

`FT_SECRET_KEY` 在部署后被改过，或每次重启都被重新生成。把它固定成 `.env` 里一个不变的值。

**大文件上传失败，日志里没有对应请求**

Nginx 的 `client_max_body_size` 太小，请求根本没转发到应用。见第 6 章。

**IDM / 迅雷多线程下载很慢或失败**

确认反代里 `proxy_buffering off`、没有过滤 `Range` 请求头。应用侧已支持 HTTP Range / 206 与 `Accept-Ranges: bytes`。

**注册收不到验证邮件**

后台「邮件服务」里点 **发送测试邮件** 看具体报错。163 / QQ 邮箱必须用**授权码**而不是登录密码，且需在邮箱后台先开启 SMTP 服务。

**日志里所有人的 IP 都是 `172.x.x.x`**

反代后忘了设 `FT_TRUST_PROXY=true`。

---

## 8. 升级到新版本

```bash
cd /opt/file-transfer
# 1) 保留原有数据库、卷、.env 和密钥，不自动备份或清库
# 2) 用新包覆盖代码
tar xzf file-transfer-standard-新版.tar.gz -C /opt
#    .env 不在包里，不会被覆盖
docker load -i images/file-transfer-1.0.tar
docker compose up -d
```

数据库迁移由应用启动时自动完成（`app.core.bootstrap`），**无需手动执行**，多 worker 之间用文件锁串行化。升级后回到 2.5 验证。

> ⚠️ **只 `docker compose build` 而不 `up -d` 会留下不一致状态。** 构建会把 `file-transfer:1.0` 标签指向新镜像，但**正在运行的容器仍然绑在旧镜像 ID 上**；旧镜像随即变成无标签状态，`docker system df` 会把它算成可回收空间（新镜像约 308 MB）。此时必须 `docker compose up -d`，Compose 会检测到镜像变化并重建容器，让绑定关系重新对齐。上面的流程已经包含 `up -d`，照做即可。
>
> 验证是否对齐：`docker system df` 里 Images 的 RECLAIMABLE 应为 `0B`。

---

## 9. 部署验收清单

- [ ] `docker compose ps` 显示 `Up ... (healthy)`
- [ ] `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/healthz` 返回 `200`
- [ ] 浏览器能打开首页
- [ ] 已用 `FT_ADMIN_PASSWORD` 登录，且**已改掉默认口令**
- [ ] 后台「站点设置」里已确认单文件上限
- [ ] 上传一个文件 → 首页可见 → 点下载能下来
- [ ] 游客（无痕窗口）不登录也能看到并下载公开文件
- [ ] 后台「操作日志」能看到刚才的上传记录，含 IP 与浏览器
- [ ] 配了反代的话：`FT_TRUST_PROXY=true`、`client_max_body_size` 已放宽
- [ ] 配了邮件的话：测试邮件发送成功
- [ ] 已确认数据库、用户上传目录、数据卷和配置保持完整；按本项目维护约定不自动创建生产备份

## 历史回退与数据库迁移兼容

此前分片上传版 1.2.0 曾撤回到 1.1.2。2026-10-17 用户授权将当前整文件上传代码的审计与布局修复统一发布为 1.2.0。数据库中的 `upload_sessions` 表和 `d42e8f6b9a10` 迁移记录继续保留，避免通过降级数据库影响新增的用户和文件。当前源码保留该迁移脚本，因此从当前源码重新构建可以识别已升级的数据卷。

直接使用原始 1.1.2 镜像时，它不包含这个迁移脚本，需要继续挂载保留的新版迁移目录。以 `/opt/file-transfer/docker-compose.rollback.yml` 为例：

```yaml
services:
  app:
    volumes:
      - ./migrations:/app/migrations:ro
```

先确保脚本可读：`chmod -R a+rX migrations`，再执行 `docker compose -f docker-compose.yml -f docker-compose.rollback.yml up -d --no-build --pull never app`。原 `.env` 和 `ft-data` 数据卷继续使用，不执行数据库降级或数据卷删除。

部署后的功能修改须先让用户手动测试，询问「发布还是继续修改/新增功能」；只有用户明确决定发布，才统一推送代码和更新 Release。2026-10-17 的新版 1.2.0 按用户明确发布指令执行，以本次审计与布局修复代码为准。

## 10. Aria2 离线下载服务（可选）

> **本章为可选配置。仅使用标准版文件中转的用户无需执行本章；只有需要 Aria2 离线下载功能时，才按以下步骤配置和启动。**

### 10.1 构建与接入

本地 Windows 开发使用 `.venv`，不需要安装 Docker。把经本地验证的源码同步到原服务器部署目录；不要同步本地 `data/`、`.env` 或 `.venv/`。保持原 Compose 项目名与 `ft-data` 卷，直接更新必要文件，不自动创建生产备份。

源码中的 `docker-compose.yml` 仅定义 app；`docker-compose.aria2.yml` 是独立的完整可选 Aria2 离线下载版配置，已同时定义 app、Aria2 及所需卷，不与标准版叠加。标准安装包和源码包均包含这两个文件。需要离线下载时单独使用 `-f docker-compose.aria2.yml`；不提供捆绑 Aria2 镜像的 Release 安装包。

可选 `aria2` 适配镜像为 `ghcr.io/lingmo77/filetransit-aria2:1.3.1`，从 `docker/aria2/Dockerfile` 构建，使用 Debian trixie 的固定 Aria2 包版本 `1.37.0+debian-3`。必须使用本项目适配镜像，不能直接替换为其他 Aria2 镜像。修改基础镜像/软件包版本需重新完成协议和网络防护联调。

在服务器原有 `.env` 中只增补必要项目，不覆盖原文件：

```dotenv
FT_ARIA2_BT_PORT=16881
FT_ARIA2_PACKAGE_VERSION=1.37.0+debian-3
```

服务启动后，在「管理后台 → 站点设置」填写**站点根地址**，例如 `https://pan.example.com`；自定义端口可写为 `https://pan.example.com:8443`。复用这一项即可，**无需手动填写站点或反向代理 IP**。

系统拒绝直接下载本站域名及其子域名的链接；独立网络保护进程每 20 秒解析站点域名，将全部 IPv4/IPv6 自动加入容器网络黑名单，同时保留最近 24 小时解析到的地址，防止地址轮换后旧连接绕过。网络层限制也覆盖直接 IP、HTTP 重定向和 BT 的出站连接。DDNS 地址变化后自动刷新，不必修改 `.env` 或重建容器；新解析结果通常在约 20 秒后刷新，另需 DNS 查询与策略同步时间。若域名与其他下载站点共享同一 IP，该 IP 上的其他站点也会被拦截。

未填写有效站点地址、DNS 解析失败或网络保护中断时，离线下载暂停并自动重试；容器可正常启动，普通文件中转不受影响。下载进程与磁盘监控不保留任何 Linux capability；独立网络保护进程仅保留容器网络命名空间内的 `NET_ADMIN`，不监听端口、不操作宿主机防火墙，停止刷新时内核中的短期放行凭证会自动过期。

**高级选填：**

若使用 CDN，域名解析只能获取 CDN 入口，无法自动发现隐藏的源站 IP 或其他未出现在该域名 DNS 中的公网管理入口。需要额外封锁时，可在原 `.env` 中补充 `FT_ARIA2_BLOCKED_NETWORKS`，值为英文逗号分隔的 IP/CIDR，修改后重建 Aria2；原有配置继续生效。普通部署保持为空即可。内网、回环、Docker 网络及云元数据地址已有内置限制。

```bash
docker compose -f docker-compose.aria2.yml config --quiet
docker compose -f docker-compose.aria2.yml build app  # 已从标准包加载网盘镜像可跳过
docker compose -f docker-compose.aria2.yml pull aria2
docker compose -f docker-compose.aria2.yml up -d --no-build aria2 app
docker compose -f docker-compose.aria2.yml ps
```

`config --quiet` 避免把展开后的密钥打印到终端。RPC `6800` 只在 Compose 网络中访问，不能增加公网映射。BT 使用配置端口的 TCP/UDP；部署时先确认端口占用和外部防火墙/NAT 环境。普通网盘服务允许在 Aria2 未就绪时工作，离线服务会显示不可用并自动重试。

### 10.2 新卷与密钥

| 卷 | 挂载与内容 |
| --- | --- |
| 原 `ft-data` | 仅 app `/app/data`；原有数据库与成品文件，保持不变 |
| `ft-offline` | app `/app/offline`、Aria2 `/downloads`；`tasks/<随机ID>/payload`、私有源和 `control/` |
| `ft-aria2-state` | 仅 Aria2 `/state`；session、DHT 和引擎私有配置 |

Aria2 首次启动在 `control/rpc-secret` 自动生成密钥并持久保留，不输出密钥。app 通过 `FT_ARIA2_RPC_SECRET_FILE` 读取该文件；不需要重置现有 `FT_SECRET_KEY`。仅初始化新增卷根目录和控制文件权限，不遍历或改动原用户 storage。

Aria2 使用 UID `10001`；只读磁盘监测使用 UID `10002`、组 `10001`。任务目录和策略文件应允许该组读取（目录至少 `0750`、文件 `0640`），不得向公网提供 `control/` 或任务暂存目录。

### 10.3 出口限制与独立磁盘监测

下载容器仅在自己的网络命名空间初始化 nftables OUTPUT 规则；不使用 host 网络、privileged 或 Docker socket，不修改宿主机全局防火墙。仅允许明确 DNS 请求及已建立连接回复，拒绝非公网/受保护目的地址。回环 RPC 例外仅给予 watchdog UID，下载进程本身不能利用该例外访问本地 RPC。

初始化所需能力由 Compose 显式列出；随后 Aria2 和磁盘监测进程均降权并清空能力 bounding set，禁止后续提权。单独的网络保护进程仅保留 `NET_ADMIN` 来更新容器内的域名地址黑名单。规则安装失败时引擎不启动，不以关闭防护绕过联调。健康检查仅内部调用 `getVersion`，不打印 secret。

web 协调器将预算和任务意图原子写入 `control/policy.json`：

```json
{"temp_max_bytes":42949672960,"min_free_bytes":1073741824,"tasks":{"<任务ID>":{"max_bytes":104857600,"desired_state":"running"}}}
```

独立 watchdog 每约 2 秒检查已分配暂存字节和磁盘可用空间，即使 app 重启仍持续工作。缺少有效策略、未知任务、任务预算超出或触及磁盘低水位时暂停任务；结果写入 `control/watchdog-status.json`，由协调器尊重暂停原因。磁盘保护留有传输安全余量，属于软保护；命名卷本身不是硬配额。

### 10.4 恢复、做种和限速

引擎持久化 `.aria2` 控制文件、种子/磁力元数据、会话和 DHT。启动时恢复任务保持暂停，由协调器核对数据库意图后放行，防止陈旧 session 复活已取消任务。单独更新 app 时不重建健康的 Aria2，下载可继续。

做种、全局下载/上传限速在「管理后台 → 站点设置」配置。默认完成后不做种，上下行 `0` 都表示不限速。继续做种使用暂存源副本，网盘成品通过复制入库，两份占用纳入预算。关闭继续做种会停止现有做种任务；不会自动恢复已经结束的任务。BT 下载期间仍可能上传，上传限速同时覆盖该阶段。

### 10.5 标准安装包与可选镜像

Release 仅发布标准网盘安装包、源码包和校验文件。标准包只包含网盘镜像，Aria2 不打包到 Release 附件中；需要离线下载时按第 10.1 节从镜像仓库拉取。

服务器导出网盘镜像：

```bash
docker save file-transfer:1.0 -o file-transfer-1.0.tar
```

Windows 本地打包：

```powershell
.venv\Scripts\python.exe package.py --version <批次名> --image <网盘镜像tar>
```

Linux 可使用 `./package.sh --image <网盘镜像tar>`。包内 `images/SHA256SUMS` 只记录网盘镜像。GitHub 工作流 `.github/workflows/publish-aria2.yml` 构建本项目适配的 Aria2 镜像，发布到 GHCR；对外提供下载前应确认包为公开状态，并验证无需登录即可拉取。

### 10.6 部署验收与发布

使用可控的小型 HTTP、磁力和多文件种子测试资源，验证私密/公开、14 天起算、文件选择、配额、上下行限速、做种策略、引擎/网盘分别重启、取消后不复活、保存中断恢复和暂存回收。网络限制使用专用测试服务验证，不探测真实内部服务。不得填满生产磁盘或删除既有用户文件来制造故障。

部署后仍需用户手动测试，再询问「本次修改是否通过，发布还是继续修改/新增功能」。只有明确决定发布后，才推送 GitHub、移动标签或创建/覆盖 Release。
