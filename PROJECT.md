# 文件中转站(File Transfer Station)项目设计文档

> 版本:v1.0(需求确认稿)
> 日期:2026-10-09
> 用途:临时文件中转/分享,支持多线程下载工具(IDM、迅雷等)高速下载

---

## 1. 项目概述

一个轻量级的**临时文件中转站**网站。游客无需登录即可浏览并下载站内所有用户最近上传的文件;注册用户登录后可上传文件并管理自己的分享;管理员通过后台对用户、文件、日志、邮件服务器、站点外观进行统一管理。

**核心定位**:部署简单(单机 Docker + SQLite)、下载快(支持 HTTP Range 多线程分片下载)、文件自动过期清理、前端简洁顺滑。

---

## 2. 技术选型

| 层面 | 选型 | 说明 |
| --- | --- | --- |
| 后端框架 | FastAPI | 异步、高性能、自带 OpenAPI 文档 |
| ASGI 服务器 | Uvicorn(多 worker)+ Gunicorn 可选 | 生产用 `uvicorn --workers N` 或 gunicorn+UvicornWorker |
| 数据库 | SQLite 3 | WAL 模式,单机读写性能足够;元数据与文件流分离 |
| ORM | SQLAlchemy 2.x(异步,`aiosqlite` 驱动) | 也可用 SQLModel,统一异步会话 |
| 数据库迁移 | Alembic | 便于后续表结构升级 |
| 模板引擎 | Jinja2 | 服务端渲染,首页首屏快、SEO 友好 |
| 前端 | 原生 HTML + CSS + Vanilla JS(无打包步骤) | 避免 Node 构建链,部署简单 |
| 样式 | 手写 CSS(设计令牌 + CSS 变量) | 主题切换、平滑过渡;不引重型 UI 框架 |
| 密码哈希 | bcrypt | 直接使用 `bcrypt` 库,避免 passlib 兼容坑 |
| 会话 | 签名 Cookie(`itsdangerous`)+ 服务端 Session 表 | 可强制下线、可审计 |
| 邮件 | 标准库 `smtplib` + `email.message.EmailMessage` | 支持 SSL/TLS/STARTTLS,无需额外依赖 |
| 定时任务 | APScheduler(或自实现 asyncio 后台循环) | 过期文件清理、临时数据清理 |
| 反向代理(可选) | Nginx | 提供 X-Accel-Redirect 零拷贝下载、静态资源缓存 |
| 部署 | Docker + Docker Compose | 单容器,数据卷持久化 |
| 测试 | pytest + httpx(AsyncClient) | 覆盖鉴权、配额、Range 下载等关键路径 |

> **版本策略**:`requirements.txt` 中所有依赖均写死版本(`==`),首次安装调试完成后以 `pip freeze` 结果为准锁定。文档第 11 节给出初版拟定清单。

---

## 3. 系统架构

```
                 ┌──────────────────────────────────────┐
   浏览器 / IDM  │        Nginx (可选,反向代理)         │
   迅雷 / curl   │  sendfile / X-Accel-Redirect / TLS   │
                 └───────────────┬──────────────────────┘
                                 │
                 ┌───────────────▼──────────────────────┐
                 │        FastAPI (Uvicorn, N workers)  │
                 ├──────────────────────────────────────┤
                 │  routers:  public / auth / user / admin
                 │  services: auth, upload, download, quota,
                 │            mailer, log, cleanup, settings
                 │  core:     config, security, db, deps
                 └──────┬─────────────────────┬───────────┘
                        │                     │
            ┌───────────▼─────────┐  ┌────────▼─────────────┐
            │  SQLite (WAL)       │  │  文件存储目录         │
            │  data/app.db        │  │  data/storage/xx/yy… │
            │  元数据 / 日志 / 配置│  │  二进制文件本体       │
            └─────────────────────┘  └──────────────────────┘
```

**设计原则**

1. **数据与文件分离**:SQLite 只存元数据与日志,文件字节直接由磁盘流式输出,数据库不成为下载瓶颈。
2. **无状态请求**:下载请求不依赖内存中的锁或全局状态,每个 Range 请求独立处理,天然支持多连接。
3. **配置集中于数据库**:站点设置、邮件设置存 `settings` 表,后台改动即时生效(内存缓存 + 失效刷新),无需重启。
4. **默认安全**:未登录只能读文件列表与下载,一切写操作(上传/删除)必须鉴权。

---

## 4. 功能需求拆解

### 4.1 公共区域(无需登录)

| 编号 | 功能 | 说明 |
| --- | --- | --- |
| P-1 | 首页文件墙 | 展示**所有用户最近上传**的未过期文件,按上传时间倒序分页(默认 24/页) |
| P-2 | 直接下载 | 点击文件名/下载按钮即开始下载,不跳转、不弹窗遮挡 |
| P-3 | 文件信息 | 文件名、大小、类型图标、上传者用户名、上传时间、剩余有效期、下载次数 |
| P-4 | 搜索与筛选 | 按文件名关键字搜索;按类型(图片/视频/文档/压缩包/其他)筛选 |
| P-5 | 单文件详情页 | `/f/{id}` 展示文件信息 + 下载按钮 + 二维码(可选) |
| P-6 | 主题切换 | 右上角日间/夜间切换,跟随站点默认设置,用户选择持久化 |
| P-7 | 登录/注册入口 | 顶部导航;若管理员关闭注册则不显示注册入口 |

> 未登录用户**可以下载**,这是本产品的核心体验;是否需要"下载前登录"由站点设置控制(默认关闭)。

### 4.2 用户区域(需登录)

| 编号 | 功能 | 说明 |
| --- | --- | --- |
| U-1 | 上传文件 | 支持拖拽、点击选择、多选;**大文件分片流式写入**,不整块载入内存 |
| U-2 | 上传进度 | 前端 XHR/fetch 上传进度条,支持取消;可并发上传多个文件 |
| U-3 | 我的文件 | 列表展示自己的文件,含大小、剩余时间、下载次数、分享链接 |
| U-4 | 删除文件 | 删除后磁盘文件与记录一并清理(软删除标记可选) |
| U-5 | 复制分享链接 | 一键复制公开下载直链(便于发到聊天工具) |
| U-6 | 有效期设置 | 上传时可选保留天数(1 ~ 站点上限,默认上限 30 天) |
| U-7 | 配额显示 | 显示已用文件数 / 已用空间 / 剩余额度,超额时明确报错 |
| U-8 | 个人设置 | 修改密码、修改邮箱(需重新验证)、查看登录记录 |

