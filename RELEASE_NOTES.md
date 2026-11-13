Docker 版新增 `sudo bash ftctl uninstall`，默认保留数据，输入 `DELETE` 才删除本实例专用数据卷。卸载会保留 Docker 服务及共享镜像。

安装器在 Docker 尚未启动时检查 IPv4 和 IPv6 的 iptables 工具，并通过 apt、dnf 或 yum 补装。

下载源码包或含网盘、Aria2 和容器运行组件的 Docker 安装包。解压后在安装目录运行 `sudo bash install.sh`。
