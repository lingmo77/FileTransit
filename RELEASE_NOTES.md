# 文件中转站 1.0.1

临时文件传输站点（FastAPI + SQLite），单容器部署，无外部依赖。

这是 1.0 的补丁版，只修一个下载相关的缺陷：**不改数据库结构、不需要迁移**。

## 修复：ETag 不合法，会让 `If-Range` 失效

下载接口返回的 `ETag` 里直接拼了文件的有效期时间，形如：

    ETag: "WU-nM08ArOQ-2026-10-12 03:11:22.014865-21306820"

中间那个**空格**违反 RFC 7232 的 `opaque-tag` 规则（`etagc` 是 `%x21 / %x23-7E`，
不允许 `%x20`），是个非法 ETag。

下载器（IDM / 迅雷 / aria2）断点续传和分片抓取时会把这个值回传到 `If-Range`。
Starlette 的 `FileResponse` 只要 `If-Range` 匹配失败，就会**返回 `200` 整包而不是
`206` 分片**——分片连接收到整包，只能被客户端掐断。

现在只保留 `public_id` 和文件大小，两者都只含字母数字和 `-` / `_`，天然合法：

    ETag: "WU-nM08ArOQ-21306820"

同一个 `public_id` 的内容永不改变（重名上传会换 `public_id`），改有效期、改文件名
都不影响字节，所以去掉 `expires_at` 对缓存正确性没有影响。

> 附带说明：这个改动清掉的是「部分客户端可能踩到」的隐患。**如果多线程下载只连一条
> 连接，先看服务器上行带宽。** 实测本站单连接与四连接吞吐几乎相同
> （20.6 Mbps vs 25.8 Mbps），瓶颈在带宽而不在连接数——这种情况下多开连接本来
> 就不会有收益，下载器会自己收敛回单连接。

## 升级注意

- **不需要迁移**：数据库结构没变，启动时自动执行的 `alembic upgrade` 是空操作
- 已有用户、文件、配额、设置全部不受影响
- 镜像标签仍是 `file-transfer:1.0`（compose 引用的就是它），`docker load` 后照旧
  `docker compose up -d` 即可
- 升级后可在服务端自查，`ETag` 里不该再有空格：

      curl -sI http://<站点>/d/<public_id> | grep -i etag

## 下载哪个

| 包 | 大小 | 用途 |
| --- | --- | --- |
| `file-transfer-offline-1.0.1.tar.gz` | 68 MB | 源码 + 已构建镜像，**完全离线可部署（推荐）** |
| `file-transfer-src-1.0.1.tar.gz` | 142 KB | 纯源码，目标机器需联网现场构建 |

## 部署

解包后照 `DEPLOY.md` 操作：

    tar xzf file-transfer-offline-1.0.1.tar.gz -C /opt
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

    sha256sum file-transfer-offline-1.0.1.tar.gz
    # dc3fb0ba8c57ba36c04efb18d9c2a8ad21887c0346e104add35fad1bc9037176

    sha256sum file-transfer-src-1.0.1.tar.gz
    # 85e619137aab601f6e8872980db0b220249c9e33ef310228537043a3bffd3779
