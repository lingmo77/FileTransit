# 文件中转站 1.0

临时文件传输站点（FastAPI + SQLite），单容器部署，无外部依赖。

## 本次更新

### 新增：管理员可指定用户永久保存文件

后台「用户管理」里每个账号多了一个 **允许永久保存** 开关。被勾选的用户上传时可以选
**长期有效（永久保存）**，文件不再自动清理（数据库里 `expires_at` 存 `NULL`）。

- 权限**按用户逐个发放**，不再是「只有管理员能用」
- 管理员天然具备该权限，不需要勾这个开关
- 新建用户时就可以直接勾上
- **收回权限不追溯**：用户已经存在的长期有效文件保持不动，不会被删掉
- 前端藏起选项的同时后端也会拦，伪造 `expires_hours=0` 只会拿到 400

### 修复：有文件时后台首页 500

`/admin` 在**库里有文件**时会返回 Internal Server Error。原因是大盘页要显示
「上传者」，而对应的查询漏了预加载关联，异步会话下会抛 `MissingGreenlet`。
文件为空时模板那段循环不执行，所以这个 bug 只在有真实数据的站点上才暴露。

### 升级注意

启动时自动执行数据库迁移（`b3f1c2a45d7e` → `c8e5a71f4b90`，给 `users` 加一列），
**不需要手动 migrate，已有数据不受影响**。

## 下载哪个

| 包 | 大小 | 用途 |
| --- | --- | --- |
| `file-transfer-offline-2026-10-09.tar.gz` | 68 MB | 源码 + 已构建镜像，**完全离线可部署（推荐）** |
| `file-transfer-src-2026-10-09.tar.gz` | 141 KB | 纯源码，目标机器需联网现场构建 |

## 部署

解包后照 `DEPLOY.md` 操作：

    tar xzf file-transfer-offline-2026-10-09.tar.gz -C /opt
    cd /opt/file-transfer
    cp .env.example .env      # 改 FT_SECRET_KEY 和 FT_ADMIN_PASSWORD
    docker load -i images/file-transfer-1.0.tar
    docker compose up -d

已有部署升级：覆盖源码后用离线包里的镜像 `docker load`，再 `docker compose up -d`。
**升级前先备份数据卷**（数据库与用户文件都在 `file-transfer_ft-data` 里）。

## 环境要求

- Linux **x86_64**（镜像按 amd64 构建，ARM 请用源码包自行构建）
- Docker 25.0+（镜像为 OCI layout 格式）
- Docker Compose v2
- 内存 1 GB 起，建议 2 GB+

## 完整性校验

    sha256sum file-transfer-offline-2026-10-09.tar.gz
    # d4ebddc23b28cd090195468982e7a5935b40a174c2a8bcf3d0b8f3e03c5d82df

    sha256sum file-transfer-src-2026-10-09.tar.gz
    # 68b75e4ed0ade8e67dfb1bdfd8ffbc6014708a89aa842844b53822388d3f4b64
