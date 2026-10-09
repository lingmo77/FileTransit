# 文件中转站 · 项目文件说明

适用版本：**file-transfer 1.0**（2026-10-09）

这份文档回答一个问题：**项目目录里这些东西分别是什么，部署时哪些要传到服务器上。**

---

## 0. 一句话结论

**只需要传一个文件**：

```
dist/file-transfer-offline-<日期>.tar.gz     （约 68 MB）
```

它由项目根的 `package.sh` 打包，内容是按**显式清单**挑过的——包含下面所有标 ✅ 的项 + 已构建好的镜像，并且已经自动排除了 `.venv`、`data`、`.env`、`dist` 这四个绝不能传的东西。

想要更小的包（目标机器能联网、愿意现场构建）就用 `file-transfer-src-<日期>.tar.gz`（约 135 KB）。

---

## 1. 根目录文件

| 文件 | 是什么 | 要传吗 |
| --- | --- | --- |
| `Dockerfile` | 镜像构建文件。两段式：编译器和头文件只留在 builder 阶段，最终镜像里没有它们 | ✅ 必须 |
| `docker-compose.yml` | 容器编排：端口映射、数据卷、健康检查 | ✅ 必须 |
| `.dockerignore` | 构建镜像时的排除清单，挡住 `.venv` / `data` / `.env` 进镜像 | ✅ 必须 |
| `.env.example` | 环境变量模板。服务器上 `cp .env.example .env` 再改密钥 | ✅ 必须 |
| `requirements.txt` | 运行时 Python 依赖 | ✅ 必须 |
| `alembic.ini` | 数据库迁移配置，`Dockerfile` 里会 `COPY` 它 | ✅ 必须 |
| `DEPLOY.md` | 部署实施说明（9 节，从头到尾照着做即可） | 📄 建议 |
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
| `docker/` | 只有一个 `entrypoint.sh`（容器启动入口） | ✅ 必须 |
| `tests/` | 测试代码，55 条用例 | ❌ 运行不需要 |
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
# 1) 上传 file-transfer-offline-<日期>.tar.gz，在服务器上解开
tar xzf file-transfer-offline-2026-10-09.tar.gz -C /tmp

# 2) 覆盖代码（.env 不在包里，服务器上原有的不会被覆盖）
cd /home/docker/file-transfer
tar cf - -C /tmp/file-transfer \
    app migrations docker Dockerfile docker-compose.yml \
    alembic.ini requirements.txt .env.example | tar xf -

# 3) 重建并重启
docker compose up -d --build
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
cd /home/docker/file-transfer
find app migrations docker tests -type f ! -name '*.pyc' ! -path '*__pycache__*' \
  | sort | xargs sha256sum | sort -k2 > /tmp/remote_manifest.txt
```

把两份文件放一起 `diff` 即可。两侧对不上的文件就是要同步的。

服务器上比本地**多**的通常只有：

- `.env`（密钥，本地没有）
- `images/`（离线包解出来的镜像 tar，本地在 `dist/` 里）
- `data/`（如果用 `docker compose` 挂在项目目录的话；本项目数据在命名卷 `file-transfer_ft-data` 里，不落在这里）
