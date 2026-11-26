安装方式简化：完整版只需 `docker load` 和 `docker compose up -d`，源码版只保留直接运行所需的源码和依赖清单。新增 PostgreSQL 支持：完整版默认使用内置 PostgreSQL，源码版默认仍为 SQLite，两个版本都带 SQLite → PostgreSQL 迁移工具。

### 下载安装

| 安装包 | 用途 |
| --- | --- |
| `file-transfer-full-1.5.0.tar.gz` | Docker 部署，包含 `docker-compose.yml`、`.env.example`、网盘 / PostgreSQL / Aria2 三个镜像、数据库初始化脚本 |
| `file-transfer-src-1.5.0.tar.gz` | 源码部署，包含应用代码、数据库迁移与初始化脚本、迁移工具、`requirements.txt` |

完整版解压后在 `file-transfer` 目录执行：

```bash
docker load -i images/file-transfer-images-1.5.0.tar
docker compose up -d
```

默认访问 `http://服务器IP:8000`，管理员 `admin / admin`，首次登录强制修改密码。使用离线下载前，在后台填写“站点根地址”。

### 变更

- 数据库由 `.env` 的 `FT_DB_MODE` 选择：`builtin`（完整版默认，内置 PostgreSQL，密码首次启动自动生成）、`sqlite`（源码版默认）、`external`（外置 PostgreSQL，配合 `FT_DATABASE_URL`）。完整版改用 SQLite 或外置数据库时，需按 README 删除 `docker-compose.yml` 中标注的内置 PostgreSQL 部分。
- 新增迁移工具 `python -m app.tools.sqlite_to_postgresql`：把 SQLite 的数据整体导入 PostgreSQL，源库只读，导入在单个事务里完成。
- 完整版的数据改为保存在 `docker-compose.yml` 旁边的 `data/` 目录（原先是命名卷），备份时打包整个目录即可。
- 安装包附带 `db/init-postgresql.sql` 与 `db/init-sqlite.sql` 初始化脚本（可选，应用连上空库会自动建表）。
- 搜索在两种数据库上都不区分英文大小写；操作日志中超长的账号、路径等字段会截断保存。
- 签名密钥未配置时首次启动自动生成，保存在数据目录，之后一直沿用。
- 移除一键安装脚本（`install.sh`、`ftctl`、`quick_start.sh`）和随包附带的 Docker 引擎；完整版需要服务器已安装 Docker 25+ 与 Compose v2.24+。
- Aria2 镜像不再发布到 GHCR，改为打包在完整版里。
- 源码版不再包含 Docker 构建文件、测试和 Aria2 离线下载组件。
- 无新增数据库迁移，迁移头仍为 `a6b9e2037c51`。

### 从 1.4.x 升级

先备份，并保留原有 `.env`（含 `FT_SECRET_KEY`）。

- Docker：数据需要从旧的命名卷复制到新的 `data/` 目录，之后可以继续用 SQLite，也可以用迁移工具导入内置 PostgreSQL。具体命令见 README 的「从 1.4.x 升级」。
- 源码 / 原生安装：保留 `.env` 和数据目录，覆盖程序文件；默认继续使用 SQLite。systemd 服务需把 `ExecStart` 改为 README 中的 uvicorn 命令。
