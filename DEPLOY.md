# 文件中转站 · 部署实施说明

适用版本：**file-transfer 1.0**（2026-10-09）

本文档面向执行部署的人，从头到尾照着做即可。全流程只需要 Docker，不需要装 Python、Node 或数据库。

---

## 1. 部署前确认

### 1.1 目标机器要求

| 项 | 最低 | 建议 | 说明 |
| --- | --- | --- | --- |
| 操作系统 | Linux x86_64 | Ubuntu 22.04 / 24.04 | 镜像按 **amd64** 构建，ARM 机器需自行重新构建 |
| Docker | 20.10+ | 24+ | `docker --version` |
| Docker Compose | v2（`docker compose`） | v2.20+ | 注意是空格分隔的 `docker compose`，不是 `docker-compose` |
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
| `file-transfer-src-*.tar.gz` | ~135 KB | 纯源码 + 配置 + 本文档。目标机器需联网执行构建 |
| `file-transfer-offline-*.tar.gz` | ~68 MB | 源码 + **已构建好的镜像**。完全离线可部署 |

离线包里镜像本身就是 68 MB（`docker load` 后展开占用约 308 MB 磁盘）。这个 tar 已经压不动了——实测再套一层 gzip 只省 0.7%，所以包里放的是原始 tar，不是 `.tar.gz`。

**建议优先用离线包**：不依赖外网，不受目标机器代理/镜像源故障影响。之前有台机器就因为 dockerd 配了失效代理导致任何镜像都拉不动，离线包可以完全绕开这类问题。

---

## 2. 方式一：离线包部署（推荐）

### 2.1 上传并解包

把 `file-transfer-offline-*.tar.gz` 传到目标机器（`scp`、U 盘、内网共享均可），然后：

```bash
tar xzf file-transfer-offline-2026-10-09.tar.gz -C /opt
cd /opt/file-transfer
```

不需要 `--strip-components`：包里已经是一层 `file-transfer/` 目录。

### 2.2 导入镜像

```bash
docker load -i images/file-transfer-1.0.tar
```

预期输出 `Loaded image: file-transfer:1.0`。确认一下：

```bash
docker images file-transfer
```

> **关于镜像格式**：这个镜像由 Docker 29.4.3（containerd 镜像存储）导出，是 **OCI layout** 格式（包内是 `index.json` + `blobs/sha256/`），不是老的 `manifest.json` + `layer.tar` 结构。
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

**必须改两项**，其余可保持默认：

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

离线包场景**不要加 `--build`**（也没必要，镜像已导入）。等几秒后看状态：

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
tar xzf file-transfer-src-2026-10-09.tar.gz -C /opt
cd /opt/file-transfer
cp .env.example .env
# 同样要改 FT_SECRET_KEY 和 FT_ADMIN_PASSWORD，见 2.3

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

所有持久化数据都在一个 Docker 命名卷里：`<目录名>_ft-data`，目录名叫 `file-transfer` 时即 `file-transfer_ft-data`。里面是：

```
/app/data/app.db      # SQLite 数据库（用户、文件记录、日志、站点设置）
/app/data/storage/    # 用户上传的文件本体
```

查卷的实际路径：

```bash
docker volume inspect file-transfer_ft-data --format '{{.Mountpoint}}'
```

### 5.3 备份

停容器再备份最稳妥（SQLite 用 WAL 模式，热备份需额外处理）：

```bash
cd /opt/file-transfer
docker compose stop
docker run --rm \
  -v file-transfer_ft-data:/data \
  -v "$(pwd):/backup" \
  alpine tar czf /backup/ft-backup-$(date +%F).tar.gz -C /data .
docker compose start
```

恢复（会覆盖现有数据）：

```bash
cd /opt/file-transfer
docker compose stop
docker run --rm \
  -v file-transfer_ft-data:/data \
  -v "$(pwd):/backup" \
  alpine sh -c "rm -rf /data/* && tar xzf /backup/ft-backup-2026-10-09.tar.gz -C /data"
docker compose start
```

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
# 1) 先备份（见 5.3）
# 2) 用新包覆盖代码
tar xzf file-transfer-offline-新版.tar.gz -C /opt
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
- [ ] 已确认备份方式可用
