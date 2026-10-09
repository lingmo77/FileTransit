# 文件中转站

临时的文件中转站：访客无需登录即可浏览并下载公开文件；用户登录后可上传文件并设置有效期（最长 30 天）；到期文件由定时任务自动删除，磁盘空间随之回收。

- 后端：FastAPI + SQLAlchemy 2（异步）+ SQLite（WAL）
- 前端：Jinja2 服务端渲染 + 原生 CSS/JS，无 Node 构建步骤
- 部署：Docker / Docker Compose；开发环境用 Python 虚拟环境
- 默认管理员：`admin` / `admin`（首次登录后必须改密）

设计与需求细节见 [PROJECT.md](PROJECT.md)。

---

## 1. 快速开始（本地开发）

需要 **Python 3.12**。

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# Linux / macOS
python3.12 -m venv .venv
source .venv/bin/activate

pip install -r requirements-dev.txt   # 已包含 requirements.txt

# 可选：准备本地配置
cp .env.example .env

uvicorn app.main:app --reload --port 8000
```

打开 <http://127.0.0.1:8000>，用 `admin` / `admin` 登录后台。

数据库迁移、默认设置与默认管理员都在应用启动时自动完成（`app.core.bootstrap`），命令行不需要额外步骤。

### 运行测试

```bash
pytest              # 或 pytest -q
```

测试全部跑在系统临时目录下的独立数据目录里（用完即删），**不会**碰到 `data/` 下的真实数据，也不会改动真实的 `admin` 口令。测试期间限流默认关闭（所有请求都来自同一个「IP」，否则会互相干扰）。

---

## 2. Docker 部署

```bash
cp .env.example .env
# 必须修改 .env 里的 FT_SECRET_KEY：
#   python -c "import secrets; print(secrets.token_urlsafe(48))"

docker compose up -d --build
docker compose logs -f app
```

打开 `http://<服务器IP>:8000`。

数据（SQLite 数据库 + 上传的文件）全部在命名卷 `ft-data` 里，容器重建不丢数据：

```bash
docker compose down            # 保留数据
docker compose down -v         # 连数据一起删，谨慎使用
```

### 不用 Compose

```bash
docker build -t file-transfer:1.0 .

docker run -d --name file-transfer \
  -p 8000:8000 \
  -v ft-data:/app/data \
  -e FT_SECRET_KEY='<换成随机串>' \
  -e FT_ADMIN_PASSWORD='<换成强口令>' \
  -e FT_WORKERS=4 \
  file-transfer:1.0
```

### 放到 Nginx / Caddy 后面

容器只监听 HTTP，TLS 建议交给反向代理。反代需要注意三件事：

1. **关闭响应缓冲**，否则大文件下载会先落到代理磁盘、失去流式效果：
   Nginx 里设 `proxy_buffering off;`（或对大文件路径单独设）。
2. **放行 `Range` 与 `HEAD`**（默认就支持），并放宽请求体大小以匹配后台设置的单文件上限
   （默认 20480 MB）：`client_max_body_size 20480m;`
3. **设置 `FT_TRUST_PROXY=true`**，这样真实客户端 IP 才会写进日志、Cookie 的 `Secure`
   属性也才能正确判断。同时反向代理要转发 `X-Forwarded-For` / `X-Forwarded-Proto`。

参考片段：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_request_buffering off;
    client_max_body_size 20480m;
    proxy_read_timeout 3600s;
}
```

---

## 3. 配置

所有配置项都带 `FT_` 前缀，可用环境变量或项目根目录的 `.env` 覆盖，完整清单见 [.env.example](.env.example)。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FT_SECRET_KEY` | 开发用占位值 | 签名 Cookie、加密 SMTP 密码。**生产必须改** |
| `FT_DATA_DIR` | `./data` | SQLite 与文件本体所在目录 |
| `FT_WORKERS` | `4` | uvicorn 进程数 |
| `FT_COOKIE_SECURE` | `false` | 走 HTTPS 时设为 `true` |
| `FT_TRUST_PROXY` | `false` | 在反向代理后面时设为 `true` |
| `FT_MAX_UPLOAD_MB` | `20480` | 单文件上限的**播种默认值**，只在首次建库时写入；之后在后台「站点设置」里改 |
| `FT_ADMIN_USERNAME` / `FT_ADMIN_PASSWORD` | `admin` / `admin` | **仅**在数据库中还没有管理员时用于创建 |

