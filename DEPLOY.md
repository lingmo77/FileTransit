# 部署与构建

日常安装只需阅读 [安装与升级](EDITIONS.md)。本页用于源码维护和发布构建。

## 源码运行

Python 3.12+，本地使用 `.venv`；依赖锁定于 `requirements.txt`。应用启动自动完成 Alembic 迁移、缺失设置和管理员初始化；已有值不覆盖。

`.env.example` 为手动开发配置参考。生产使用 `install.sh` 自动生成 `.env`，无需复制模板。开发默认 `admin/admin` 和占位签名密钥不能用于生产。

两份根目录 Compose 用于源码维护，分别是普通文件中转和同时包含 Aria2 的完整配置，应单独使用，不叠加。Release 完整包的 Compose 移除了 build 段，使用已打包的固定版本镜像，不执行拉取。

## 反向代理

应用默认 HTTP 8000。用 Nginx/Caddy 提供 HTTPS 时，在 `.env` 设置 `FT_TRUST_PROXY=true`、`FT_COOKIE_SECURE=true`，并重启应用。后台更新站点根地址。只允许可信代理访问应用端口。

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_request_buffering off;
    client_max_body_size 20480m;
    proxy_read_timeout 3600s;
}
```

## Aria2 网络保护

下载引擎位于独立容器；拒绝内网、回环、链路本地、Docker 网段及非公网 IPv6 目标，自动封锁后台站点地址解析出的 IP。保护服务持续刷新；解析或保护不可用时暂停下载。磁力、BT、跳转也受容器出口规则约束。

`FT_ARIA2_BLOCKED_NETWORKS` 为可选额外 IP/CIDR 列表，例如 CDN 后隐藏的源站公网地址。它不替代站点地址；普通部署留空。RPC 密钥由下载容器首次启动生成并持久保存，不能公开到网络或仓库。完整包不映射 RPC；原生接入配置仅绑定 127.0.0.1。

## 构建发布包

开发机器没有 Docker，镜像在构建服务器构建。先运行 `.venv` 的完整 pytest 回归，再在服务器验证两个安装包，最终发布只上传两个包和 `SHA256SUMS`。

```bash
docker build -t file-transfer:1.4.0 .
docker build -f docker/aria2/Dockerfile -t file-transfer-aria2:1.4.0 .
mkdir -p dist
docker save file-transfer:1.4.0 -o dist/app.tar
docker save file-transfer-aria2:1.4.0 -o dist/aria2.tar
bash deploy/prepare-runtime.sh
python3 package.py --version 1.4.0 --image dist/app.tar --aria2-image dist/aria2.tar --runtime dist/runtime
```

`package.py` 的白名单仅包含运行代码、迁移、安装脚本和用户文档；不打包 `.github`、测试、内部审计记录、AGENTS、生产配置或用户数据。包内镜像和运行组件有独立 SHA-256 校验。Docker/Compose 的上游版本、下载地址和许可证保存在 `runtime/NOTICE.md` 及 `runtime/licenses/`；Compose 同时验证官方发布校验值。

仅生成原生包可运行 `python3 package.py --version 1.4.0`。GitHub 源码继续提供完整维护资料；可选 Aria2 镜像通过现有 workflow 发布供原生用户后续接入。不得在未决定发布时推送或移动发布标签。
