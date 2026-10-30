# 文件中转站 1.4.0

两个版本，一个安装命令。首次安装自动生成每个站点专属的管理员密码和签名密钥，无需复制配置模板。

| 安装包 | 用途 |
| --- | --- |
| `file-transfer-with-aria2-1.4.0.tar.gz` | 完整版：网盘 + Aria2 离线下载，内置 Linux amd64 镜像及 Docker/Compose 运行组件 |
| `file-transfer-native-1.4.0.tar.gz` | 原生版：自动安装 Python 环境和 systemd 服务，默认无离线下载，可后续接入适配容器 |

## 启动

解压任一安装包后进入 `file-transfer` 目录：

```bash
sudo bash install.sh
sudo bash ftctl password
```

默认访问 `http://服务器IP:8000`，管理员 `admin`，使用生成的随机密码，首次登录后改密。

联网服务器也可直接使用一键命令，`bundled` 换成 `native` 即原生版：

```bash
curl -fsSL https://raw.githubusercontent.com/lingmo77/FileTransit/v1.4.0/quick_start.sh | sudo bash -s -- bundled
```

完整版支持 Linux amd64 + systemd，已有 Docker 时直接复用；镜像及 Docker/Compose 无需额外下载。精简系统缺少 iptables 等系统工具时需联网补装。原生版支持 Ubuntu 24.04+ / Debian 13+，需要联网安装 Python 3.12+ 和依赖。安装说明：[选择版本、管理与升级](https://github.com/lingmo77/FileTransit/blob/v1.4.0/EDITIONS.md)。

## 本次调整

- 发布附件精简为两个安装包和 `SHA256SUMS`，仓库首页只保留选择版本与快速启动。
- 统一 `ftctl` 管理安装、启动、停止、重启、状态、日志及查看初始密码。
- 完整版首次安装默认启用离线下载，自动使用服务器地址初始化网络保护；绑定域名后可在后台调整。
- 每个新实例生成独立随机密码和密钥，Aria2 另外生成持久化 RPC 密钥；管理员明文密码不再写入应用日志。
- 重复安装保留 `.env`、密码、密钥及已有后台设置；修复自定义宿主机端口影响容器内部监听的问题。

本次没有新增数据库迁移，迁移头仍为 `a6b9e2037c51`。已有部署保留原数据库、用户文件、所有数据卷、`.env`、密钥、Compose 项目名及覆盖配置。旧版 Docker 安装请先按安装说明合并自定义配置，不要直接覆盖原 Compose。不能用旧数据库覆盖新增用户数据。

## 验证

- 本地 `.venv` 完整回归：215 项通过。
- 服务器验证原生版安装、随机密码登录改密、上传下载、覆盖升级与数据保留。
- 验证完整版复用已有 Docker，以及空白 systemd 环境自动安装包内 Docker 29.9.2 / Compose 5.6.0。
- 完整版与原生版后接 Aria2 均验证真实 HTTP 离线下载和默认私密入库。
- 生产网盘及 Aria2 全程保持运行，测试使用独立目录、端口与数据；结束后停止测试服务并核验生产健康状态。
