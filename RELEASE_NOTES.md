新增匿名上传（默认关闭），并调整配置：页面上的时间统一按站点时区显示；数据库配置改为 `.env` 里的 `DB_BUILTIN` / `DB_HOST` 等几项，内置数据库密码直接写在 `.env` 里；外置 PostgreSQL 的初始化脚本单独提供，并带上管理员和默认设置。

### 下载安装

| 安装包 | 用途 |
| --- | --- |
| `file-transfer-full-1.6.0.tar.gz` | Docker 部署，包含 `docker-compose.yml`、`.env`、镜像包 `images-1.6.0.tar`（网盘、Aria2、官方 `postgres:18.7-trixie`）、数据库初始化脚本 `db/` |
| `file-transfer-src-1.6.0.tar.gz` | 源码部署，包含应用代码、数据库迁移与初始化脚本、迁移工具、`requirements.txt`、`.env.example` |

完整版解压后在 `file-transfer` 目录执行：

```bash
docker load -i images-1.6.0.tar
docker compose up -d
```

默认使用内置 PostgreSQL，首次启动自动建库并生成随机密码。访问 `http://服务器IP:8000`，管理员 `admin / admin`，首次登录强制修改密码。

### 变更

- 数据库配置：`FT_DB_MODE`、`FT_DATABASE_URL` 改为以下几项（密码照原样填写，不需要 URL 编码）。仍设置旧配置时启动报错并提示如何修改。

  ```dotenv
  DB_BUILTIN=true        # true：内置数据库（默认）；false：外置数据库，为空使用sqlite
  DB_HOST=
  DB_PORT=5432
  DB_NAME=
  DB_USER=
  DB_PASSWORD=
  ```

- 内置数据库密码：不再单独保存在 `data/secrets/postgres-password`，改为首次启动时写进 `.env` 的 `DB_PASSWORD`，`.env` 改为只有属主可读写。完整版安装包直接带 `.env`，不再需要 `cp .env.example .env`。
- 初始化脚本（安装包的 `db/init-postgresql.sql`、`db/init-sqlite.sql`）除了表结构，还写入默认设置和管理员 admin / admin（首次登录强制修改密码），导入后即可启动使用；导入方法见 README 的「外置 PostgreSQL」。
- 内置数据库改为直接使用官方镜像 `postgres:18.7-trixie`（随镜像包导入，镜像名不变），不再构建 `file-transfer-postgres`；原有数据目录直接沿用。
- 镜像包移到安装包根目录，改名为 `images-<版本>.tar`。
- 改用外置数据库或 SQLite 时，只需删除 `docker-compose.yml` 里的 `depends_on` 和 `db` 服务两处。
- 新增匿名上传（默认关闭）：后台「站点设置 → 匿名上传」开启后，未登录用户可在 `/upload` 上传，文件最多保存 1 天、凭链接下载、默认不上首页。按 IP（IPv6 按 /64）限制每天的文件数和总量，并限制全站每小时文件数、匿名文件总空间和同时上传数，都可在后台调整。
- 日志：每次匿名上传及被拒绝都写入操作日志（IP、浏览器、文件信息、拒绝原因）；所有下载都记录日志，已登录用户的下载记在本人名下。原「记录匿名下载日志」开关取消，下载始终记录。
- 修复：离线下载页的「保存于」时间和「修改文件设置」弹窗里的到期时间按浏览器所在时区显示，与其他页面不一致。现在全站统一按后台「站点设置」的时区显示。
- 新增数据库迁移 `b5e1d7c3a9f4`（用户表加 `is_system` 字段、建匿名上传系统账号、日志表加索引），启动时自动执行。

### 从 1.5.0 升级

完整版（先备份 `data/` 和 `.env`）：

1. `docker compose down`，在原目录的上一级解压新包，跳过包里的 `.env`：`tar xzf file-transfer-full-1.6.0.tar.gz --exclude=file-transfer/.env`。删过内置 PostgreSQL 的，`docker-compose.yml` 要再删一次。
2. 修改原来的 `.env`：删除 `FT_DB_MODE` 和 `FT_DATABASE_URL` 两行，按原来的值改写：
   - `builtin`：加上 `DB_BUILTIN=true`，并把密码搬进来：`echo "DB_PASSWORD=$(cat data/secrets/postgres-password)" >> .env`。确认网盘正常后可以删除 `data/secrets/`。
   - `external`：加上 `DB_BUILTIN=false`，把原连接串 `postgresql://用户:密码@主机:端口/库名` 拆成 `DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD`（密码写解码后的原文，例如 `%40` 写回 `@`）。
   - `sqlite`：加上 `DB_BUILTIN=false`，`DB_HOST` 留空。
3. `docker load -i images-1.6.0.tar`，然后 `docker compose up -d`。数据目录沿用，内置数据库从 18.6 升到 18.7 属于小版本升级，不需要转换。确认正常后，旧的 `images/` 目录和 `file-transfer-postgres:1.5.0` 镜像（`docker rmi file-transfer-postgres:1.5.0`）可以删除。

源码版：覆盖程序文件，保留 `data/`、`.venv/`；`.env` 里删除 `FT_DB_MODE` 和 `FT_DATABASE_URL`，使用 SQLite 的加上 `DB_BUILTIN=false`，使用 PostgreSQL 的按上面 `external` 的方式改写，然后重启。

从 1.4.x 升级见 README 的「从 1.4.x 升级」。
