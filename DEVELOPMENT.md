# 开发说明

安装、配置和升级见 [README](README.md)（它也随安装包发布）。本页面向维护者。

## 目录

本地工作目录 `File Transfer/` 下只有两个文件夹：`src/` 是 git 仓库（推送的就是它的内容），`release/` 放打包产物，不进仓库。仓库内：

```text
app/                 FastAPI 应用：core/ 配置与数据库，models/，routers/，services/，templates/，static/，
                     tools/ 随应用发布的维护工具（SQLite → PostgreSQL 迁移）
migrations/          Alembic 迁移
db/                  数据库初始化脚本 init-*.sql 与生成脚本
alembic.ini
requirements.txt     运行时依赖（全部锁定版本）
.env.example         源码版的配置模板
docker/app/          网盘镜像 Dockerfile 与 entrypoint（以 root 修正挂载目录属主后降权运行）
docker/postgres/     内置 PostgreSQL 的启动脚本：compose 把它挂进官方镜像 postgres:18.7-trixie，首次启动生成数据库密码并写进 .env
docker/env.example   完整版的配置文件，打包时改名为 .env（权限 0600）
docker/aria2/        离线下载引擎镜像：Dockerfile、守护进程、出口防火墙、网络保护
tests/               pytest 回归测试；run_in_docker.sh 在服务器上用 Docker 跑完整测试
docs/screenshots/    README 的界面预览图（不进发布包）
docker-compose.yml   网盘 + 内置 PostgreSQL（官方镜像）+ Aria2，带 build 段，发布时由 package.py 改写
package.py           生成两个发布包，输出到仓库旁边的 ../release/<版本>/，只保留最新两个版本
RELEASE_NOTES.md     当前版本的发布说明（GitHub Release 正文）
```

## 测试

测试统一在装有 Docker 的测试服务器上运行，本地不装依赖、不留虚拟环境和运行数据。把仓库同步到服务器后，在仓库根目录执行：

```bash
sh tests/run_in_docker.sh            # 先 SQLite、再临时 PostgreSQL 各跑一遍完整测试
sh tests/run_in_docker.sh -k quota   # 额外参数原样传给 pytest
```

脚本用与生产镜像相同的 `python:3.12-slim`，仓库只读挂载、复制到容器内再测，不会在服务器的源码目录里留下缓存或数据；PostgreSQL 用临时容器（tmpfs），结束即删除。pip 下载缓存保存在 `ft-test-pip-cache` 卷里以加快重跑。

不用脚本时：先 `pip install -r requirements.txt pytest==9.1.1 pytest-asyncio==1.4.0`，再按脚本里的 pytest 参数在仓库根目录运行，默认跑 SQLite；设置 `FT_TEST_DATABASE_URL=postgresql://.../<含 test 的库名>` 改跑 PostgreSQL（`tests/conftest.py` 把它拆成 `DB_HOST` 等几项；测试会清空该库的 `public` schema）。手动启动应用：在仓库根目录 `uvicorn app.main:app --port 8000`，数据写到 `data/`，配置读 `.env`。

## 架构要点

- FastAPI + Uvicorn 多 worker，SQLAlchemy 2 异步，Jinja2 服务端渲染 + 原生 JS/CSS，无前端构建步骤。
- **数据与文件分离**：数据库只存元数据、日志和站点设置；文件本体在数据目录 `storage/`，下载用 `FileResponse`（支持 Range / HEAD / If-Range），字节流不经过数据库事务。
- **上传**：整文件流式写入 `tmp/` 后原子移动；接收名额和临时空间预算用跨 worker 的文件锁控制；配额在入库前的短写事务里复核并扣减。
- **多 worker 协调**：启动时用数据目录里的文件锁串行执行迁移和种子数据；定时维护用 `task_locks` 表互斥。
- **站点设置**存 `settings` 表，后台修改即时生效；SMTP 密码等敏感值用签名密钥派生的密钥加密。
- **匿名上传**：`app/routers/anon.py` + `app/services/anonymous.py`。匿名文件挂在迁移建好的系统账号 `@anonymous`（`users.is_system`）名下，文件表、清理、下载都复用登录用户的逻辑；系统账号不能登录，不出现在用户管理和统计里。限额按 `activity_logs` 里的 `anon_upload` 记录统计（成功日志与文件记录同一事务提交，入库前在写锁内复核），并发用 `upload_slots.receive_slot` 的分组名额限制。
- **离线下载**：Aria2 在独立容器，网盘通过 RPC 协调，任务状态持久化在 `offline_tasks*` 表；下载完成后导入网盘存储并计入配额。

## 数据库

由 `.env` 里不带 `FT_` 前缀的 `DB_BUILTIN`、`DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD` 决定（`app/core/config.py` 的 `db_kind`，字段用 `validation_alias` 读取）：`DB_BUILTIN=true` 是 compose 里的 `db` 服务（完整版默认，只用 `DB_PASSWORD`）；`false` 时填了 `DB_HOST` 就是外置 PostgreSQL，没填就是数据目录下的 `app.db`（源码版默认）。连接串由 `URL.create` 拼出，密码不需要用户自己编码。1.5.0 的 `FT_DB_MODE`、`FT_DATABASE_URL` 仍被设置时直接报错，不按旧值猜测。PostgreSQL 运行时用 asyncpg，迁移和启动初始化用 psycopg 3。不支持其他数据库。