### 4.3 注册与邮箱验证

| 编号 | 功能 | 说明 |
| --- | --- | --- |
| A-1 | 注册开关 | 管理员可全局开启/关闭注册;关闭后注册接口返回 403,前端隐藏入口 |
| A-2 | 注册流程 | 用户名 + 邮箱 + 密码 → 创建未激活账号 → 发送验证邮件 → 点击链接激活 → 可登录 |
| A-3 | 验证链接 | 一次性 Token(存库、带过期时间,默认 30 分钟),使用后立即失效 |
| A-4 | 邮件内容 | 站点名称 + 验证链接 + 过期说明;HTML + 纯文本双版本 |
| A-5 | 重发验证 | 限流(同邮箱 60 秒 1 次,每日上限可配) |
| A-6 | 未验证限制 | 未验证邮箱的账号**不能登录**,提示"请先完成邮箱验证" |
| A-7 | 邮件未配置 | 若 SMTP 未配置,注册开关自动置灰并提示管理员先配置邮件服务器 |
| A-8 | 密码强度 | 最少 8 位,包含字母与数字(策略可在设置中放宽/收紧) |

### 4.4 文件生命周期与配额

| 编号 | 功能 | 说明 |
| --- | --- | --- |
| L-1 | 默认保留期 | 默认 **30 天**,单文件在创建时确定 `expires_at` |
| L-2 | 自动清理 | 后台定时任务(默认每 10 分钟)扫描过期文件,删除磁盘文件与数据库记录,并回收用户已用配额 |
| L-3 | 默认配额 | 每用户 **最多同时分享 10 个未过期文件**,**总大小不超过 10 GB** |
| L-4 | 配额可调 | 管理员可在后台对**单个用户**覆盖配额(文件数、总字节数),也可设置全局默认值与"无限制" |
| L-5 | 上传前校验 | 先校验配额再落盘;分片上传过程中超限则中止并回滚删除已写入数据 |
| L-6 | 配额统计口径 | 只统计**未过期**文件;过期后即使未被清理任务删除也不计入用户配额 |
| L-7 | 剩余时间展示 | 前端展示"剩余 x 天 y 小时",临近过期(<24h)高亮提示 |

### 4.5 管理员后台

| 编号 | 模块 | 功能 |
| --- | --- | --- |
| G-1 | 仪表盘 | 站点统计:用户数、文件数、总占用空间、今日上传/下载次数、近 7 日趋势图(纯 CSS/SVG,不引图表库或使用轻量内联 SVG) |
| G-2 | 用户管理 | 列表/搜索/分页;编辑(改密、改邮箱、启用禁用、设为管理员);单独调整配额;**手动创建用户**;删除用户(连带文件) |
| G-3 | 文件管理 | 全站文件列表,按用户/时间/大小筛选;强制删除;手动调整到期时间 |
| G-4 | 邮件服务器配置 | SMTP 主机、端口、加密方式(无/SSL/STARTTLS)、用户名、密码(加密存储,界面脱敏)、发件人名称与地址;**发送测试邮件**按钮 |
| G-5 | 注册开关 | 一键开启/关闭用户注册 |
| G-6 | 站点设置 | 网站名称、站点副标题/公告、Logo、页脚、默认主题(日间/夜间/跟随系统)、默认保留天数、默认配额、单文件最大体积、是否允许游客下载 |
| G-7 | 日志查看 | 按用户、动作类型、时间范围、IP 筛选;分页浏览;详情展开(含 User-Agent 解析出的浏览器/OS) |
| G-8 | 日志清理 | **一键清空**,支持可选时间范围(如"清空 30 天前"或"清空某时间区间"),清理后可选执行 `VACUUM` 回收 SQLite 磁盘空间 |
| G-9 | 日志导出 | 导出 CSV/JSON(可选,便于外部分析) |
| G-10 | 审计保护 | 管理员自身的关键操作(删除用户、清空日志、改配置)同样写入日志 |

#### 4.5.1 日志记录内容(写入数据库 `activity_logs` 表)

| 字段 | 内容 |
| --- | --- |
| `user_id` / `username` | 操作者(未登录时为 NULL / `anonymous`) |
| `action` | 动作枚举,见下表 |
| `ip_address` | 客户端真实 IP(支持 `X-Forwarded-For`,由设置中的"信任代理"开关控制) |
| `user_agent` | 原始 UA 字符串 |
| `browser` / `os` / `device` | 由 UA 解析出的浏览器名+版本、操作系统、设备类型 |
| `method` / `path` | HTTP 方法与请求路径 |
| `target_type` / `target_id` | 目标对象(如 `file:123`、`user:5`) |
| `detail` | JSON 扩展信息:上传时的**原始文件名、大小、MIME**;下载时的文件 id;失败原因等 |
| `status` | success / failure |
| `created_at` | 时间(UTC 存储,前端按本地时区显示) |

**需要记录的动作**:`register` `email_verified` `login` `login_failed` `logout` `password_change` `upload` `download` `delete_file` `expire_cleanup`,以及管理员的 `admin_user_update` `admin_delete_file` `admin_settings_update` `admin_logs_purge` `admin_smtp_test` 等。

> 说明:游客下载也会记录(可用于统计),但为控制表膨胀,可在设置中关闭"记录匿名下载日志"。

---

## 5. 数据库设计(SQLite)

### 5.1 表结构

**users**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | |
| username | TEXT UNIQUE | 登录名 |
| email | TEXT UNIQUE | 邮箱 |
| password_hash | TEXT | bcrypt |
| is_admin | BOOLEAN | 默认 0 |
| is_active | BOOLEAN | 被禁用则不能登录 |
| email_verified | BOOLEAN | 默认 0 |
| quota_max_files | INTEGER NULL | NULL = 用全局默认 |
| quota_max_bytes | INTEGER NULL | NULL = 用全局默认;-1 = 无限制 |
| used_files | INTEGER | 冗余计数(便于快速校验) |
| used_bytes | INTEGER | 冗余计数 |
| created_at / last_login_at | DATETIME | |
| last_login_ip | TEXT | |

