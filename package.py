"""Build the two public installers; never include live configuration or data."""
from __future__ import annotations

import argparse
import hashlib
import io
import re
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ["app", "migrations", "deploy", "install.sh", "ftctl", "alembic.ini",
            "requirements.txt", "LICENSE", "README.md", "EDITIONS.md", "DEPLOY.md"]


def checksum(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def filtered(info):
    if "__pycache__" in Path(info.name).parts or info.name.endswith((".pyc", ".pyo")):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mode = 0o755 if info.isdir() or info.name.endswith((".sh", "/ftctl")) else 0o644
    return info


def add_text(archive, name, text):
    data = text.encode("utf-8")
    info = tarfile.TarInfo(f"file-transfer/{name}")
    info.size = len(data)
    info.mode = 0o644
    archive.addfile(info, io.BytesIO(data))


def bundled_compose():
    lines = []
    skip_build = False
    for line in (ROOT / "docker-compose.aria2.yml").read_text(encoding="utf-8").splitlines():
        if line == "    build:":
            skip_build = True
            continue
        if skip_build:
            if line.startswith("      "):
                continue
            skip_build = False
        if line.lstrip().startswith("#"):
            continue
        if "image: ${FT_ARIA2_IMAGE:" in line:
            line = "    image: file-transfer-aria2:1.4.0"
        lines.append(line)
    return "\n".join(lines) + "\n"


def build(version, image=None, aria2_image=None, runtime=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        raise ValueError("Invalid version")
    if bool(image) != bool(aria2_image) or bool(image) != bool(runtime):
        raise ValueError("Full installer requires --image, --aria2-image and --runtime together")
    if runtime:
        for name in ("docker.tgz", "docker-compose", "SHA256SUMS", "NOTICE.md"):
            if not (Path(runtime) / name).is_file():
                raise ValueError(f"Missing runtime/{name}")
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    outputs = []
    for kind in (["native", "with-aria2"] if image else ["native"]):
        target = dist / f"file-transfer-{kind}-{version}.tar.gz"
        with tarfile.open(target, "w:gz") as archive:
            for entry in MANIFEST:
                archive.add(ROOT / entry, arcname=f"file-transfer/{entry}", filter=filtered)
            add_text(archive, ".edition", "bundled\n" if kind == "with-aria2" else "native\n")
            add_text(archive, "VERSION", version + "\n")
            if kind == "with-aria2":
                add_text(archive, "docker-compose.yml", bundled_compose())
                images = [(Path(image), "app.tar"),
                          (Path(aria2_image), "aria2.tar")]
                for source, name in images:
                    archive.add(source, arcname=f"file-transfer/images/{name}", filter=filtered)
                add_text(archive, "images/SHA256SUMS", "".join(
                    f"{checksum(source)}  {name}\n" for source, name in images))
                archive.add(runtime, arcname="file-transfer/runtime", filter=filtered)
        outputs.append(target)
        print(target.name, target.stat().st_size, checksum(target), flush=True)
    (dist / "SHA256SUMS").write_text("".join(
        f"{checksum(path)}  {path.name}\n" for path in outputs), encoding="utf-8")
    return outputs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--aria2-image", type=Path)
    parser.add_argument("--runtime", type=Path)
    args = parser.parse_args()
    build(args.version, args.image, args.aria2_image, args.runtime)
