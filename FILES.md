# 文件中转站 · 项目文件说明

适用版本：**file-transfer 1.3.0**（2026-10-22）

这份文档回答一个问题：**项目目录里这些东西分别是什么，部署时哪些要传到服务器上。**

---

## 0. 一句话结论

**按需要选择一个安装包**（配置见 `EDITIONS.md`）：

```
dist/file-transfer-standard-1.3.0.tar.gz      # 仅文件中转
dist/file-transfer-with-aria2-1.3.0.tar.gz    # 含可选离线下载
```

它由项目根的 `package.py` 或 `package.sh` 按显式清单打包，标准版包含网盘镜像，增强版包含网盘/Aria2 两份镜像，两者均有源码和通用配置；排除 `.venv`、`data`、`.env`、`dist`。仅上传选定的发布包，不同步整个开发目录。

目标机器能联网并现场构建时，可使用更小的 `file-transfer-src-1.3.0.tar.gz`。包大小和校验值以 Release 附件为准。

---

## 1. 根目录文件

| 文件 | 是什么 | 要传吗 |
| --- | --- | --- |
| `Dockerfile` | 网盘镜像构建文件，两阶段安装二进制 wheel | ✅ 必须 |
| `docker-compose.yml` | 标准版网盘编排：端口、数据卷、健康检查 | ✅ 必须 |
| `docker-compose.aria2.yml` | 增强版完整编排，网盘与 Aria2 在同一文件，单独使用 | 增强版需要 |
| `EDITIONS.md` | 两种安装包的配置、升级与切换说明 | 📄 建议 |
| `.dockerignore` | 构建镜像时的排除清单，挡住 `.venv` / `data` / `.env` 进镜像 | ✅ 必须 |
| `.env.example` | 环境变量模板。服务器上 `cp .env.example .env` 再改密钥 | ✅ 必须 |
| `requirements.txt` | 运行时 Python 依赖 | ✅ 必须 |
| `alembic.ini` | 数据库迁移配置，`Dockerfile` 里会 `COPY` 它 | ✅ 必须 |
| `DEPLOY.md` | 部署实施说明，第 10 节为 Aria2 配置 | 📄 建议 |
| `RELEASE_NOTES.md` / `OFFLINE_DOWNLOAD_PLAN.md` | 当前发布内容、实施与验证记录 | 📄 建议 |
| `FILES.md` | 本文档 | 📄 建议 |
| `README.md` | 项目简介 | 📄 可选 |
| `PROJECT.md` | 开发文档：架构、需求、每轮调整记录 | 📄 可选 |
| `package.sh` | 打包脚本，**在开发机上跑**，服务器上没用 | ❌ 不用 |
| `pytest.ini` | 测试配置 | ❌ 不用 |
| `requirements-dev.txt` | 测试专用依赖 | ❌ 不用 |
| `.gitignore` | Git 忽略清单，不影响运行 | ❌ 不用 |

---

## 2. 子目录

| 目录 | 是什么 | 要传吗 |
| --- | --- | --- |
| `app/` | **应用本体**，见下面第 3 节 | ✅ 必须 |
| `migrations/` | Alembic 迁移脚本 | ✅ 必须 |
| `docker/` | 网盘 `entrypoint.sh`，以及 `aria2/` 独立引擎镜像、网络出口过滤和磁盘监测 | ✅ 必须 |
| `tests/` | 自动化测试及隔离联调脚本，188 项 pytest 回归 | ❌ 运行不需要 |
| `data/` | **本地开发用的 SQLite 库**（`app.db`） | ⛔ 绝不能传 |
| `dist/` | 打包产物本身（就是上面那两个 tar.gz） | ❌ 不用 |
| `.venv/` | **Windows 本地虚拟环境**，几百 MB，Windows 二进制 | ⛔ 绝不能传 |

### 2.1 `app/` 内部结构

