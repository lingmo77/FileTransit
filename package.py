"""Build the two release archives; never include live configuration or data.

完整版：docker-compose.yml + .env.example + 一个包含网盘、内置 PostgreSQL 与 Aria2 三个镜像的 tar，
用户只需 ``docker load`` 和 ``docker compose up -d``。镜像在构建服务器上生成并导出：

    docker save file-transfer:<版本> file-transfer-postgres:<版本> file-transfer-aria2:<版本> -o images.tar
    python3 package.py --version <版本> --images images.tar

源码版：直接部署所需的文件（应用代码、迁移、数据库初始化脚本、依赖清单、配置模板）加说明。
两个包解压后都是 file-transfer/ 目录。本脚本只用标准库。

产物写到仓库旁边的 release/<版本>/（不进仓库），每个版本一个目录：两个包加 SHA256SUMS。
release/ 只保留版本号最新的两个版本，更旧的版本目录在打包后自动删除。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import re
import shutil
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
#: 默认输出目录：与仓库目录并列的 release/
OUTPUT = ROOT.parent / "release"
TOP = "file-transfer"
#: release/ 里保留的版本数
KEEP_VERSIONS = 2
VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

#: 数据库初始化脚本（由 db/generate_init_sql.py 生成）；生成脚本本身不随包发布
INIT_SQL = ["db/init-postgresql.sql", "db/init-sqlite.sql"]
#: 源码版内容：python -m venv + pip install -r requirements.txt + uvicorn 即可运行
SOURCE_FILES = ["app", "migrations", "alembic.ini", "requirements.txt", ".env.example", *INIT_SQL]
#: 两个包都附带的说明
DOCS = ["README.md", "LICENSE"]
#: 完整版的配置模板，在包里叫 .env.example（源码版用仓库根目录的 .env.example）
FULL_ENV_EXAMPLE = "docker/env.example"


def checksum(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def filtered(info):
    if "__pycache__" in Path(info.name).parts or info.name.endswith((".pyc", ".pyo")):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mode = 0o755 if info.isdir() else 0o644
    return info


def add_path(archive, source, arcname):
    """像 archive.add 一样递归添加，但文本文件统一为 LF 换行。

    Windows 上的 git 默认把工作区文件检出为 CRLF；不论在哪台机器打包，发布包都和仓库里一样是 LF。
    含 NUL 字节的视为二进制，原样写入。
    """
    source = Path(source)
    info = filtered(archive.gettarinfo(str(source), arcname))
    if info is None:
        return
    if info.isdir():
        archive.addfile(info)
        for child in sorted(source.iterdir()):
            add_path(archive, child, f"{arcname}/{child.name}")
        return
    data = source.read_bytes()
    if b"\0" not in data:
        data = data.replace(b"\r\n", b"\n")
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def add_text(archive, name, text):
    data = text.encode("utf-8")
    info = tarfile.TarInfo(f"{TOP}/{name}")
    info.size = len(data)
    info.mode = 0o644
    archive.addfile(info, io.BytesIO(data))


#: 完整版里的镜像（docker save 时三个都要导出）
IMAGES = ["file-transfer", "file-transfer-postgres", "file-transfer-aria2"]


def release_compose(version):
    """去掉开头的仓库说明和 build 段，镜像标签固定为本次版本，只使用已 docker load 的镜像。

    其余注释（数据目录、切换数据库时要删除哪几处）是写给用户的，原样保留。
    """
    source = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").splitlines()
    start = source.index("name: file-transfer")
    lines = []
    skip_build = False
    for line in source[start:]:
        if line == "    build:":
            skip_build = True
            continue
        if skip_build:
            if line.startswith("      "):
                continue
            skip_build = False
        match = re.fullmatch(r"    image: ([a-z0-9-]+):.+", line)
        if match:
            if match.group(1) not in IMAGES:
                raise ValueError(f"docker-compose.yml 里有未知镜像 {match.group(1)}")
            lines.append(f"    image: {match.group(1)}:{version}")
            lines.append("    pull_policy: never")
            continue
        lines.append(line)
    header = (f"# 文件中转站 {version} 完整版\n"
              f"#   docker load -i images/file-transfer-images-{version}.tar\n"
              "#   docker compose up -d\n")
    return header + "\n".join(lines).strip("\n") + "\n"


def version_key(name):
    """1.10.0 排在 1.9.0 之后；数字段比字符串段大，所以 1.5.0 排在 1.5.0-rc1 之后。"""
    return [(1, int(part), "") if part.isdigit() else (0, 0, part)
            for part in re.split(r"[._-]", name)]


def prune(root, keep=KEEP_VERSIONS):
    """只删除 package.py 生成的版本目录（目录名是版本号且装着发布包），返回删除的目录。"""
    versions = sorted((path for path in root.iterdir()
                       if path.is_dir() and VERSION_RE.fullmatch(path.name)
                       and any(path.glob(f"{TOP}-*-{path.name}.tar.gz"))),
                      key=lambda path: version_key(path.name), reverse=True)
    for old in versions[keep:]:
        shutil.rmtree(old)
        print("removed old release", old.name, flush=True)
    return versions[keep:]


def build(version, images=None, output=None):
    if not VERSION_RE.fullmatch(version):
        raise ValueError("Invalid version")
    if images is not None and not Path(images).is_file():
        raise ValueError(f"Missing image archive {images}")
    root = Path(output) if output else OUTPUT
    release = root / version
    # 重新打同一版本时先清掉旧产物，避免残留上一次的完整版
    if release.exists():
        shutil.rmtree(release)
    release.mkdir(parents=True)
    outputs = []
    for kind in (["src", "full"] if images else ["src"]):
        target = release / f"file-transfer-{kind}-{version}.tar.gz"
        with tarfile.open(target, "w:gz") as archive:
            for entry in [*(SOURCE_FILES if kind == "src" else INIT_SQL), *DOCS]:
                add_path(archive, ROOT / entry, f"{TOP}/{entry}")
            add_text(archive, "VERSION", version + "\n")
            if kind == "full":
                add_text(archive, "docker-compose.yml", release_compose(version))
                add_path(archive, ROOT / FULL_ENV_EXAMPLE, f"{TOP}/.env.example")
                archive.add(images, arcname=f"{TOP}/images/file-transfer-images-{version}.tar",
                            filter=filtered)
        outputs.append(target)
        print(target.name, target.stat().st_size, checksum(target), flush=True)
    (release / "SHA256SUMS").write_text("".join(
        f"{checksum(path)}  {path.name}\n" for path in outputs), encoding="utf-8")
    prune(root)
    return outputs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True)
    parser.add_argument("--images", type=Path,
                        help="docker save 导出的 tar，同时包含网盘和 Aria2 镜像；不给则只生成源码版")
    parser.add_argument("--output", type=Path, help=f"输出根目录，产物写到其中的 <版本>/ 子目录，默认 {OUTPUT}")
    args = parser.parse_args()
    build(args.version, args.images, args.output)