站点层面的设置（站点名称、主题、注册开关、配额默认值、邮件服务器、日志保留策略等）在
**管理后台 → 站点设置 / 邮件设置** 里改，改完立即生效，无需重启。

---

## 4. 多线程下载（IDM / 迅雷）

下载直链对下载器是透明的，取文件详情页上的地址即可：

```
http://<站点>/download/<public_id>/<原文件名>
```

服务端为保证分片抓取做了这些事：

- 响应带 `Accept-Ranges: bytes`，完整支持 `bytes=a-b` / `bytes=a-` / `bytes=-n`，命中范围返回 `206` + `Content-Range`；
- 支持 `HEAD`（下载器先探测文件大小与是否支持断点）；
- 按请求独立打开文件句柄，不加进程内锁，连接数只受反向代理与系统 `ulimit` 限制；
- 响应不做 gzip，`Content-Length` 与实际字节数严格一致；
- 文件内容不可变，带长缓存 `Cache-Control` 与 `ETag`，重复下载走 304/本地缓存。

用 curl 自测：

```bash
URL=http://127.0.0.1:8000/download/<public_id>/demo.bin

curl -sI "$URL"                    # 看 Accept-Ranges 与 Content-Length
curl -s -r 0-1023 "$URL" -o p1
curl -s -r 1024-2047 "$URL" -o p2  # 两段拼起来应与原文件一致
```

下载次数只按「一次完整下载」记一次：非 0 起始的分片和 `HEAD` 探测都不计数，避免多线程把统计刷高。

---

## 5. 目录结构

```
app/
  main.py              应用装配、中间件、通用路由
  core/                配置、数据库、安全、CSRF、模板环境、启动引导
  models/              SQLAlchemy 模型（User / FileItem / Session / ActivityLog / Setting / TaskLock）
  routers/             页面路由、JSON 接口、下载路由、管理后台
  services/            业务逻辑（存储、配额、账号、邮件、日志、清理、统计）
  templates/           Jinja2 模板（含自包含的邮件模板）
  static/              CSS / JS / favicon
migrations/            Alembic 迁移脚本
tests/                 pytest 用例
docker/entrypoint.sh   容器启动脚本
data/                  运行时数据（数据库 + 上传文件，已被 .gitignore 忽略）
```

---

## 6. 安全说明

- **口令**：bcrypt（cost 12）哈希存储；会话令牌只存 SHA-256 摘要，数据库泄露也无法直接冒用。
- **CSRF**：双提交 Cookie（`ft_csrf` Cookie + `X-CSRF-Token` 头），所有写操作强制校验。
- **SMTP 密码**：用标准库实现（HKDF-SHA256 派生密钥 + HMAC-CTR 加密 + Encrypt-then-MAC），
  以密文存库，API 只返回 `email.has_password` 布尔值，任何接口都不会回显明文。
- **上传的 HTML/SVG/XML** 一律降级为 `application/octet-stream` + `attachment` 返回，
  避免在自己的域名下被浏览器渲染执行（存储型 XSS）。
- **限流**：登录、注册、重发验证邮件、上传按 IP/用户滑动窗口限流，可用
  `FT_RATE_LIMIT_ENABLED=false` 关闭（自动化测试就是这么做的）。
- **默认口令**：`admin/admin` 带着 `must_change_password` 标记，未改密前访问后台会被强制跳到个人设置页。上线前请即刻修改。

---

## 7. 常用运维命令

```bash
# 查看日志
docker compose logs -f --tail=200 app

# 备份数据卷
docker run --rm -v ft-data:/data -v "$PWD:/backup" alpine \
  tar czf /backup/ft-data-$(date +%F).tar.gz -C /data .

# 直接查数据库（日志也可以直接用 SQL 查，需求 4）
sqlite3 data/app.db "select created_at, username, action, ip_address, browser from activity_logs order by id desc limit 20;"

# 手动触发一次过期清理（也可在后台点「立即执行维护」）
docker compose exec app python -c "import asyncio; from app.services import cleanup; print(asyncio.run(cleanup.run_maintenance()))"
```