**files**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | 对外可用 `id` + 短随机串拼成下载码 |
| user_id | INTEGER FK | |
| original_name | TEXT | 原始文件名(下载时还原) |
| stored_path | TEXT | 相对存储路径,如 `ab/cd/<uuid>` |
| size | INTEGER | 字节 |
| mime_type | TEXT | 由扩展名/`python-magic` 推断 |
| sha256 | TEXT | 可选,便于秒传/查重(后续扩展) |
| download_count | INTEGER | |
| is_public | BOOLEAN | 是否为公开分享(默认 1) |
| created_at | DATETIME | |
| expires_at | DATETIME | 索引,清理任务扫描用 |
| deleted_at | DATETIME NULL | 软删除标记 |

**activity_logs**(见 4.5.1)

**settings**(K-V)

| 字段 | 说明 |
| --- | --- |
| key | TEXT PK,如 `site.name`、`site.default_theme`、`registration.enabled`、`email.smtp_host`、`quota.default_max_files` |
| value | TEXT(JSON 序列化) |
| updated_at / updated_by | |

**sessions**(可强制下线)

| 字段 | 说明 |
| --- | --- |
| id / user_id / token_hash / ip / user_agent / created_at / last_seen_at / expires_at / revoked |

**email_tokens**

| 字段 | 说明 |
| --- | --- |
| id / user_id / token_hash / purpose(verify_email / reset_password) / expires_at / used_at |

**索引**:`files(expires_at)`、`files(created_at DESC)`、`files(user_id, expires_at)`、`activity_logs(created_at)`、`activity_logs(user_id)`、`activity_logs(action)`。

### 5.2 SQLite 并发配置

```sql
PRAGMA journal_mode = WAL;      -- 读写并发
PRAGMA synchronous = NORMAL;    -- 性能与安全平衡
PRAGMA busy_timeout = 5000;     -- 写锁等待,避免 database is locked
PRAGMA foreign_keys = ON;
PRAGMA temp_store = MEMORY;
```

- 每个请求/每个 worker 独立连接;写操作集中在短事务中,避免长时间持有写锁。
- 关键更新(配额增减)使用 `UPDATE ... SET used_bytes = used_bytes + ?` 原子语句或事务 + `SELECT ... FOR UPDATE` 语义(用 `BEGIN IMMEDIATE`)。
- 若部署为多 worker 多进程,SQLite 依然可用(WAL + busy_timeout);若未来写入压力大,可平滑切换 PostgreSQL(SQLAlchemy 层不写死方言)。

---

## 6. 接口设计(主要路由)

### 6.1 页面路由(服务端渲染)

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 首页文件墙(分页、搜索、筛选) |
| GET | `/f/{file_id}` | 文件详情页 |
| GET | `/login` `/register` `/verify` | 登录/注册/验证结果页 |
| GET | `/dashboard` | 我的文件(需登录) |
| GET | `/me/settings` | 个人设置(需登录) |
| GET | `/admin` `/admin/users` `/admin/files` `/admin/logs` `/admin/settings` `/admin/email` | 管理后台(需管理员) |

