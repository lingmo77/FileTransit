# 文件中转站 1.1.1

临时文件传输站点（FastAPI + SQLite），单容器部署，无外部依赖。

**不改数据库结构、不需要迁移**，纯前端 + 模板改动。

## 修复：上传过程中删除文件会把上传一起掐断

删除自己的文件后页面会立刻重载，而**重载会 abort 掉所有在途的 XHR**——正在
传的那几个当场变成「已取消」，看起来像是删除操作把上传弄没了。修改文件设置的
保存路径有同样的问题。

现在队列页只有**一个**刷新出口，而且它排队等上传跑完再刷：

- 有上传在跑时，删除/修改只登记「待刷新」，页面不重载
- 队列空了再刷新，文件列表和配额数字照旧是最新的
- 这条约束有回归测试钉着（`test_upload_queue_has_exactly_one_reload_exit`）

## 新增：重试按钮

队列每一行右侧多了个重试按钮，**取消后、失败后、上传卡死时**都会出现：

- 卡死（超过 2 秒收不到进度事件）时状态显示「已停顿，可重试…」，重试按钮同时
  露出；进度一旦恢复，按钮自动收回去
- 点重试＝中断当前这次、带着同一个文件立刻重来一遍，进度条从头开始
- 「已取消」「失败」的行同样可以重试，不用再重新选文件

顺带一个细节：重试是**新起一次请求**，所以上一次尝试迟到的 abort 事件不会被
误记到新一次头上（否则新一次的进度条会被标成「已取消」）。

## 调整：单文件上限一律按 GB

- 前台「单文件上限」和上传区的提示：`20.00 GB`，不再写成 `20480 MB`
- 后台设置项改名为「单文件大小上限（GB）」，按 GB 填（填 20 就是 20 GB）
- 超限的报错信息同样按 GB 显示

内部存储的键和单位没变（仍是 `quota.max_upload_mb`），**已有配置不用重新填**，
也没有数据迁移。小数值仍会自动降级显示成 MB（比如设 0.5 GB 会显示 `512.00 MB`），
免得出现「0 GB」这种读数。

## 升级注意

- **不需要迁移**：只改了静态资源、模板和一处设置项展示，启动时的
  `alembic upgrade` 是空操作
- 已有用户、文件、配额、有效期设置全部不受影响
- 镜像标签仍是 `file-transfer:1.0`（compose 引用的就是它），`docker load` 后照旧
  `docker compose up -d`
- ⚠️ **升级后强制刷新一次**（Ctrl+F5）。静态资源带版本号查询串，正常会自动失效，
  但个别代理会缓存旧的 CSS/JS——不刷新的话重试按钮会因为缺 CSS 而一直显示

## 下载哪个

| 包 | 大小 | 用途 |
| --- | --- | --- |
| `file-transfer-offline-1.1.1.tar.gz` | 68 MB | 源码 + 已构建镜像，**完全离线可部署（推荐）** |
| `file-transfer-src-1.1.1.tar.gz` | 150 KB | 纯源码，目标机器需联网现场构建 |

## 部署

解包后照 `DEPLOY.md` 操作：

    tar xzf file-transfer-offline-1.1.1.tar.gz -C /opt
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

    sha256sum file-transfer-offline-1.1.1.tar.gz
    # 38c47b03fc3e3a05752589c11d132da28d09f51c723c152f9c45b371473448ae

    sha256sum file-transfer-src-1.1.1.tar.gz
    # 424fea3a7001a3905c3eb8dce77be384187b5912ec6b122da8fd4a9b3b70e3b0

离线包内的镜像（`images/file-transfer-1.0.tar`）另有校验值，见包内
`images/SHA256SUMS`：

    b3d7e1ec733e701420528645df73155d1e9d3c7a85bd0b84bc34c0b9e7822b61