内置 PostgreSQL 直接用官方镜像 `postgres:18.7-trixie`，不改镜像名，也不另外构建：compose 把 `docker/postgres/entrypoint.sh` 挂进容器作为入口，它准备好密码后交给官方的 `docker-entrypoint.sh`；健康检查写在 compose 里。升级 PostgreSQL 小版本时只改 compose 的镜像标签、`tests/run_in_docker.sh` 的 `PG_IMAGE` 和下面的 `docker save` 命令。compose 目录的 `.env` 以单文件方式挂进两个容器（`/run/ft-env/.env`，db 可写、网盘只读）。`docker/postgres/entrypoint.sh` 发现 `DB_PASSWORD` 为空时生成 40 位字母数字密码，原地改写 `.env`（单文件挂载不能用 mv 替换）并改为 0600。compose 在这之前就读完了 `.env`，所以首次启动时 `env_file` 传给网盘的还是空值：`docker/app/entrypoint.sh` 在环境变量里的 `DB_PASSWORD` 为空时，降权前以 root 从挂载的 `.env` 读出密码再导出（有值时以 compose 的解析为准），网盘按 `builtin_db_host` 拼出连接串。库已初始化但 `.env` 里没有密码时，数据库容器拒绝启动，不会生成对不上的新密码。`tests/test_builtin_db_password.py` 直接运行这两个脚本验证。网盘启动时会等待 PostgreSQL 就绪（最长 120 秒）。

业务代码按「同一时刻只有一个写事务」设计：配额扣减、离线任务认领都是先写一行取锁，再读最新状态。SQLite 天然如此；PostgreSQL 下 `app/core/database.py` 让每个事务在第一条写语句前取得事务级咨询锁 `pg_advisory_xact_lock`，提交或回滚自动释放，只读查询不受影响。不要绕过这个机制，也不要在写事务里做文件传输。

时间一律存不带时区的 UTC。搜索用 `ilike`，保证两种数据库都不区分英文大小写。PostgreSQL 会强制检查字符串长度，写入用户可控的字符串前要截断到列宽。

新增迁移或修改默认设置后运行 `python db/generate_init_sql.py` 重新生成 `db/init-*.sql` 并一起提交，`tests/test_database.py` 会检查两者是否同步，并在 PostgreSQL 轮里把脚本导入空库再启动应用。脚本除了表结构，还写入 `DEFAULTS` 里的默认设置（`site.base_url`、`offline.enabled` 取决于部署方式，留给应用启动时写入）和管理员 admin/admin；管理员的 bcrypt 哈希用固定盐，保证重新生成的内容不变。旧迁移里重建表的写法（`recreate="always"`）只在 SQLite 上执行，PostgreSQL 走原生 `ALTER`。不要删除或合并已发布的迁移：已有站点要靠完整的迁移链升级。

迁移工具 `app/tools/sqlite_to_postgresql.py` 按 `Base.metadata` 的依赖顺序逐表复制：源库先做临时副本并升级到最新迁移，目标库在一个事务里导入（持有同一把写锁），最后把自增序列推到最大 id 之后。新增表或列只要进了模型和迁移，工具不需要改；新增的日志类表如果允许截断超长值，加进 `TRUNCATABLE`。

## 构建与发布

镜像不走 CI，也不推送到镜像仓库：在测试服务器上构建，打进完整版安装包，随 GitHub Release 发布。

1. 在服务器上用 `tests/run_in_docker.sh` 跑完整测试。
2. 在服务器的仓库根目录构建镜像、启动验证完整版，再把网盘、Aria2 和官方 PostgreSQL 三个镜像导出到同一个 tar（用户只需一次 `docker load`）：

   ```bash
   docker compose build                 # 生成 file-transfer、file-transfer-aria2 两个 <版本> 镜像
   docker pull postgres:18.7-trixie     # 官方镜像，原名打包
   docker save file-transfer:1.6.0 file-transfer-aria2:1.6.0 postgres:18.7-trixie \
       -o /root/ft-images-1.6.0.tar
   ```

3. 把镜像 tar 取回本地临时目录，在本地 `src/` 打包（`package.py` 只用标准库，不需要安装依赖）：

   ```bash
   python package.py --version 1.6.0 --images <临时目录>/ft-images-1.6.0.tar
   ```

4. 以 `RELEASE_NOTES.md` 为正文创建 GitHub Release，附上 `release/1.6.0/` 里的三个文件。

产物写到仓库旁边的 `release/<版本>/`（可用 `--output` 指定其他根目录）。`release/` 只保留版本号最新的两个版本，更旧的版本目录在打包后自动删除；重打同一版本会先清空该版本目录。每个版本目录里：

- `file-transfer-full-<版本>.tar.gz`：由 `package.py` 改写的 `docker-compose.yml`（去掉开头的仓库说明和 build 段，本项目镜像标签固定为本次版本、官方 PostgreSQL 镜像保持原名原标签，都加 `pull_policy: never`，其余注释保留）、`.env`（来自 `docker/env.example`，权限 0600）、`images-<版本>.tar`（放在包的根目录；打包时会检查 compose 用到的镜像是否都在里面）、`docker/postgres/entrypoint.sh`、`db/init-*.sql`、`README.md`、`LICENSE`、`VERSION`。
- `file-transfer-src-<版本>.tar.gz`：`app/`、`migrations/`、`alembic.ini`、`requirements.txt`、`.env.example`、`db/init-*.sql`，加 `README.md`、`LICENSE`、`VERSION`。
- `SHA256SUMS`：两个包的校验值。

只生成源码版：`python3 package.py --version 1.6.0`。两个包都不含测试、Dockerfile、内部文档、仓库里的 `.env` 或用户数据。

发版时同步更新 `app/__init__.py`、`app/main.py` 中的版本号，`docker-compose.yml` 的镜像标签，以及 `RELEASE_NOTES.md`。只有决定发布后才推送、打标签、创建 Release。