### 6.2 数据接口(JSON)

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/register` | 注册,触发验证邮件 |
| POST | `/api/auth/login` | 登录,写 Cookie + 日志 |
| POST | `/api/auth/logout` | 登出 |
| GET | `/api/auth/verify?token=...` | 邮箱验证 |
| POST | `/api/auth/resend-verification` | 重发验证邮件(限流) |
| GET | `/api/files` | 公开文件列表(分页/搜索/筛选) |
| POST | `/api/files` | 上传文件(需登录,multipart 流式) |
| DELETE | `/api/files/{id}` | 删除自己的文件 |
| PATCH | `/api/files/{id}` | 修改有效期/公开状态 |
| GET | `/api/me/quota` | 查询配额使用情况 |
| GET | `/admin/api/logs` | 日志查询 |
| DELETE | `/admin/api/logs` | 清空日志(`?before=ISO时间` 可选范围) |
| POST | `/admin/api/settings` | 更新站点设置 |
| POST | `/admin/api/email/test` | 发送测试邮件 |
| GET | `/admin/api/stats` | 仪表盘统计 |
| GET | `/healthz` | 健康检查(Docker healthcheck 用) |

### 6.3 下载接口(多线程下载核心)

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/HEAD | `/d/{file_id}` | 不带文件名,`Content-Disposition` 中给出原始名 |
| GET/HEAD | `/download/{file_id}/{filename}` | **推荐分享直链**,URL 中带原始文件名,IDM/迅雷 能正确识别 |

**必须满足的 HTTP 语义:**

```
HTTP/1.1 200 OK
Accept-Ranges: bytes
Content-Length: <size>
Content-Type: application/octet-stream  (或真实 MIME)
Content-Disposition: attachment; filename="a.zip"; filename*=UTF-8''%E4%B8%AD%E6%96%87.zip
ETag: "<file_id>-<mtime>-<size>"
Last-Modified: <mtime>
Cache-Control: public, max-age=31536000, immutable   (文件内容不可变)
```

```
Range: bytes=0-1048575    →  206 Partial Content
Content-Range: bytes 0-1048575/123456789
```

**实现要点(保证 IDM / 迅雷 多线程高速下载):**

1. **完整的 Range 支持**:单区间 `bytes=a-b`、`bytes=a-`、`bytes=-n`;多个区间可选支持(返回 `multipart/byteranges`,非必需,主流下载器都先并发发单区间请求)。
2. 正确处理 `416 Range Not Satisfiable`(带 `Content-Range: bytes */<size>`)。
3. 支持 `If-Range`(配合 ETag/Last-Modified),避免文件被替换后拼接出损坏数据。
4. 必须支持 **HEAD** 请求(下载器先探测大小与是否支持断点续传)。
5. 每次请求独立打开文件句柄,**不加任何进程内锁**,不限制单文件并发连接数(连接数限制交给 Nginx / 系统 ulimit)。
6. 使用**流式响应**(`StreamingResponse` / `FileResponse`),分块大小 64KB ~ 1MB;对二进制响应**禁止 gzip 压缩**(避免 `Content-Length` 变化导致下载器出错)。
7. 若使用 Nginx:开启 `sendfile on`、`proxy_buffering off`,并优先走 `X-Accel-Redirect` 由 Nginx 直接发送文件(零拷贝,后端只做鉴权与计数),这是大文件高并发的最佳实践。
8. Range 切片由后端计算,支持 `Starlette FileResponse` 内建 Range(Starlette ≥ 0.24);开发时**实际验证 206 行为与多区间处理**,若内建行为不满足再实现自定义 Range 响应。
9. 下载计数使用原子 `UPDATE files SET download_count = download_count + 1`,不阻塞响应;`206` 分片请求只在首个区间(或首字节为 0 时)计数,避免一次下载被计成几十次。

---

## 7. 关键技术方案

### 7.1 上传(大文件、流式)

- 接收 `UploadFile`,**异步分块读取**(如 1MB/块)写入 `data/tmp/<uuid>.part`,同时累加字节数。
- 一旦超过"单文件上限"或"用户剩余配额",立即中止、删除临时文件并返回 413/403。
- 上传完成后 `os.replace()` 原子移动到最终路径(`data/storage/<前2位>/<后2位>/<uuid>`),再写数据库记录,避免出现"有文件无记录"。
- 请求体大小在上限处**提前拒绝**:检查 `Content-Length`;同时依赖 Nginx `client_max_body_size` 兜底。
- 支持前端**分片上传**(可选进阶):大文件切片 + 断点续传,提升弱网体验;若首版不做,则用单请求流式 + 进度条。

### 7.2 并发与性能

- Uvicorn 多 worker(`WORKERS` 环境变量,默认 `2 × CPU + 1` 的保守值,如 4)。
- 所有数据库访问异步化(`aiosqlite`),下载/上传的阻塞式文件 IO 放到线程池(`anyio.to_thread` / `run_in_threadpool`),避免阻塞事件循环。
- 静态资源(本地 CSS/JS、图标)由 FastAPI `StaticFiles` 提供,并设置长缓存。
- 首页列表接口只查必要字段,分页 + 索引,单页查询 < 10ms 量级。
- 文件存储按哈希前缀分目录,避免单目录文件过多导致文件系统性能下降。

### 7.3 安全

| 项 | 措施 |
| --- | --- |
| 密码 | bcrypt,成本因子 12 |
| 会话 | HttpOnly + SameSite=Lax Cookie;HTTPS 下 Secure;会话表可撤销;登录后轮换 token |
| CSRF | 所有写操作校验 CSRF Token(表单 / `X-CSRF-Token` 头) |
| XSS | Jinja2 默认自动转义;文件名等用户内容渲染时显式转义 |
| 路径穿越 | 存储路径完全由服务端生成,只按 `file_id` 查库取路径,绝不拼接用户输入 |
| 上传校验 | 限制扩展名黑名单(如 `.php` `.exe` 可配置)、MIME 校验、文件名长度与非法字符清洗 |
| 限流 | 登录/注册/重发邮件/上传 基于 IP + 账号的滑动窗口限流(内存或 SQLite 计数) |
| 响应头 | `X-Content-Type-Options: nosniff`、`X-Frame-Options: SAMEORIGIN`、CSP、Referrer-Policy |
| 管理员 | 默认 `admin/admin`,**首次登录强制修改密码**并给出明显告警 |
| 密钥 | `SECRET_KEY` 由环境变量提供,`.env` 不提交仓库;SMTP 密码加密存储(用 SECRET_KEY 派生密钥) |

### 7.4 前端设计与交互

- **风格**:极简、留白充足、卡片式文件列表;主色由站点设置驱动(CSS 变量 `--primary`)。
- **主题**:`data-theme="light|dark"` 挂在 `<html>` 上,全部颜色走 CSS 变量,切换时对 `color/background/border` 加 `transition: .25s ease` 实现平滑过渡;首帧前用内联脚本读取 localStorage 防止"白闪"(FOUC)。
- **动画**:卡片悬浮轻微上浮 + 阴影加深、按钮按压缩放、上传进度条平滑增长、列表加载骨架屏(skeleton)、Toast 轻提示;统一使用 `cubic-bezier(.2,.8,.2,1)` 缓动;并遵循 `prefers-reduced-motion` 关闭动效。
- **响应式**:移动端优先的栅格,断点 640 / 1024 / 1280px。
- **无构建**:CSS/JS 直接由服务端托管,改样式不需编译;图标使用内联 SVG sprite。
- **可达性**:键盘可操作、表单有 label、颜色对比度 ≥ 4.5:1、焦点样式可见。

### 7.5 定时任务

| 任务 | 频率 | 内容 |
| --- | --- | --- |
| 过期文件清理 | 每 10 分钟 | 删除 `expires_at < now()` 的文件与记录,回收用户配额 |
| 临时文件清理 | 每小时 | 清理 `data/tmp` 中超过 24h 的残留分片 |
| 会话清理 | 每天 | 删除过期 session 与过期 email_token |
| 日志容量保护 | 每天 | 若 `activity_logs` 超过设定条数(默认 50 万),自动删除最旧记录并提示管理员 |

> 多 worker 下定时任务会重复触发:通过 SQLite 的 `BEGIN IMMEDIATE` + 轻量锁表(或只在 0 号 worker 启动任务)保证只执行一次。

---

## 8. 目录结构

```
file-transfer/
├─ app/
│  ├─ main.py                  # FastAPI 应用装配、生命周期、中间件
│  ├─ core/
│  │  ├─ config.py             # 环境变量配置(Pydantic Settings)
│  │  ├─ database.py           # 异步引擎、会话、PRAGMA 初始化
│  │  ├─ security.py           # 密码哈希、会话、CSRF、限流
│  │  └─ logging.py            # 应用日志
│  ├─ models/                  # SQLAlchemy 模型(user/file/log/setting/...)
│  ├─ schemas/                 # Pydantic 请求/响应模型
│  ├─ routers/
│  │  ├─ pages.py              # 页面路由
│  │  ├─ auth.py               # 注册/登录/验证
│  │  ├─ files.py              # 列表/上传/删除/下载(含 Range)
│  │  ├─ download.py           # 下载专用路由 /d /download
│  │  └─ admin.py              # 后台数据接口
│  ├─ services/
│  │  ├─ storage.py            # 落盘、分片路径、删除、磁盘统计
│  │  ├─ quota.py              # 配额校验与增减
│  │  ├─ mailer.py             # SMTP 发送、模板渲染、测试邮件
│  │  ├─ activity.py           # 日志写入与 UA 解析
│  │  ├─ settings.py           # 站点设置读写 + 内存缓存
│  │  └─ cleanup.py            # 定时清理任务
│  ├─ templates/               # Jinja2: base / index / login / dashboard / admin/*
│  └─ static/                  # css / js / icons / logo
├─ migrations/                 # Alembic
├─ data/                       # 运行时数据(挂载卷)
│  ├─ app.db
│  ├─ storage/
│  └─ tmp/
├─ tests/                      # pytest
├─ .env.example
├─ requirements.txt            # 锁定版本
├─ Dockerfile
├─ docker-compose.yml
├─ nginx.conf.example          # 可选反向代理示例
├─ .dockerignore
├─ .gitignore
└─ README.md                   # 部署与使用说明
```

---

## 9. 部署方案

### 9.1 虚拟环境(本地开发)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### 9.2 Docker 部署

- 基础镜像:`python:3.12-slim`(稳定、wheel 覆盖好;本地 Python 3.14 亦可运行,容器内固定 3.12 保证依赖可复现)
- 多阶段构建:builder 装依赖到 venv → runtime 只拷贝 venv 与代码,镜像更小
- 非 root 用户运行
- 数据卷:`/app/data`(SQLite + 文件),必须持久化
- 环境变量:`SECRET_KEY` `DATA_DIR` `WORKERS` `MAX_UPLOAD_MB` `TRUST_PROXY`
- `HEALTHCHECK` 打 `/healthz`
- `docker-compose.yml` 提供一键启动 + 卷映射 + 端口映射 + 重启策略

```bash
docker compose up -d --build
# 访问 http://localhost:8000  默认管理员 admin / admin
```

- 首次启动自动建库(Alembic 迁移或 `create_all`)、创建默认管理员、写入默认设置。
- 生产建议前置 Nginx:HTTPS、`client_max_body_size 0`(交给后端限额)、`X-Accel-Redirect` 加速下载。

---

## 10. 开发里程碑

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| M1 骨架 | 项目结构、配置、数据库、Alembic、虚拟环境、requirements 锁定 | 可启动的空应用 + `/healthz` |
| M2 认证 | 用户模型、注册/登录/登出、会话、CSRF、邮箱验证、默认 admin | 可注册登录 |
| M3 上传下载 | 存储层、配额校验、上传页、文件列表、**Range 多线程下载** | 端到端上传下载可用 |
| M4 后台 | 用户/文件管理、站点设置、邮件配置、日志查看与清理、仪表盘 | 管理功能完整 |
| M5 前端打磨 | 主题切换、动画、响应式、骨架屏、错误提示、空状态 | 观感与体验达标 |
| M6 测试与容器化 | pytest 覆盖关键路径、并发压测(IDM/curl 多线程验证)、Dockerfile/Compose 联调 | 可交付部署包 + README |

**验收要点**

1. `curl -r 0-1023`、`curl -r 1024-2047` 对同一文件并发请求,返回 206 且字节正确拼接后校验 SHA256 一致。
2. IDM / 迅雷 添加直链后能识别文件大小并启用多线程下载。
3. 超过 10 个文件 / 超过 10GB 时上传被拒并给出明确提示;管理员调整配额后立即生效。
4. 过期文件在 10 分钟内被清理,磁盘空间回落,用户配额恢复。
5. 日志可查、可按时间范围清空,清空后 `app.db` 文件体积因 `VACUUM` 减小。
6. 关闭注册开关后,注册页与接口同时失效;SMTP 未配置时给出明确引导。
7. 主题切换平滑无闪烁,移动端布局正常。

---

## 11. requirements.txt(已锁定)

运行时依赖（`requirements.txt`，全部精确到版本）：

```
fastapi==0.143.0
starlette==1.7.0          # 直接锁定：FileResponse 的 Range 实现决定多线程下载行为
uvicorn[standard]==0.54.0
Jinja2==3.1.6
pydantic==2.14.0
pydantic-settings==2.15.0
python-dotenv==1.2.4
SQLAlchemy[asyncio]==2.1.4
aiosqlite==0.22.1
greenlet==3.5.6           # SQLAlchemy 异步的必需依赖
alembic==1.20.0
python-multipart==0.0.32
bcrypt==5.0.0
APScheduler==3.11.3
tzlocal==5.4.4
tzdata==2026.5            # 容器里没有系统 tzdata
```

开发/测试依赖（`requirements-dev.txt`，`-r requirements.txt` 之上追加）：

```
pytest==9.1.1
pytest-asyncio==1.4.0
httpx==0.28.1             # 通过 ASGITransport 直接打应用，无需起服务器
```

> 与原拟定版本的差异：去掉了 `gunicorn`（uvicorn 自带 `--workers` 多进程）、
> `itsdangerous`（签名改用标准库 `hmac`/`hashlib`）、`qrcode`、`user-agents`
> （UA 解析自写正则，少一个依赖）、`python-dateutil`（标准库 `datetime` 足够）。
> 实际安装版本见交付时的 `pip freeze`。

---

## 12. 需求确认结论(2026-10-09 已确认)

| # | 事项 | 结论 |
| --- | --- | --- |
| 1 | 游客下载 | ✅ **无需登录即可直接下载** |
| 2 | 首页展示范围 | ✅ **默认公开全部文件**;用户上传时可选"不公开(仅自己可见)" |
| 3 | 提取码 | ❌ **不需要** |
| 4 | 有效期 | ✅ 用户上传时可自选到期时间,**上限 30 天**,默认 3 天;到期自动删除。管理员可额外设为「长期有效」(见 §13) |
| 5 | 配额口径 | ✅ **同时存在**的未过期文件:≤ 10 个 且 合计 ≤ 10GB;删除或过期即释放 |
| 6 | Python 版本 | ✅ **固定 Python 3.12**(容器与开发环境一致) |
| 7 | Docker | ⚠️ 开发机(Windows)无 Docker;代码完成后由用户提供带 Docker 的 Ubuntu SSH 环境进行打包部署验证 |
| 8 | 邮箱验证 | 强制(未验证不可登录);管理员后台可关闭注册 |
| 9 | 前端技术 | Jinja2 + 原生 CSS/JS,无 Node 构建 |
| 10 | 下载限速 | 不限速(最大化 IDM/迅雷 多线程速度) |

---

## 13. 实施顺序

M1 骨架 → M2 认证 → M3 上传下载 → M4 后台 → M5 前端打磨 → M6 测试与容器化。
详见第 10 节里程碑。

---

## 14. 交付状态(2026-10-09)

M1 ~ M5 已完成，M6 的测试与容器化产物均已就绪。

**代码结构**

```
app/main.py              应用装配、CSRF 中间件、/healthz、静态资源挂载
app/core/                config / database / security / crypto / deps / templating / bootstrap
app/models/              User、UserSession、EmailToken、FileItem、ActivityLog、Setting、TaskLock
app/routers/             pages、api_auth、files、download、admin、admin_pages
app/services/            storage、quota、auth、account、mailer、files、users、logs、cleanup、stats、activity、pagination
app/templates/           页面模板 + 自包含邮件模板（verify / reset / test）
app/static/              style.css、upload.js、admin.js、favicon.svg
migrations/versions/     e97a949478ec 初始迁移（6 张业务表 + 索引）
docker/entrypoint.sh     环境变量 → uvicorn 参数
tests/                   conftest + test_auth / test_files / test_admin / test_cleanup / test_security
Dockerfile               python:3.12-slim 两阶段构建，非 root，HEALTHCHECK
docker-compose.yml       命名卷持久化 + 健康检查 + 日志轮转
```

**测试结果**：`pytest` → **54 passed**（执行顺序无关，连跑多次结果一致）。覆盖：

| 模块 | 用例数 | 覆盖内容 |
| --- | --- | --- |
| test_auth | 7 | 默认 admin 登录、错误口令、CSRF 缺失/伪造拦截、注册与邮箱验证门槛、登出、改密并踢掉其它设备 |
| test_files | 14 | 游客浏览+下载、上传需登录、有效期钳制到 30 天、Range/206/416/HEAD/友好直链、HTML 强制下载、下载计数不被分片刷高、按文件数与字节的配额（含拒绝后不留孤儿文件）、可见性与越权删除、管理员设「长期有效」（含首页可见 / 可下载 / 详情页徽标）、普通用户伪造 `expires_hours=0` 被 400 拦下、后台把已有文件改成长期有效 |
| test_admin | 17 | 游客/普通用户 403 门禁、站点设置生效与恢复、未知键与空请求拒绝、SMTP 密码不回显（含落库密文与派生字段只读）、用户 CRUD、禁用账号无法登录、不能删除自己/最后一名管理员、删用户连带清文件、日志含 IP/浏览器/上传详情、筛选与 CSV 导出、按时间范围清空日志、清空覆盖站点时区当天全天、日期→UTC 换算、仪表盘统计、手动维护、管理员下架文件 |
| test_cleanup | 6 | 过期文件从磁盘与数据库删除并回退配额、未过期文件不受影响、只回收超过 24 小时的临时分片（不误删进行中的上传）、任务锁使并发维护互斥、长期有效文件不被清理且重算用量时不丢配额、过期私有文件同样被清理 |
| test_security | 10 | bcrypt 加盐与 72 字节截断、会话令牌随机性、限流滑动窗口/过期/重置/可关闭 |

**额外验证**（真机 `uvicorn`，非 ASGI 直连）

- 300KB 随机文件切成 4 段并发下载：每段均为 `206` 且 `Content-Range` 正确，四段拼接后的 SHA-256 与原文件一致；`HEAD` 返回 `Accept-Ranges: bytes` 与正确 `Content-Length`；越界 Range 返回 `416`。
- `--workers 4` 冷启动（空数据库）：只跑了一次迁移、只创建了一个默认管理员、25 项默认设置齐全，日志无 `database is locked` 或异常。

**本轮修复的问题**

1. 多 worker 同时启动并发跑 Alembic 迁移会撞 `database is locked` → `bootstrap()` 增加基于 `O_CREAT|O_EXCL` 的跨进程启动锁（带陈旧锁接管与超时兜底）。
2. 限流的阈值写死在代码里，测试无法放开 → 改为 `FT_RATE_LIMIT_*` 配置项，并新增 `FT_RATE_LIMIT_ENABLED` 开关。
3. `starlette.status.HTTP_413_REQUEST_ENTITY_TOO_LARGE` 已废弃 → 换成 `HTTP_413_CONTENT_TOO_LARGE`。
4. 后台保存站点设置时，请求体里的 `email.has_password` 会被当成真实设置项写进 `settings` 表 → 改为只读派生字段，不再落库。

**环境说明**：开发机只装了 Python 3.14，虚拟环境基于 3.14；代码本身不含 3.12 以上才有的语法，
容器内固定使用 `python:3.12-slim`（需求 6 / 12-6）。

**部署联调（2026-10-09 完成）**

目标机 `docker@192.168.88.10`（Ubuntu 24.04.4 / Docker 29.4.3 / Compose v5.1.3 / 6 核 / 1.9G 内存）。

- 代码部署在 `/home/docker/file-transfer`，镜像 `file-transfer:1.0`（308MB），容器 `file-transfer` 常驻 `0.0.0.0:8000`，状态 `healthy`。
- 命名卷 `file-transfer_ft-data` 持久化 SQLite 与上传文件；实测 `--force-recreate` 重建后数据完好。
- `.env`（`chmod 600`）中 `FT_SECRET_KEY` 为 48 字符随机值；因该值曾在 `docker compose config` 输出中出现，部署后已重新生成并重建容器，再验一遍全绿。
- 因目标机内存仅 1.9G，`FT_WORKERS` 定为 **2**（非镜像默认的 4）；空手启动，两个 worker 中只有一个执行了 `Running upgrade -> e97a949478ec`，另一个只打印 Alembic 上下文——**跨进程启动锁在生产环境同样生效**。
- 端到端验收（从开发机直连 `http://192.168.88.10:8000`）**21/21 通过**：健康检查、游客浏览与免登录下载、`admin/admin` 登录与错误口令拒绝、上传、4 段并发 Range 下载拼接后 SHA-256 一致、后缀 Range、越界 416、HTML 强制附件下载、未登录上传 401、仪表盘统计、日志含 IP/浏览器/上传详情、CSV 导出、删除后直链失效。

