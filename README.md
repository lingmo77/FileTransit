# 文件中转站

上传、分享和下载临时文件，到期自动清理。访客可下载公开文件，用户登录后上传，管理员可调整配额、有效期和站点设置。

**当前版本：1.4.0** · [下载安装包](https://github.com/lingmo77/FileTransit/releases/latest) · [安装与升级](EDITIONS.md)

## 选择一种部署方式

| | 完整版（推荐） | 原生版 |
| --- | --- | --- |
| 安装包 | `file-transfer-with-aria2-1.4.0.tar.gz` | `file-transfer-native-1.4.0.tar.gz` |
| 运行方式 | Docker，内置网盘和 Aria2 镜像及容器运行组件 | 自动安装 Python 虚拟环境和 systemd 服务 |
| 离线下载功能 | 自带，支持 HTTP/HTTPS、磁力和 BT | 默认不启用，以后可接入适配的 Aria2 容器 |
| 安装网络 | 镜像和 Docker/Compose 无需联网下载 | 需要联网安装 Python 和依赖 |
| 支持服务器 | Linux amd64 + systemd，需具备 iptables、mount | Ubuntu 24.04+ / Debian 13+，Python 3.12+ |

两种安装包都不需要手工填写密码或密钥。首次安装为**每个站点实例**生成随机管理员密码、签名密钥；完整版本的 Aria2 单独生成 RPC 密钥。各网站账号的密码仍由注册用户自行设置。

## 一键启动

联网安装可直接运行（默认为完整版；末尾的 `bundled` 换为 `native` 即原生版）：

```bash
curl -fsSL https://raw.githubusercontent.com/lingmo77/FileTransit/v1.4.0/quick_start.sh | sudo bash -s -- bundled
```

也可以下载安装包后启动：

下载其中一个包，上传到服务器 `/opt`，解压后执行：

```bash
tar xzf file-transfer-with-aria2-1.4.0.tar.gz -C /opt
cd /opt/file-transfer
sudo bash install.sh
```

原生版只需把文件名换成 `file-transfer-native-1.4.0.tar.gz`，其余步骤相同。

打开安装结果中的地址，默认端口 **8000**，管理员 **admin**。查看随机初始密码：

```bash
sudo bash ftctl password
```

首次登录后修改密码。后续升级沿用 `.env`，不会重新生成密码和密钥。

```bash
sudo bash ftctl status    # 状态
sudo bash ftctl restart   # 重启
sudo bash ftctl stop      # 停止，保留数据
sudo bash ftctl logs      # 日志
```

系统默认使用服务器网卡地址，无域名也可使用。绑定域名或使用反向代理后，在管理后台更新“站点根地址”，以便邮件和离线下载网络保护使用正确的访问地址。自定义端口、原生版后接 Aria2、数据保留及升级步骤见 [安装说明](EDITIONS.md)。

## 开发

FastAPI + SQLAlchemy + SQLite，Jinja2 + 原生 JS/CSS，无前端构建步骤。使用 Python 3.12+：

```bash
python -m venv .venv
# Linux: source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest -q
```

直接从源码启动使用开发默认配置；生产请通过安装器生成独立凭据。测试使用独立临时数据目录。更多技术信息见 [部署与构建](DEPLOY.md) 和 [项目说明](PROJECT.md)。