| 子目录 | 内容 |
| --- | --- |
| `app/main.py` | FastAPI 应用入口 |
| `app/core/` | 基础设施：`config.py` 配置、`database.py` 数据库、`bootstrap.py` 启动引导（跑迁移 + 播种设置）、`security.py` 密码/会话、`crypto.py` SMTP 密码加解密、`deps.py` 依赖注入、`templating.py` 模板与时间格式化、`timeutil.py` 站点时区换算 |
| `app/models/` | SQLAlchemy 模型：`user` / `file` / `setting` / `activity` |
| `app/routers/` | HTTP 路由：`pages` 页面、`files` 文件接口、`download` 下载（支持 Range）、`api_auth` 登录注册、`admin` 后台接口、`admin_pages` 后台页面 |
| `app/services/` | 业务逻辑：`auth` 认证、`users` 用户、`files` 文件、`storage` 落盘、`quota` 配额、`cleanup` 过期清理、`logs` 操作日志、`activity` 动态、`mailer` 邮件、`settings_service` 设置（带 15 秒进程内缓存）、`stats` 统计、`account` 账户、`pagination` 分页 |
| `app/templates/` | Jinja2 模板：`base.html` 骨架 + 页面 + `admin/` 后台 7 个 + `partials/` 片段 + `email/` 邮件模板 |
| `app/static/` | `css/style.css`、`js/app.js` / `upload.js` / `admin.js`、`favicon.svg`。原生实现，无 Node 构建 |

---

## 3. 绝不能传的三个

| 项 | 原因 |
| --- | --- |
| `.env` | 里面是 `FT_SECRET_KEY` 和 SMTP 密码。泄露 `FT_SECRET_KEY` 等于交出会话签名密钥，且能解密库里已加密的 SMTP 密码 |
| `data/` | 本地数据库。传上去会覆盖服务器上**正在使用**的库 |
| `.venv/` | Windows 版虚拟环境，Linux 容器里完全不能用（镜像内是重新 `pip install` 的） |

> `package.sh` 的打包清单里本来就没有这三项，用打包产物就天然安全。

---

## 4. 上传与更新步骤

```bash
# 1) 上传标准版安装包，在服务器上解开；增强版步骤见 EDITIONS.md
tar xzf file-transfer-standard-1.3.0.tar.gz -C /tmp

# 2) 覆盖代码（.env 不在包里，服务器上原有的不会被覆盖）
cd /opt/file-transfer
tar cf - -C /tmp/file-transfer \
    app migrations docker Dockerfile docker-compose.yml \
    alembic.ini requirements.txt .env.example | tar xf -

# 3) 标准版只加载网盘镜像；已有覆盖文件要继续使用
docker load -i /tmp/file-transfer/images/file-transfer-1.0.tar
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.yml up -d --no-build
# 增强版使用 with-aria2 安装包，包内默认 Compose 已包含两个服务，按 EDITIONS.md 启动。
```

> ⚠️ **只 `docker compose build` 而不跟 `up -d` 会留下不一致状态**：标签已指向新镜像，但运行中的容器仍绑在旧镜像 ID 上，`docker system df` 会把它算成 300 MB 左右「可回收」。补一次 `up -d` 即可对齐。详见 `DEPLOY.md` §8。

改过前端资源（`app/static/`、`templates/`）时，上线前要升版本号，否则浏览器缓存里的旧文件不会更新：

- `app/templates/dashboard.html` 里的 `upload.js?v=`
- `app/templates/admin/base_admin.html` 里的 `admin.js?v=`

---

## 5. 核对服务器上的文件是否与本地一致

在**本地**项目根目录跑：

```bash
find app migrations docker tests -type f ! -name '*.pyc' ! -path '*__pycache__*' \
  | sort | xargs sha256sum | sort -k2 > /tmp/local_manifest.txt
```

在**服务器**上跑（同一份清单文件名，排除 `images/`、`.env` 等服务器专有项）：

```bash
cd /opt/file-transfer
find app migrations docker tests -type f ! -name '*.pyc' ! -path '*__pycache__*' \
  | sort | xargs sha256sum | sort -k2 > /tmp/remote_manifest.txt
```

把两份文件放一起 `diff` 即可。两侧对不上的文件就是要同步的。

服务器上比本地**多**的通常只有：

- `.env`（密钥，本地没有）
- `images/`（离线包解出来的镜像 tar，本地在 `dist/` 里）
- `data/`（如果用 `docker compose` 挂在项目目录的话；本项目数据在命名卷 `file-transfer_ft-data` 里，不落在这里）

新增离线下载后还会有 `ft-offline`（共享暂存/密钥）和 `ft-aria2-state`（仅引擎状态）命名卷。原有 `ft-data` 保持原名和内容。源码包包含可选 `docker/aria2/`；标准版只需要网盘镜像，增强版才需要 Aria2 镜像，并使用包含两个服务的单个 Compose 文件。具体命令见 `EDITIONS.md`。