**部署阶段修复的问题**

5. Dockerfile 的 `# syntax=docker/dockerfile:1` 会强制拉取 BuildKit 前端镜像，在受限网络下成为失败点 → 本文件只用标准指令，去掉该行。
6. 目标机 `/etc/systemd/system/docker.service.d/http-proxy.conf` 把所有拉取指向已关闭的 Windows 代理，导致任何镜像都拉不动 → 改名为 `http-proxy.conf.off` 停用（未删除，可随时改回），`daemon-reload` + 重启 docker，原有容器自动恢复。构建随即成功。
7. 深色主题下 `<input type="date">` 的日历图标几乎看不见（用户反馈"操作日志选不了年月日"）→ 根因是 `color-scheme` 仅由 `<meta name="color-scheme" content="light dark">` 声明，它跟随**操作系统**偏好，而站点主题由 `data-theme` 决定；两者不一致时（例如系统浅色、后台把默认主题设为深色）浏览器按错误配色绘制原生控件，深灰图标落在 `#16191e` 的近黑背景上 → 在 `:root` 与 `[data-theme='dark']` 上显式声明 `color-scheme`，并放大日历图标热区、补悬停反馈；`style.css?v=1` 一并升到 `?v=2` 破浏览器缓存。

**已知待办**：管理员口令已由用户自行改掉（不再是 `admin/admin`）；未配置 HTTPS，生产使用建议前置 Nginx 并开启 TLS。

