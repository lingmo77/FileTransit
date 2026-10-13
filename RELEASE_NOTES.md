# 文件中转站 1.1.2

临时文件传输站点（FastAPI + SQLite），单容器部署，无外部依赖。

**不改数据库结构、不需要迁移**，纯前端 + 模板改动。

## 修复：删掉最后一个分享后，标题里的计数不刷新

1.1.1 引入的回归。表现是删掉文件后卡片当场消失了，标题却还写着
`我的分享 （1）`，不及时整页刷新就一直不改。

**为什么会这样**：1.1.1 为了不掐断正在上传的文件，把所有刷新都改成「等上传
队列空了再刷新」。而标题里的计数和空状态**只由服务端渲染**，前端摘掉卡片时
谁也不去动它。于是只要队列里还挂着东西——最典型的就是一次卡住没结束的上传，
它可能挂在那里几分钟——那次刷新就一直排不上队，计数永久停在旧值上。
这不是「慢一秒」，是「一直不变」。

**改法**：列表区域改成**两个块都渲染、只切显隐**，前端摘掉卡片时自己把计数和
空状态当场改对，不再依赖那次延后的刷新：

- 标题计数块带上 `data-total`，前端只减自己那一份，不用去猜总数
- 删到 0 时把列表收起来、空状态露出来；有文件时反过来
- 分页边界（删掉第 2 页最后一张、第 1 页还有文件）不动空状态——总数为 0 才说明
  是真空了，这时候说「还没有上传过文件」才是真话

顺带补了一个会让页面**永久不再刷新**的隐患：如果某一次请求压根没发出去
（选中的文件读不出来、`FormData` 拼不起来），这一趟占的队列名额不会被还回去，
`active` 再也回不到 0，之后所有「等队列空了再刷新」就全排不上队——计数、配额、
文件列表会一起停在旧值上。现在两条抛错路径都会收尾。

另外 `.file-grid` 是 `display: grid`，优先级高于浏览器默认的 `[hidden]` 规则，
所以补了 `.file-grid[hidden] { display: none }`，否则收起列表这一步是白做的。
（这个坑之前踩过一次：上传行的重试按钮。）

## 升级注意

- **不需要迁移**：只改了静态资源、模板和一处列表更新逻辑，启动时的
  `alembic upgrade` 是空操作
- 已有用户、文件、配额、有效期设置全部不受影响
- 镜像标签仍是 `file-transfer:1.0`（compose 引用的就是它），`docker load` 后照旧
  `docker compose up -d`
- ⚠️ **升级后强制刷新一次**（Ctrl+F5）。静态资源版本号已提升
  （`style.css?v=7`、`upload.js?v=6`），正常会自动失效，但个别代理会缓存旧的
  CSS/JS——不刷新的话列表收起后会留一块空白，重试按钮也可能一直露着

## 下载哪个

| 包 | 大小 | 用途 |
| --- | --- | --- |
| `file-transfer-offline-1.1.2.tar.gz` | 68 MB | 源码 + 已构建镜像，**完全离线可部署（推荐）** |
| `file-transfer-src-1.1.2.tar.gz` | 152 KB | 纯源码，目标机器需联网现场构建 |

## 部署

解包后照 `DEPLOY.md` 操作：

    tar xzf file-transfer-offline-1.1.2.tar.gz -C /opt
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

    sha256sum file-transfer-offline-1.1.2.tar.gz
    # d1ff8cf81848f6d1284ed589e053bb9c1248063b768cf31b9711842c5ea93a2a

    sha256sum file-transfer-src-1.1.2.tar.gz
    # a2d8d000bf3e17e83a9728292513d0d4d7e63170ffad894635770049968f85c8

离线包内的镜像（`images/file-transfer-1.0.tar`）另有校验值，见包内
`images/SHA256SUMS`：

    feab2d8baab0cb1cc6c154e017169892a8f44d32e5861522fd9281023eaad51e
