Docker 版新增一键卸载，默认保留数据库和上传文件。在精简版 Linux 上安装时，如果 Docker 因缺少 `iptables` 或 `ip6tables` 无法启动，安装器会通过 `apt`、`dnf` 或 `yum` 自动补齐。

### 下载安装

| 安装包 | 用途 |
| --- | --- |
| `file-transfer-with-aria2-1.4.1.tar.gz` | Linux amd64 一键部署，包含网盘、Aria2 离线下载和容器运行组件 |
| `file-transfer-src-1.4.1.tar.gz` | 完整源码，用于原生部署、二次开发或自行构建镜像 |

在线安装 Docker 版：

```bash
curl -fsSL https://raw.githubusercontent.com/lingmo77/FileTransit/v1.4.1/quick_start.sh | sudo bash -s -- bundled
```

手动下载安装包后，解压并进入 `file-transfer` 目录：

```bash
sudo bash install.sh
sudo bash ftctl password
```

默认访问 `http://服务器IP:8000`，管理员账号为 `admin`，第二条命令可以查看首次安装时生成的随机密码。

### 卸载 Docker 版

在原安装目录执行：

```bash
sudo bash ftctl uninstall
```

直接回车即可卸载并保留数据，之后在原目录重装可以继续使用原账号和文件。**输入大写 `DELETE` 会永久删除列出的专用数据卷**，包括其中的数据库、上传文件和离线任务。

Docker 服务会继续运行，其他容器正在使用的镜像和数据卷也会保留，配置和密钥仍在原安装目录的 `.env` 中。