---

## 15. 第二轮调整(2026-10-09)

### 15.1 需求变更

用户在验收后提出 7 项调整,其中 2 项经排查确认「原本就是现状」,无需改动:

| # | 诉求 | 处理 |
| --- | --- | --- |
| 1 | 填了年月日也清不掉日志 | ✅ **根因是时区**,已修(见 15.2) |
| 2 | `FT_MAX_UPLOAD_MB` 默认改成 20GB,实际大小由管理员在后台设定 | ✅ 播种默认值改 20480MB;并澄清它**不是硬上限**(见 15.3) |
| 3 | 默认保留时长改为 3 天 | ✅ 上传表单与接口默认值 `Form(72)` |
| 4 | 每个上传的文件默认就是分享的 | ⚠️ **原本如此** (`FileItem.is_public` 默认 `True`),补测试固化 |
| 5 | 即使不公开分享,到期也会删除 | ⚠️ **原本如此** (清理查询不区分公开/私有),补测试固化 |
| 6 | 最长保留 30 天 | ⚠️ 原本如此 (`quota.max_expire_days = 30`) |
| 7 | 管理员可在后台设为「长期有效」,普通用户不能 | ✅ 新增(见 15.4) |

### 15.2 清空日志失效:时区

**根因**:项目在**显示**方向一直是对的(`site.timezone` 默认 `Asia/Shanghai`,`fmt_dt` 把 UTC 正确渲染成北京时间),错的是**反方向**——`services/logs.py` 的 `parse_date` 把用户填的日期**按 UTC 解释**再与 UTC 存储的 `created_at` 比较。UTC+8 的用户填「10 月 9 日」,实际比较的是北京 10-09 08:00,边界整体偏 8 小时。叠加两处:清空面板的 `before` 漏了 `end_of_day`,导致「填今天」最多删到今天零点前;筛选与清空一个用 `<=` 一个用 `<`。

**修复**:新增 `app/core/timeutil.py`(`site_zone` / `local_to_utc` / `to_local`,纯标准库,避免服务层反向依赖 Jinja2);`parse_date` 增加 `tz_name` 参数,日期串先按站点时区补 tzinfo 再换算 UTC,带偏移的 ISO 串改用 `astimezone` 而不是丢弃偏移;四个调用方(`admin_pages.py`、`admin.py` 的列表/清空/CSV)统一传入 `site.timezone`;清空面板的 `before` 启用 `end_of_day=True`(文案同步改为「清空此日期(含当天)及更早的日志」)。

顺带修掉:清空成功后 `form.submit()` 会立刻刷掉「已清理 N 条」的 toast → 延时 1.2 秒再提交。

### 15.3 `FT_MAX_UPLOAD_MB` 的真实语义

它**不是硬上限**,而是 `bootstrap._seed_settings` 写入 `settings` 表的**播种默认值**,只补缺失键。上传时实际读的是后台设置 `quota.max_upload_mb`。后台那句「同时受环境变量 FT_MAX_UPLOAD_MB 限制」是错误文案,已改。

⚠️ **已建好的库不会因改环境变量而变化**:目标机 `settings` 表里仍是 `quota.max_upload_mb = 4096`,需在后台「站点设置」手动改成需要的值。

### 15.4 管理员「长期有效」

用哨兵值 `expires_hours = 0` 表示长期有效,库里存 `expires_at = NULL`。

- **迁移** `b3f1c2a45d7e`:SQLite 不支持 `ALTER COLUMN`,用 `batch_alter_table(recreate="always")` 重建 `files` 表把 `expires_at` 改为 nullable。已实测:PK、`ON DELETE CASCADE` 外键、`ix_files_public_id` 唯一索引与其余 6 个索引全部保留,原数据完好。
- **必须同时加 `expires_at IS NULL` 的查询**:SQL 里 NULL 参与比较恒为假,长期有效的文件会**同时从「有效」和「已过期」两边消失**。抽出 `services/files.py:active_clause()` 供 5 处复用;其中 `quota.sync_usage` 最危险——维护任务重算用量时会把长期有效文件排除,**用户配额被静默清零**。
- **入口**:上传表单(`dashboard.html` + `upload.js` 的修改弹窗)按 `is_admin` 追加选项;后台文件编辑弹窗的时长输入框改成下拉,含「长期有效」。
- **后端强制**:`resolve_expiry()` 对 `expires_hours <= 0` 且非管理员直接 400「普通用户不能设置长期有效」,不依赖前端隐藏选项。
- **顺带修复**:`_serialize` 输出的是不带时区的 naive ISO,被前端 `new Date()` 按浏览器本地时区解析(与模板 `fmt_iso` 行为不一致)→ 统一补 `+00:00`;`download.py` 的 ETag 里 `expires_at.timestamp()` 对 `None` 会崩 → 退化为 `public_id-forever-size`。

### 15.5 上传被拒时不再留下孤儿文件

`routers/files.py` 的 `upload_file` 原先在 `storage.store_upload()` **之后**才调用 `_resolve_expiry()`。普通用户伪造 `expires_hours=0` 时,文件已经落盘,`resolve_expiry` 抛 `ForeverNotAllowed` → 转成 400,但**没有任何一处清理那个文件**:数据库里查不到它,磁盘上却永久占着空间(配额不算,`cleanup` 也扫不到)。

修复:把 `_resolve_expiry()` 提到 `store_upload()` **之前**。它只读一次设置加算术,不依赖已落盘的对象,提前调用等于「先验参数再收字节」——既不留孤儿,也省掉整段传输。

回归测试 `test_normal_user_cannot_set_forever_expiry` 增加两条断言:被拒后存储目录的**文件个数**与**总字节数**都不变。对照实验确认改回旧顺序即失败(`assert 1 == 0`)。辅助函数 `count_stored_blobs()` / `stored_blob_bytes()` 放在 `tests/conftest.py`。

### 15.6 验证

- `pytest` → **55 passed**(新增 8 条)。新用例都实测过「改回旧代码即失败」:清空日志漏 `end_of_day`、`sync_usage` 漏 `IS NULL`、上传孤儿文件、单文件上限只读环境变量,四条已用对照实验确认。
- 本地 `uvicorn` + `acceptance.py`(21 项)全绿;另跑一轮长期有效专项:上传 → `expires_at = None` → 首页可见 → 下载 200(`ETag` 含 `forever`)→ 详情页显示「长期有效」→ 普通用户 `expires_hours=0` 在**上传与修改两条路径**都被 400 拒绝。
- 目标机重启后自动执行了 `Running upgrade e97a949478ec -> b3f1c2a45d7e`(两个 worker 只有一个真正执行,启动锁仍生效),容器 `healthy`,线上库 `expires_at` 已 nullable、7 个索引完好、管理员账号与既有数据未受影响。
- 迁移前已在卷内留了一份 `app.db.pre-forever` 备份,已于 2026-10-09 确认无误后删除。
- 目标机复测孤儿文件(15.5):新建一次性普通用户承载探测 → `expires_hours=0` 得 400 → 容器内 `find /app/data/storage -type f` 仍为 **0** 个文件 → 同一用户正常上传 201、删除 200 作为对照 → 探测用户已删除,站点恢复为 1 个用户 / 0 个文件。

## 16. 第三轮调整(2026-10-09)

| # | 诉求 | 处理 |
| --- | --- | --- |
| 1 | 删掉操作日志里「导出 CSV」右侧的「清空日志」按钮,把「导出 CSV」挪到它原来的位置 | ✅ 见 16.1 |
| 2 | 邮件服务的 SMTP 地址默认提示改用 163 而非 QQ | ✅ 见 16.2 |
| 3 | `quota.max_upload_mb` 会随网站设置变动吗 | ✅ 会,见 16.3 |
| 4 | 删掉卷里的 `app.db.pre-forever` 备份 | ✅ 已删 |

### 16.1 日志页按钮

顶部那个「清空日志」按钮其实是**冗余**的:`data-action="purge-logs"` 的处理函数读的是折叠面板里的输入框,所以直接点它只会弹出「请至少指定时间范围或日志类型,避免误删全部日志」,并不会打开面板。删掉后 `.page-head` 只剩一个 `<a>`(连包裹的 `<div class="row">` 一起去掉),`justify-content: space-between` 自然把它顶到右边缘,即原「清空日志」的位置。

删除 `<details>` 折叠面板与其中的「执行清空」按钮,清空功能不受影响。

### 16.2 SMTP 默认提示

`admin/email.html` 的 `placeholder="smtp.qq.com"` → `smtp.163.com`;右侧「常见服务商」列表里 163 提到 QQ 之前(QQ 作为备选项保留)。端口默认 `465` 两家通用,无需改。

### 16.3 `quota.max_upload_mb` 与网站设置的关系

**会跟随网站设置变动,不需要重启。** 上传时每次请求都实时读库:

- `routers/files.py:77` — `site_limit = int(cfg["max_upload_mb"]) * 1024 * 1024`,`cfg` 来自 `settings_service.template_settings(db)`
- 同一值也驱动前台首页「单文件上限」与 `/dashboard` 的展示(`pages.py:204`)

环境变量 `FT_MAX_UPLOAD_MB` **只是首次建库的播种默认值**(`bootstrap._seed_settings` 只补缺失键),不构成任何硬上限——这正是「网站内部的大小我自己来设定」的落点。

⚠️ 一个需要知道的细节:`settings_service` 有一层进程内缓存(`_CACHE_TTL = 15.0` 秒,`_cache` 是模块级全局)。后台保存会调 `set_many` → `load_all` 立刻刷新缓存,但**只刷新处理该请求的那个进程**。线上 `FT_WORKERS=2`,所以另一个 worker 最多再陈旧 15 秒。表现为:改完上限后绝大多数情况立即生效,极端情况下 15 秒内完全一致。**始终不需要重启容器。**

回归测试 `test_max_upload_mb_setting_takes_effect_without_restart`:上限收紧到 1MB → 2MB 文件得 413;放宽到 20MB → 同一个文件立刻 201,且 `/dashboard` 显示「20 MB」。对照实验:把该行改回只读 `env_settings.max_upload_mb` 即失败。
